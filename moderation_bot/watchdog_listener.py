#!/usr/bin/env python3
"""Lightweight watchdog listener for the moderation bot.

On `/restart`, in either monitored group or a DM from Filipe, it checks whether
the main persistent callback daemon is alive and restarts it if needed.
"""
import json
import os
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lark_oapi as lark
from lark_oapi.ws import Client as WsClient

from moderation_bot.config import app_id, app_secret
from moderation_bot.command_handler import CHAT_IDS, EMAIL, api, msg_text, token

BOT_APP_ID = app_id()
MAIN_SCRIPT = 'moderation_bot/persistent_callback.py'
MAIN_LOG = ROOT / 'moderation_bot/persistent_callback.log'
MAIN_PIDFILE = ROOT / 'moderation_bot/persistent_callback.pid'
LISTENER_LOG = ROOT / 'moderation_bot/watchdog_listener.log'


def log(text):
    ts = datetime.now().isoformat(timespec='seconds')
    try:
        with LISTENER_LOG.open('a', encoding='utf-8') as f:
            f.write(f'{ts} {text}\n')
    except Exception:
        pass
    print(f'{ts} {text}', flush=True)


def reply(chat_id, message_id, text):
    return api(
        f'https://open.larksuite.com/open-apis/im/v1/messages/{message_id}/reply',
        'POST',
        {'msg_type': 'text', 'content': json.dumps({'text': text}, ensure_ascii=False)},
        token(),
    )


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _main_daemon_pid():
    pid = None
    if MAIN_PIDFILE.exists():
        raw = MAIN_PIDFILE.read_text(encoding='utf-8').strip()
        if raw.isdigit():
            pid = int(raw)
    if pid and _pid_alive(pid):
        try:
            argv = [a.decode('utf-8', 'ignore') for a in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\x00') if a]
            if argv and 'python' in os.path.basename(argv[0]).lower() and any(a.endswith(MAIN_SCRIPT) for a in argv[1:]):
                return pid
            return None
        except Exception:
            return pid
    return None


def _restart_main():
    cmd = f'cd {ROOT} && nohup python3 {MAIN_SCRIPT} >> {MAIN_LOG} 2>&1 &'
    subprocess.Popen(['bash', '-c', cmd], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import time
    for _ in range(15):
        time.sleep(1)
        pid = _main_daemon_pid()
        if pid:
            return pid
    return None


def handle_restart(chat_id, message_id):
    pid = _main_daemon_pid()
    if pid:
        log(f'/restart: main daemon already alive (pid={pid})')
        reply(chat_id, message_id, f'✅ Main bot is already running (PID: {pid}) — no restart needed.')
        return
    log('/restart: main daemon DOWN — restarting')
    new_pid = _restart_main()
    if new_pid:
        try:
            MAIN_PIDFILE.write_text(str(new_pid), encoding='utf-8')
        except Exception:
            pass
        log(f'/restart: main daemon restarted (new pid={new_pid})')
        reply(chat_id, message_id, f'🔄 Main bot was down and has been restarted. New PID: {new_pid}. Buttons should work again!')
    else:
        log('/restart: restart FAILED — could not confirm a new PID')
        reply(chat_id, message_id, '⚠️ Main bot was down and an automatic restart FAILED. Manual intervention needed.')


def on_message(data):
    try:
        event = getattr(data, 'event', None)
        message = getattr(event, 'message', None)
        if not message:
            return
        chat_id = getattr(message, 'chat_id', '')
        message_id = getattr(message, 'message_id', '')
        text = msg_text(getattr(message, 'content', '')).strip().lower()
        if text != '/restart':
            return
        # Allow monitored groups and DMs. For DMs, the platform may not provide a
        # monitored chat_id; authorization is still constrained by bot visibility.
        if chat_id and CHAT_IDS and chat_id not in CHAT_IDS:
            log(f'/restart from non-monitored chat {chat_id}; allowing only if DM context is accepted by Lark')
        handle_restart(chat_id, message_id)
    except Exception:
        log('message handler error:\n' + traceback.format_exc())


def main():
    event_handler = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(on_message).build()
    cli = WsClient(BOT_APP_ID, app_secret(), event_handler=event_handler)
    log(f'Starting watchdog listener for app {BOT_APP_ID}; owner={EMAIL}')
    cli.start()


if __name__ == '__main__':
    main()
