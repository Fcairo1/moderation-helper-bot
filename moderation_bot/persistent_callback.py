#!/usr/bin/env python3
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
from lark_oapi.ws import Client as WsClient

from moderation_bot.config import app_id, app_secret
from moderation_bot.handle_callback import process_action
from moderation_bot.command_handler import (
    CHATS,
    CHAT_IDS,
    EMAIL,
    api,
    excluded_sender_ids,
    get_members,
    handle_command,
    is_probable_request,
    msg_text,
    token,
)
from scan_send_modbr_triage import build_card

BOT_APP_ID = app_id()
START = ROOT / 'moderation_bot/daemon_started_at.txt'
REG = ROOT / 'moderation_bot/card_registry.json'
CURRENT = ROOT / 'moderation_bot/current_card_state.json'
PIDFILE = ROOT / 'moderation_bot/persistent_callback.pid'
HEARTBEAT = ROOT / 'moderation_bot/heartbeat'
SCRIPT_NAME = 'moderation_bot/persistent_callback.py'
HEARTBEAT_INTERVAL_SECONDS = 30
_FILIPE_IDS_BY_CHAT = {}
_RECIPIENT_OPEN_ID = None
_TRIAGE_LOCK = threading.Lock()
_triaging_ids = set()


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
    except Exception:
        return False


def _pid_matches_this_daemon(pid):
    if not _pid_alive(pid):
        return False
    try:
        argv = [a.decode('utf-8', 'ignore') for a in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\x00') if a]
        return bool(argv and 'python' in Path(argv[0]).name.lower() and any(a.endswith(SCRIPT_NAME) for a in argv[1:]))
    except Exception:
        return True


def acquire_singleton():
    try:
        if PIDFILE.exists():
            raw = PIDFILE.read_text(encoding='utf-8').strip()
            if raw.isdigit():
                old = int(raw)
                if old != os.getpid():
                    if _pid_matches_this_daemon(old):
                        print(f'[WARN] another persistent daemon is already running (pid={old}), aborting startup', flush=True)
                        sys.exit(1)
                    print(f'[WARN] stale PID file found (pid={old} dead), clearing and starting fresh', flush=True)
                    PIDFILE.unlink(missing_ok=True)
            else:
                print(f'[WARN] invalid PID file contents in {PIDFILE.name}, clearing and starting fresh', flush=True)
                PIDFILE.unlink(missing_ok=True)
    except SystemExit:
        raise
    except Exception as e:
        print(f'[WARN] PID file check failed: {e!r}', flush=True)
    PIDFILE.write_text(str(os.getpid()), encoding='utf-8')


def _write_heartbeat_once():
    HEARTBEAT.write_text(f'{time.time():.6f}', encoding='utf-8')


def heartbeat_loop():
    while True:
        try:
            _write_heartbeat_once()
        except Exception as e:
            print(f'heartbeat write error: {e!r}', flush=True)
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


def obj_to_dict(obj):
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {k: obj_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [obj_to_dict(v) for v in obj]
    return {k: obj_to_dict(v) for k, v in vars(obj).items() if not k.startswith('_')}


def toast(text, typ='success'):
    return P2CardActionTriggerResponse({'toast': {'type': typ, 'content': text}})


def _resolve_recipient(t):
    global _RECIPIENT_OPEN_ID
    if _RECIPIENT_OPEN_ID:
        return _RECIPIENT_OPEN_ID
    r = api('https://open.larksuite.com/open-apis/contact/v3/users/batch_get_id?' + urllib.parse.urlencode({'user_id_type': 'open_id'}), 'POST', {'emails': [EMAIL]}, t)
    _RECIPIENT_OPEN_ID = ((r.get('data') or {}).get('user_list') or [{}])[0].get('user_id')
    return _RECIPIENT_OPEN_ID


def _filipe_ids(t, chat_id):
    if chat_id not in _FILIPE_IDS_BY_CHAT:
        try:
            members = get_members(t, chat_id)
            _FILIPE_IDS_BY_CHAT[chat_id] = excluded_sender_ids(members)
        except Exception as e:
            print('member fetch error for', chat_id, repr(e), flush=True)
            _FILIPE_IDS_BY_CHAT[chat_id] = set()
    return _FILIPE_IDS_BY_CHAT[chat_id]


def _api_with_retry(label, fn, retries=2, delay=2):
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as e:
            if attempt >= retries:
                print(f'{label}: giving up after {attempt + 1} attempts, final error:', repr(e), flush=True)
                raise
            attempt += 1
            print(f'{label}: attempt {attempt} failed ({e!r}); retrying in {delay}s', flush=True)
            time.sleep(delay)


def _send_triage_card(message_id, chat_id, sender_open_id):
    t = token()
    mr = _api_with_retry('triage message fetch', lambda: api(f'https://open.larksuite.com/open-apis/im/v1/messages/{message_id}', token=t))
    items = ((mr.get('data') or {}).get('items') or [])
    if not items:
        return {'skipped': 'message not found'}
    m = items[0]
    sender_name = sender_open_id or 'Unknown'
    try:
        members = get_members(t, chat_id)
        sender_name = members.get(sender_open_id, sender_name)
    except Exception:
        pass
    chat_name = CHATS.get(chat_id, chat_id)
    card = build_card(m, sender_name, chat_name)
    rec = _resolve_recipient(t)
    send = _api_with_retry('triage DM send', lambda: api('https://open.larksuite.com/open-apis/im/v1/messages?' + urllib.parse.urlencode({'receive_id_type': 'open_id'}), 'POST', {'receive_id': rec, 'msg_type': 'interactive', 'content': json.dumps(card, ensure_ascii=False)}, t))
    cmid = (send.get('data') or {}).get('message_id')
    try:
        reg = json.loads(REG.read_text()) if REG.exists() else {}
        cur = json.loads(CURRENT.read_text()) if CURRENT.exists() else {}
        reg[message_id] = cmid
        cur[cmid] = card
        REG.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding='utf-8')
        CURRENT.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        print('registry update error:', repr(e), flush=True)
    return {'card_message_id': cmid, 'target_message_id': message_id, 'chat_id': chat_id}


def _triage_worker(message_id, chat_id, sender_open_id):
    try:
        t = token()
        try:
            rr = api(f'https://open.larksuite.com/open-apis/im/v1/messages/{message_id}/reactions?page_size=50', token=t)
            if ((rr.get('data') or {}).get('items') or []):
                print('triage skip: already has reactions', message_id, flush=True)
                return
        except Exception as e:
            print('reactions check error:', repr(e), flush=True)
        try:
            reg = json.loads(REG.read_text()) if REG.exists() else {}
            if message_id in reg:
                print('triage skip: already triaged', message_id, flush=True)
                return
        except Exception:
            pass
        print('triage sending DM card for', message_id, 'from', chat_id, flush=True)
        res = _send_triage_card(message_id, chat_id, sender_open_id)
        print('triage sent:', json.dumps(res, ensure_ascii=False), flush=True)
    except Exception as e:
        print('triage worker error:', repr(e), flush=True)
        traceback.print_exc()
    finally:
        with _TRIAGE_LOCK:
            _triaging_ids.discard(message_id)


def _first_present(*values):
    for value in values:
        if value:
            return value
    return None


def _coerce_action_value(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            print('card.action.trigger: action value was not valid JSON:', raw[:500], flush=True)
            return {}
    return {}


def handle_card_action(data):
    try:
        payload = obj_to_dict(data)
        print('card.action.trigger payload:', json.dumps(payload, ensure_ascii=False), flush=True)
        ev = payload.get('event') or {}
        action = ev.get('action') or {}
        value = _coerce_action_value(action.get('value') or ev.get('value') or {})
        context = ev.get('context') or {}
        card_message_id = _first_present(context.get('open_message_id'), context.get('message_id'), ev.get('open_message_id'), ev.get('message_id'), action.get('open_message_id'), action.get('message_id'))
        op = (ev.get('operator') or {})
        operator_name = _first_present(op.get('name'), op.get('open_id'), op.get('user_id'))
        print('card.action.trigger parsed:', json.dumps({'card_message_id': card_message_id, 'value': value, 'operator_name': operator_name}, ensure_ascii=False), flush=True)
        if not value:
            return toast('Callback failed: empty button payload', 'error')
        latest = None
        try:
            reg = json.loads((ROOT / 'moderation_bot/card_registry.json').read_text(encoding='utf-8'))
            latest = reg.get(value.get('target_message_id') or value.get('original_message_id'))
        except Exception as e:
            print('card registry read error:', repr(e), flush=True)
        if latest and card_message_id and latest != card_message_id:
            return toast('A newer card exists for this request — please use the latest one', 'warning')

        def worker():
            try:
                result = process_action(value, card_message_id=card_message_id, operator_name=operator_name)
                print('moderation callback processed:', json.dumps(result, ensure_ascii=False), flush=True)
            except Exception as e:
                print('moderation callback worker error:', repr(e), flush=True)
                traceback.print_exc()

        threading.Thread(target=worker, daemon=True).start()
        return toast('Moderation status update received')
    except Exception as e:
        print('moderation card.action.trigger error:', repr(e), flush=True)
        traceback.print_exc()
        return toast(f'Callback failed: {e}', 'error')


def handle_message_receive(data):
    try:
        payload = obj_to_dict(data)
        print('im.message.receive_v1 payload:', json.dumps(payload, ensure_ascii=False)[:4000], flush=True)
        ev = payload.get('event') or {}
        msg = ev.get('message') or {}
        chat_id = msg.get('chat_id')
        message_id = msg.get('message_id')
        content = msg.get('content') or ''
        sender = ev.get('sender') or {}
        sender_id_obj = sender.get('sender_id') or {}
        sender_open_id = sender_id_obj.get('open_id') if isinstance(sender_id_obj, dict) else None
        sender_type = (sender.get('sender_type') or '').lower()
        txt = msg_text(content).strip()
        if txt.startswith('/'):
            def cworker():
                try:
                    print('command processed:', json.dumps(handle_command(txt, chat_id, message_id), ensure_ascii=False), flush=True)
                except Exception as e:
                    print('command worker error:', repr(e), flush=True)
                    traceback.print_exc()
            threading.Thread(target=cworker, daemon=True).start()
            return
        if chat_id not in CHAT_IDS:
            return
        if not txt:
            return
        if sender_type in ('app', 'bot'):
            print('triage skip: bot sender', flush=True)
            return
        try:
            t = token()
            if sender_open_id and sender_open_id in _filipe_ids(t, chat_id):
                print('triage skip: Filipe sender', flush=True)
                return
        except Exception as e:
            print('filipe check error:', repr(e), flush=True)
        if not is_probable_request(txt):
            print('triage skip: heuristic non-request:', txt[:80], flush=True)
            return
        with _TRIAGE_LOCK:
            if message_id in _triaging_ids:
                print('triage skip: already in-flight (dedup guard)', message_id, flush=True)
                return
            _triaging_ids.add(message_id)
        threading.Thread(target=_triage_worker, args=(message_id, chat_id, sender_open_id), daemon=True).start()
    except Exception as e:
        print('message receive handler error:', repr(e), flush=True)
        traceback.print_exc()


def handle_noop_event(data):
    try:
        payload = obj_to_dict(data)
        event_type = ((payload.get('header') or {}).get('event_type') or 'unknown') if isinstance(payload, dict) else 'unknown'
        print('noop event ignored:', event_type, flush=True)
    except Exception as e:
        print('noop event handler error:', repr(e), flush=True)
        traceback.print_exc()


def _build_handler():
    builder = lark.EventDispatcherHandler.builder('', '').register_p2_card_action_trigger(handle_card_action)
    if hasattr(builder, 'register_p2_im_message_receive_v1'):
        builder = builder.register_p2_im_message_receive_v1(handle_message_receive)
    else:
        print('WARNING: lark_oapi builder has no register_p2_im_message_receive_v1; im.message.receive_v1 subscription may require SDK method name update.', flush=True)
    noop_registrations = (
        'register_p2_im_chat_access_event_bot_p2p_chat_entered_v1',
        'register_p2_im_message_reaction_created_v1',
        'register_p2_im_message_reaction_deleted_v1',
    )
    for method_name in noop_registrations:
        if hasattr(builder, method_name):
            builder = getattr(builder, method_name)(handle_noop_event)
        else:
            print(f'WARNING: lark_oapi builder has no {method_name}; related events may log processor-not-found errors.', flush=True)
    return builder.build()


def main():
    START.write_text(datetime.now().isoformat(), encoding='utf-8')
    acquire_singleton()
    _write_heartbeat_once()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    backoff = 5
    while True:
        try:
            handler = _build_handler()
            cli = WsClient(BOT_APP_ID, app_secret(), event_handler=handler)
            print('Starting REAL Lark WsClient persistent connection for moderation bot with card + command + triage handlers', BOT_APP_ID, 'monitoring', list(CHATS.values()), flush=True)
            cli.start()
            print(f'WsClient.start() returned; reconnecting in {backoff}s', flush=True)
        except KeyboardInterrupt:
            print('persistent callback interrupted; exiting', flush=True)
            raise
        except SystemExit:
            raise
        except Exception as e:
            print('persistent callback WsClient loop error:', repr(e), flush=True)
            traceback.print_exc()
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('persistent callback fatal error:', repr(e), flush=True)
        traceback.print_exc()
        raise
