#!/usr/bin/env python3
"""Daily pending digest for the Lark moderation bot."""
import argparse
import datetime
import json
import sys
import time
import traceback
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from moderation_bot.command_handler import EMAIL, api, fetch_unreacted_candidates, token
from moderation_bot.watchdog import ensure_daemon
from scan_send_modbr_triage import build_card

REG = ROOT / 'moderation_bot/card_registry.json'
CURRENT = ROOT / 'moderation_bot/current_card_state.json'
LOG = ROOT / 'moderation_bot/daily_digest.log'
PIDFILE = ROOT / 'moderation_bot/daily_digest.pid'


def log(text):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {text}"
    print(line, flush=True)
    try:
        with LOG.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def resolve_owner(t):
    r = api('https://open.larksuite.com/open-apis/contact/v3/users/batch_get_id?' + urllib.parse.urlencode({'user_id_type': 'open_id'}), 'POST', {'emails': [EMAIL]}, t)
    rec = ((r.get('data') or {}).get('user_list') or [{}])[0].get('user_id')
    if not rec:
        raise RuntimeError(f'Could not resolve open_id for {EMAIL}')
    return rec


def send_pending_digest(days=7):
    status, pid = ensure_daemon()
    log(f'watchdog status={status} pid={pid}')
    t = token()
    receiver = resolve_owner(t)
    pending, stats = fetch_unreacted_candidates(days=days, request_filter=True)
    reg = json.loads(REG.read_text()) if REG.exists() else {}
    cur = json.loads(CURRENT.read_text()) if CURRENT.exists() else {}
    sent = 0
    skipped = 0
    for item in pending:
        mid = item['message_id']
        if mid in reg:
            skipped += 1
            continue
        card = build_card(item['message'], item['sender'], item.get('chat_name'))
        res = api('https://open.larksuite.com/open-apis/im/v1/messages?' + urllib.parse.urlencode({'receive_id_type': 'open_id'}), 'POST', {'receive_id': receiver, 'msg_type': 'interactive', 'content': json.dumps(card, ensure_ascii=False)}, t)
        cmid = (res.get('data') or {}).get('message_id')
        reg[mid] = cmid
        cur[cmid] = card
        sent += 1
        time.sleep(0.2)
    REG.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding='utf-8')
    CURRENT.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {'sent': sent, 'skipped_existing': skipped, 'stats': stats, 'watchdog_status': status, 'watchdog_pid': pid}
    log(json.dumps(summary, ensure_ascii=False))
    return summary


def seconds_until_next_9am_brt():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    brt = datetime.timezone(datetime.timedelta(hours=-3))
    now = now_utc.astimezone(brt)
    target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return max(1, int((target - now).total_seconds()))


def run_daemon(days=7):
    PIDFILE.write_text(str(os.getpid()), encoding='utf-8')
    while True:
        try:
            send_pending_digest(days=days)
        except Exception as e:
            log(f'daily digest error: {e!r}\n{traceback.format_exc()}')
        sleep_for = seconds_until_next_9am_brt()
        log(f'next digest in {sleep_for} seconds')
        time.sleep(sleep_for)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--daemon', action='store_true')
    args = parser.parse_args()
    if args.daemon:
        run_daemon(days=args.days)
    else:
        print(json.dumps(send_pending_digest(days=args.days), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    import os
    main()
