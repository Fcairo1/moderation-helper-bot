#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

MAIN_PIDFILE="moderation_bot/persistent_callback.pid"
WATCHDOG_PIDFILE="moderation_bot/watchdog_listener.pid"
SUPERVISOR_PIDFILE="moderation_bot/supervisor.pid"
MAIN_LOG="moderation_bot/persistent_callback.log"
WATCHDOG_LOG="moderation_bot/watchdog_listener.log"
SUPERVISOR_LOG="moderation_bot/supervisor.log"
HEARTBEAT_FILE="moderation_bot/heartbeat"
HEARTBEAT_STALE_SECONDS=180
HEARTBEAT_GRACE_SECONDS=120
MAIN_CMD="python3 moderation_bot/persistent_callback.py"
WATCHDOG_CMD="python3 moderation_bot/watchdog_listener.py"

mkdir -p moderation_bot
printf '%s\n' "$$" > "$SUPERVISOR_PIDFILE"

log() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$SUPERVISOR_LOG"
}

pid_alive() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

pid_from_file() {
  local file="$1"
  [ -f "$file" ] && tr -dc '0-9' < "$file"
}

start_process() {
  local name="$1"
  local cmd="$2"
  local pidfile="$3"
  local logfile="$4"
  log "starting $name"
  nohup bash -c "$cmd" >> "$logfile" 2>&1 &
  printf '%s\n' "$!" > "$pidfile"
}

ensure_process() {
  local name="$1"
  local cmd="$2"
  local pidfile="$3"
  local logfile="$4"
  local pid
  pid="$(pid_from_file "$pidfile")"
  if pid_alive "$pid"; then
    return 0
  fi
  start_process "$name" "$cmd" "$pidfile" "$logfile"
}

while true; do
  ensure_process "main" "$MAIN_CMD" "$MAIN_PIDFILE" "$MAIN_LOG"
  ensure_process "watchdog-listener" "$WATCHDOG_CMD" "$WATCHDOG_PIDFILE" "$WATCHDOG_LOG"

  if [ -f "$HEARTBEAT_FILE" ]; then
    now=$(date +%s)
    hb=$(python3 - <<'PY'
from pathlib import Path
try:
    print(int(float(Path('moderation_bot/heartbeat').read_text().strip())))
except Exception:
    print(0)
PY
)
    age=$((now - hb))
    if [ "$age" -gt "$HEARTBEAT_STALE_SECONDS" ]; then
      log "heartbeat stale (${age}s); restarting main daemon"
      pid="$(pid_from_file "$MAIN_PIDFILE")"
      if pid_alive "$pid"; then kill "$pid" >/dev/null 2>&1 || true; fi
      sleep "$HEARTBEAT_GRACE_SECONDS"
      start_process "main" "$MAIN_CMD" "$MAIN_PIDFILE" "$MAIN_LOG"
    fi
  fi
  sleep 30
done
