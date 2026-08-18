#!/usr/bin/env python3
"""Health-check helpers for the moderation bot persistent-connection daemon."""
import json
import os
import subprocess
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from moderation_bot.command_handler import EMAIL, api, token
from moderation_bot.config import app_id

ROOT = Path(__file__).resolve().parents[1]
TARGET_SCRIPT = ROOT / 'moderation_bot/persistent_callback.py'
TARGET_PIDFILE = ROOT / 'moderation_bot/persistent_callback.pid'
WATCHDOG_LOG = ROOT / 'moderation_bot/watchdog.log'
BOT_APP_ID = app_id()


def log_line(text):
    ts = datetime.now().isoformat(timespec='seconds')
    try:
        with WATCHDOG_LOG.open('a', encoding='utf-8') as f:
            f.write(f'{ts} {text}\n')
    except Exception:
        pass
    print(f'{ts} {text}', flush=True)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_target_process(pid):
    if not _pid_alive(pid):
        return False
    try:
        raw = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\x00')
        args = [x.decode('utf-8', 'ignore') for x in raw if x]
        return any(str(TARGET_SCRIPT) == a or a.endswith('moderation_bot/persistent_callback.py') for a in args)
    except Exception:
        return True


def daemon_running():
    if TARGET_PIDFILE.exists():
        raw = TARGET_PIDFILE.read_text(encoding='utf-8').strip()
        if raw.isdigit():
            pid = int(raw)
            if _pid_alive(pid) and _is_target_process(pid):
                return pid
    try:
        out = subprocess.check_output(['pgrep', '-f', 'moderation_bot/persistent_callback.py'], text=True).split()
        for raw in out:
            if raw.isdigit() and _is_target_process(int(raw)):
                pid = int(raw)
                TARGET_PIDFILE.write_text(str(pid), encoding='utf-8')
                return pid
    except subprocess.CalledProcessError:
        pass
    except Exception as e:
        log_line(f'pgrep fallback error: {e!r}')
    return None


def send_dm(text):
    try:
        t = token()
        r = api('https://open.larksuite.com/open-apis/contact/v3/users/batch_get_id?' + urllib.parse.urlencode({'user_id_type': 'open_id'}), 'POST', {'emails': [EMAIL]}, t)
        rec = ((r.get('data') or {}).get('user_list') or [{}])[0].get('user_id')
        if not rec:
            log_line(f'DM skipped: could not resolve open_id for {EMAIL}')
            return False
        api('https://open.larksuite.com/open-apis/im/v1/messages?' + urllib.parse.urlencode({'receive_id_type': 'open_id'}), 'POST', {'receive_id': rec, 'msg_type': 'text', 'content': json.dumps({'text': text}, ensure_ascii=False)}, t)
        return True
    except Exception as e:
        log_line(f'DM send error: {e!r}')
        return False


def restart_target():
    cmd = f'cd {ROOT} && nohup python3 {TARGET_SCRIPT} >> {ROOT / "moderation_bot/persistent_callback.log"} 2>&1 &'
    subprocess.Popen(['bash', '-c', cmd], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    new_pid = None
    for _ in range(15):
        time.sleep(1)
        new_pid = daemon_running()
        if new_pid:
            break
    return new_pid


def ensure_daemon():
    pid = daemon_running()
    if pid:
        return 'ok', pid
    log_line('persistent_callback daemon is down; attempting restart')
    new_pid = restart_target()
    if new_pid:
        log_line(f'persistent_callback daemon restarted, pid={new_pid}')
        return 'restarted', new_pid
    log_line('persistent_callback daemon restart failed')
    return 'restart_failed', None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='check once and print JSON')
    args = parser.parse_args()
    status, pid = ensure_daemon()
    print(json.dumps({'status': status, 'pid': pid, 'app_id': BOT_APP_ID}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
