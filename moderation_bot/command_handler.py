#!/usr/bin/env python3
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import requests

from moderation_bot.config import app_id, app_secret, monitored_chats, owner_email, primary_chat_id
from moderation_bot.admin_client import AdminLookupError, DEFAULT_REGION as DEFAULT_ADMIN_REGION, format_lookup, lookup, one_search
from moderation_bot.review_watcher import active_watches

BOT_APP_ID = app_id()
EMAIL = owner_email()
CHATS = monitored_chats()
CHAT_IDS = list(CHATS.keys())
CHAT_ID = primary_chat_id()
ROOT = Path(__file__).resolve().parents[1]
REG = ROOT / 'moderation_bot/card_registry.json'
CURRENT = ROOT / 'moderation_bot/current_card_state.json'
START = ROOT / 'moderation_bot/daemon_started_at.txt'
HEARTBEAT = ROOT / 'moderation_bot/heartbeat'
REQUEST_KEYWORDS = re.compile(r'\b(solicita(?:ç|c)[aã]o|solicito|pedido|release|lan[cç]amento|[áa]lbum|album|single|ep\b|faixa|track|m[uú]sica|artista|cantor|banda|selo|upc|isrc|approve|approval|aprovar|aprova|aprova[cç][aã]o|aprovem|moderar|moderem|urgente|urgent|rejeitar|rejei[cç][aã]o|rejected|moderation|modera[cç][aã]o|rean[aá]lise|reanalise|an[aá]lise|metadata|metadados|cover|distribui[cç][aã]o)\b', re.I)
FORCED_REQUEST_RE = re.compile(
    r'\b(?:please\s+approve|approve|approval|aprovar|aprova(?:r|ção|cao)?|aprovem|'
    r'please\s+moderate|moderar|moderem|rejected|rejeitad[oa]s?|reprovad[oa]s?)\b',
    re.I,
)
SHORT_ACK_RE = re.compile(r'^(?:ok(?:ay)?|okkk+|obrigad[oa]|valeu|vlw|show|boa|perfeito|fechado|entendi|certo|blz|beleza|thanks?|tmj|feito|resolvido|resolvida|aprovado|rejeitado|de nada|isso|sim|n[aã]o|yes|no|👍+|🙏+|✅+|👀+|👏+|🙌+|🙂+|😉+|😂+|🔥+|❤️+|❤+)$', re.I)
PURE_EMOJI_RE = re.compile(r'^[\W_\s\u2600-\u27BF\U0001F000-\U0001FAFF]+$', re.UNICODE)
ISRC_RE = re.compile(r'\b[A-Z]{2}[A-Z0-9]{3}\d{7}\b')
UPC_RE = re.compile(r'\b\d{12,13}\b')
LINK_ID_RE = re.compile(r'(albumId|songId|userId|trackId)=\d+', re.I)
BOT_STATUS_REACTIONS = {'DONE', 'CRY', 'THINKING'}


def api(url, method='GET', payload=None, token=None, retries=3, retry_delay=2):
    headers = {}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    kwargs = {'headers': headers, 'timeout': 30}
    if payload is not None:
        kwargs['json'] = payload
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, **kwargs)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.SSLError:
            try:
                r = requests.request(method, url, verify=False, **kwargs)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_error = e
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError, requests.exceptions.ProxyError) as e:
            last_error = e
        if attempt < retries:
            print(f'api retry {attempt}/{retries} for {method} {url}: {last_error!r}; retrying in {retry_delay}s', flush=True)
            time.sleep(retry_delay)
    if last_error:
        raise last_error
    r = requests.request(method, url, **kwargs)
    r.raise_for_status()
    return r.json()


def token():
    return api(
        'https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal',
        'POST',
        {'app_id': BOT_APP_ID, 'app_secret': app_secret()},
        retries=4,
    )['tenant_access_token']


def msg_text(content):
    try:
        obj = json.loads(content or '{}')
    except Exception:
        return content or ''
    def render(x):
        if isinstance(x, str):
            return x
        if isinstance(x, list):
            return ' '.join(filter(None, (render(v) for v in x)))
        if not isinstance(x, dict):
            return ''

        # Lark post messages can contain equivalent locale branches. Select a
        # single branch so the entire message is not rendered two or three times.
        for locale in ('en_us', 'pt_br', 'zh_cn', 'ja_jp'):
            if locale in x and isinstance(x[locale], (dict, list)):
                return render(x[locale])
        tag = str(x.get('tag') or '').lower()
        if tag in ('a', 'link'):
            return str(x.get('href') or x.get('url') or x.get('text') or '')
        if tag in ('at', 'mention'):
            return str(x.get('name') or x.get('text') or '')
        if tag == 'text':
            return str(x.get('text') or '')
        if isinstance(x.get('content'), (dict, list)):
            title = str(x.get('title') or '').strip()
            body = render(x['content'])
            return ' '.join(part for part in (title, body) if part)
        if isinstance(x.get('text'), str):
            return x['text']

        # Unknown message types: walk values, but de-duplicate identical blocks.
        parts = []
        seen = set()
        for value in x.values():
            part = ' '.join(render(value).split())
            if part and part not in seen:
                parts.append(part)
                seen.add(part)
        return ' '.join(parts)

    return ' '.join(render(obj).split()).strip()


def is_forced_request(txt):
    return bool(FORCED_REQUEST_RE.search(txt or ''))


def reply(chat_id, message_id, text):
    return api(
        f'https://open.larksuite.com/open-apis/im/v1/messages/{message_id}/reply',
        'POST',
        {'msg_type': 'text', 'content': json.dumps({'text': text}, ensure_ascii=False)},
        token(),
    )


def handle_admin_lookup(identifier, chat_id, message_id):
    identifier = (identifier or '').strip()
    if not identifier:
        return reply(chat_id, message_id, 'Usage: /admin <UPC, ISRC, album ID, song ID, artist ID, or user ID>')
    try:
        body = format_lookup(lookup(identifier))
    except AdminLookupError as exc:
        body = f'⚠️ SoundOn Admin lookup failed: {exc}'
    except Exception as exc:
        print('unexpected Admin lookup failure:', repr(exc), flush=True)
        body = '⚠️ SoundOn Admin lookup failed unexpectedly. Check the bot logs.'
    return reply(chat_id, message_id, body)


def get_members(t, chat_id=None):
    chat_id = chat_id or CHAT_ID
    members = {}
    page = ''
    while True:
        params = {'member_id_type': 'open_id', 'page_size': '100'}
        if page:
            params['page_token'] = page
        r = api(f'https://open.larksuite.com/open-apis/im/v1/chats/{chat_id}/members?' + urllib.parse.urlencode(params), token=t)
        for it in (r.get('data') or {}).get('items') or []:
            members[it.get('member_id')] = it.get('name') or it.get('member_id')
        if not (r.get('data') or {}).get('has_more'):
            break
        page = (r.get('data') or {}).get('page_token') or ''
    return members


def excluded_sender_ids(members):
    return {k for k, v in members.items() if 'filipe cairo' in (v or '').lower() or (v or '').strip() == 'Filipe Cairo'}


def is_bot_message(m):
    sender = m.get('sender') or {}
    sender_id = sender.get('id') or sender.get('sender_id') or ''
    sender_type = (sender.get('sender_type') or sender.get('type') or '').lower()
    return sender_id == BOT_APP_ID or sender_type in ('app', 'bot')


def has_bot_status_reaction(items):
    for item in items or []:
        operator = item.get('operator') or {}
        reaction_type = item.get('reaction_type') or {}
        emoji = reaction_type.get('emoji_type') or item.get('emoji_type')
        if emoji in BOT_STATUS_REACTIONS and (
            operator.get('operator_type') == 'app'
            or operator.get('operator_id') == BOT_APP_ID
        ):
            return True
    return False


def is_probable_request(txt):
    s = (txt or '').strip()
    if not s:
        return False
    compact = ' '.join(s.split())
    lower = compact.lower()
    if is_forced_request(compact):
        return True
    if SHORT_ACK_RE.fullmatch(compact) or PURE_EMOJI_RE.fullmatch(compact):
        return False
    if len(compact) <= 20 and not REQUEST_KEYWORDS.search(compact) and not ISRC_RE.search(compact) and not UPC_RE.search(compact) and not LINK_ID_RE.search(compact):
        return False
    if REQUEST_KEYWORDS.search(compact):
        return True
    if ISRC_RE.search(compact) or UPC_RE.search(compact) or LINK_ID_RE.search(compact):
        return True
    if 'http://' in lower or 'https://' in lower:
        return True
    if len(compact) >= 80 and any(ch.isdigit() for ch in compact):
        return True
    if len(compact) >= 140 and any(ch.isalpha() for ch in compact):
        return True
    return False


def _fetch_for_chat(chat_id, t, start, end, request_filter, members, stats, include_reacted=False):
    out = []
    msgs = []
    page = ''
    while True:
        params = {'container_id_type': 'chat', 'container_id': chat_id, 'start_time': str(start), 'end_time': str(end), 'page_size': '50', 'sort_type': 'ByCreateTimeAsc'}
        if page:
            params['page_token'] = page
        r = api('https://open.larksuite.com/open-apis/im/v1/messages?' + urllib.parse.urlencode(params), token=t)
        msgs += (r.get('data') or {}).get('items') or []
        if not (r.get('data') or {}).get('has_more'):
            break
        page = (r.get('data') or {}).get('page_token') or ''
    filipe_ids = excluded_sender_ids(members)
    chat_stats = stats.setdefault('by_chat', {}).setdefault(
        chat_id,
        {'name': CHATS.get(chat_id, chat_id), 'total': 0, 'pending': 0, 'skipped_non_request': 0, 'already_had_reactions': 0},
    )
    for m in msgs:
        stats['total_messages'] += 1
        chat_stats['total'] += 1
        txt = msg_text((m.get('body') or {}).get('content', '')).strip()
        if not txt or txt.startswith('/'):
            stats['skipped_empty_or_command'] += 1
            continue
        if is_bot_message(m):
            stats['skipped_bot'] += 1
            continue
        sid = (m.get('sender') or {}).get('id')
        if sid in filipe_ids:
            stats['skipped_filipe'] += 1
            continue
        if request_filter and not is_probable_request(txt):
            stats['skipped_non_request'] += 1
            chat_stats['skipped_non_request'] += 1
            continue
        if not include_reacted:
            rr = api(f'https://open.larksuite.com/open-apis/im/v1/messages/{m["message_id"]}/reactions?page_size=50', token=t)
            # Explicit approval/moderation requests are guaranteed a card when
            # there are only unrelated reactions, but a bot status reaction
            # means the card was already acted on.
            reactions = ((rr.get('data') or {}).get('items') or [])
            if reactions and (has_bot_status_reaction(reactions) or not is_forced_request(txt)):
                stats['already_had_reactions'] += 1
                chat_stats['already_had_reactions'] += 1
                continue
        ts = datetime.datetime.fromtimestamp(int(m['create_time']) / 1000).strftime('%m-%d %H:%M')
        out.append({'message_id': m['message_id'], 'sender_id': sid, 'sender': members.get(sid, sid or 'Unknown'), 'snippet': txt[:100].replace('\n', ' '), 'text': txt, 'time': ts, 'message': m, 'chat_id': chat_id, 'chat_name': CHATS.get(chat_id, chat_id)})
        stats['pending'] += 1
        chat_stats['pending'] += 1
    return out


def fetch_unreacted_candidates(days=7, start_time=None, end_time=None, request_filter=True, chat_ids=None, include_reacted=False):
    t = token()
    end = int(end_time or time.time())
    start = int(start_time or (end - days * 86400))
    chat_ids = chat_ids or CHAT_IDS
    stats = {'total_messages': 0, 'skipped_empty_or_command': 0, 'skipped_filipe': 0, 'skipped_bot': 0, 'skipped_non_request': 0, 'already_had_reactions': 0, 'pending': 0, 'by_chat': {}, 'chat_errors': {}}
    out = []
    for cid in chat_ids:
        try:
            members = get_members(t, cid)
            out += _fetch_for_chat(cid, t, start, end, request_filter, members, stats, include_reacted=include_reacted)
        except Exception as exc:
            # One unavailable group must not prevent the other group from being
            # scanned. Surface the failure in command/digest diagnostics.
            stats['chat_errors'][cid] = f'{type(exc).__name__}: {exc}'
            print(f'group scan failed for {CHATS.get(cid, cid)} ({cid}): {exc!r}', flush=True)
    return out, stats


def list_recent_unreacted(days=7):
    out, _ = fetch_unreacted_candidates(days=days, request_filter=True)
    return out


def _run_card_sender(*args):
    command = [sys.executable, str(ROOT / 'scan_send_modbr_triage.py'), *args]
    process = subprocess.run(command, capture_output=True, text=True, timeout=600)
    output = (process.stdout or process.stderr or '').strip()
    if process.returncode != 0:
        raise RuntimeError(output[-1200:] or f'card sender exited with {process.returncode}')
    return output[-1800:]


def _health_report():
    from moderation_bot.watchdog import ensure_daemon

    status, pid = ensure_daemon()
    now = time.time()
    heartbeat_age = None
    try:
        heartbeat_age = max(0, int(now - float(HEARTBEAT.read_text().strip())))
    except Exception:
        pass
    _, stats = fetch_unreacted_candidates(
        start_time=int(now) - 300,
        end_time=int(now),
        request_filter=True,
        chat_ids=CHAT_IDS,
    )
    try:
        one_search('000000000000', DEFAULT_ADMIN_REGION)
        admin_status = 'reachable and authenticated (read-only)'
    except Exception as exc:
        admin_status = f'unavailable: {exc}'
    group_lines = []
    for chat_id, name in CHATS.items():
        error = (stats.get('chat_errors') or {}).get(chat_id)
        group_lines.append(f"- {name}: {'ERROR — ' + error if error else 'reachable'}")
    heartbeat = f'{heartbeat_age}s old' if heartbeat_age is not None else 'unavailable'
    watch_count = len(active_watches())
    return (
        f"Daemon: {status} (PID: {pid or 'none'})\n"
        f"Heartbeat: {heartbeat}\n"
        "Groups:\n" + '\n'.join(group_lines) + '\n'
        f"SoundOn Admin: {admin_status}\n"
        f"Active review watches: {watch_count}"
    )


def handle_command(command, chat_id, message_id):
    parts = command.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    argument = parts[1] if len(parts) > 1 else ''
    if cmd in ('/help', '/commands'):
        return reply(chat_id, message_id, 'Available commands:\n/help or /commands — list commands\n/admin <identifier> — read-only Admin lookup by UPC, ISRC, album/song/artist/user ID\n/lookup <identifier> — alias for /admin\n/testcard — force-resend the card for the latest relevant message\n/resendpending — force-resend every pending/unacted card\n/scan — send only pending cards that have not been sent before\n/pending — list pending/unacted messages from both groups\n/watches — list active Under Review monitors\n/wake or /restart — check the bot and restart it if needed\n/checkbot — health-check from the main daemon\n/diagnose — test daemon, heartbeat, both groups, and read-only Admin access\n/status — show bot status\n/legend — explain reaction meanings')
    if cmd in ('/admin', '/lookup'):
        return handle_admin_lookup(argument, chat_id, message_id)
    if cmd == '/legend':
        return reply(chat_id, message_id, 'Reaction legend:\nApproved = [Like]\nRejected = [CryCoveringMouth]\nWorking on it = [Thinking]\nDismiss = (no reaction)')
    if cmd == '/pending':
        pending, stats = fetch_unreacted_candidates(7, request_filter=True)
        if not pending:
            body = 'No pending unreacted moderation-request messages found in the last 7 days across monitored groups.'
        else:
            by_chat = {}
            for p in pending:
                by_chat.setdefault(p['chat_name'], []).append(p)
            sections = []
            for name, items in by_chat.items():
                lines = '\n'.join([f"- {p['time']} · {p['sender']}: {p['snippet']} (message_id: {p['message_id']})" for p in items[:20]])
                sections.append(f"**[{name}]** ({len(items)} pending)\n{lines}")
            body = 'Pending unreacted moderation-request messages from last 7 days:\n\n' + '\n\n'.join(sections)
        body += f"\n\nScan summary: total={stats['total_messages']}, skipped bot={stats['skipped_bot']}, skipped Filipe={stats['skipped_filipe']}, skipped non-request={stats['skipped_non_request']}, already reacted={stats['already_had_reactions']}, pending={stats['pending']}"
        return reply(chat_id, message_id, body)
    if cmd == '/watches':
        watches = active_watches()
        if not watches:
            body = 'No active Admin review watches.'
        else:
            lines = []
            for identifier, entry in list(watches.items())[:30]:
                title = entry.get('title') or 'Track'
                status = entry.get('lastStatusLabel') or entry.get('lastStatus') or 'waiting'
                lines.append(f'- {title} · `{identifier}` · {status}')
            body = f'Active Admin review watches ({len(watches)}):\n' + '\n'.join(lines)
        return reply(chat_id, message_id, body)
    if cmd in ('/scan', '/testcard', '/resendlatest', '/resendpending'):
        flags = []
        label = 'Pending-card scan'
        if cmd in ('/testcard', '/resendlatest'):
            flags = ['--latest', '--force', '--include-reacted']
            label = 'Latest relevant card test'
        elif cmd == '/resendpending':
            flags = ['--force']
            label = 'Pending/unacted card resend'
        try:
            reply(chat_id, message_id, f'{label} started for both monitored groups. I will reply when it completes.')
        except Exception as e:
            print('scan ack reply failed:', repr(e), flush=True)
        try:
            output = _run_card_sender(*flags)
            body = f'{label} completed (both groups):\n{output}'
        except Exception as exc:
            body = f'⚠️ {label} failed: {exc}'
        return reply(chat_id, message_id, body)
    if cmd in ('/checkbot', '/wake', '/restart'):
        try:
            from moderation_bot.watchdog import ensure_daemon
            status, pid = ensure_daemon()
            if status == 'ok':
                body = f'✅ Bot is healthy — persistent connection daemon is running (PID: {pid})'
            elif status == 'restarted':
                body = f'⚠️ Persistent connection daemon was down and has been restarted. New PID: {pid}. Buttons should work again!'
            else:
                body = '⚠️ Persistent connection daemon is DOWN and an automatic restart FAILED. Manual intervention needed.'
        except Exception as e:
            body = f'⚠️ {cmd} failed to run health check: {e!r}'
        return reply(chat_id, message_id, body)
    if cmd in ('/diagnose', '/selftest'):
        try:
            body = '🔎 Moderation Helper diagnostics\n' + _health_report()
        except Exception as exc:
            body = f'⚠️ Diagnostics failed: {exc!r}'
        return reply(chat_id, message_id, body)
    if cmd == '/status':
        reg = json.loads(REG.read_text()) if REG.exists() else {}
        started = START.read_text().strip() if START.exists() else 'unknown'
        groups = ', '.join([f"{n} ({c})" for c, n in CHATS.items()])
        return reply(chat_id, message_id, f'Bot status: connected\nDaemon started: {started}\nRegistered cards: {len(reg)}\nMonitored groups: {groups}')
    return reply(chat_id, message_id, 'Unknown command. Use /help')
