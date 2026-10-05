#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

load_env_file() {
  local file="$1"
  local line key value
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
      export\ *) line="${line#export }" ;;
    esac
    case "$line" in
      *=*) ;;
      *) continue ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      ''|*[!A-Za-z0-9_]*) continue ;;
      [0-9]*) continue ;;
    esac
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    export "$key=$value"
  done < "$file"
}

load_env_file .env

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
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "$SUPERVISOR_LOG"
}

pid_alive() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

pid_matches() {
  local pid="$1"
  local script="$2"
  [ -n "$pid" ] || return 1
  if [ ! -r "/proc/$pid/cmdline" ]; then
    # macOS and some minimal environments do not expose /proc. In that case,
    # keep the supervisor conservative and fall back to the old liveness check
    # so an otherwise healthy process does not get restarted in a loop.
    pid_alive "$pid"
    return $?
  fi
  python3 - "$pid" "$script" <<'PY'
import sys
from pathlib import Path
pid, script = sys.argv[1], sys.argv[2]
try:
    argv = [part.decode('utf-8', 'ignore') for part in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0') if part]
except Exception:
    sys.exit(1)
if not argv or 'python' not in Path(argv[0]).name.lower() and 'bash' not in Path(argv[0]).name.lower():
    sys.exit(1)
needle = script.replace('\\', '/')
for arg in argv[1:]:
    normalized = arg.replace('\\', '/')
    if normalized == needle or normalized.endswith('/' + needle):
        sys.exit(0)
sys.exit(1)
PY
}

pid_from_file() {
  local file="$1"
  [ -f "$file" ] && tr -dc '0-9' < "$file"
}

cleanup_stale_pidfile() {
  local name="$1"
  local script="$2"
  local pidfile="$3"
  local pid
  pid="$(pid_from_file "$pidfile")"
  if [ -n "$pid" ] && ! pid_matches "$pid" "$script"; then
    log "removing stale $name pidfile ($pidfile pid=$pid)"
    rm -f "$pidfile"
  fi
}

find_existing_pid() {
  local script="$1"
  local candidate
  if ! command -v pgrep >/dev/null 2>&1; then
    return 1
  fi
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    [ "$candidate" != "$$" ] || continue
    if pid_matches "$candidate" "$script"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done <<EOF
$(pgrep -f "$script" 2>/dev/null || true)
EOF
  return 1
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
  local script="$5"
  local pid
  cleanup_stale_pidfile "$name" "$script" "$pidfile"
  pid="$(pid_from_file "$pidfile")"
  if pid_matches "$pid" "$script"; then
    return 0
  fi
  pid="$(find_existing_pid "$script")"
  if [ -n "$pid" ]; then
    log "found existing $name process (pid=$pid); refreshing $pidfile"
    printf '%s\n' "$pid" > "$pidfile"
    return 0
  fi
  start_process "$name" "$cmd" "$pidfile" "$logfile"
}

cleanup_stale_pidfile "supervisor" "moderation_bot/supervisor.sh" "$SUPERVISOR_PIDFILE"

while true; do
  ensure_process "main" "$MAIN_CMD" "$MAIN_PIDFILE" "$MAIN_LOG" "moderation_bot/persistent_callback.py"
  ensure_process "watchdog-listener" "$WATCHDOG_CMD" "$WATCHDOG_PIDFILE" "$WATCHDOG_LOG" "moderation_bot/watchdog_listener.py"

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
