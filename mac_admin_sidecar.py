#!/usr/bin/env python3
"""Mac-side SoundOn Admin enrichment sidecar for the Moderation Helper bot.

Why this exists: the Aime sandbox that runs the main daemon is blocked from
sg-musician-admin.bytedance.net by ROW Operations Gateway network segregation
(HTTP 403 / code 4005). A Mac on an approved network path is not blocked. This
script does not touch the sandbox or the running daemon at all — it is a
separate, additive process that:

  1. Reads the same two monitored Lark groups the bot reads (read-only).
  2. Re-computes the SoundOn Admin context locally (works from this machine).
  3. Posts the result once per pending request, either as a private DM to the
     bot owner or as a reply under the original group message.

If this machine is off or offline, nothing breaks: the main daemon keeps
triaging, posting cards, and handling buttons exactly as it does today. This
sidecar only fills in the "Admin lookup unavailable" gap opportunistically.

State (dedupe + a short recheck window for "nothing to add yet") lives in
moderation_bot/mac_admin_sidecar_state.json, which is gitignored like the
bot's other runtime state files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so this script needs no extra dependency.

    Existing environment variables always win; values already set (e.g. by
    launchd's EnvironmentVariables) are never overridden.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")

# Imported after .env is loaded so MODERATION_HELPER_* overrides take effect.
from moderation_bot.command_handler import (  # noqa: E402
    CHATS,
    EMAIL,
    api,
    fetch_unreacted_candidates,
    reply,
    token,
)
from moderation_bot.admin_client import (  # noqa: E402
    AdminLookupError,
    approval_rejection_summary,
    track_review_summary,
)

STATE_FILE = ROOT / "moderation_bot" / "mac_admin_sidecar_state.json"
LOG_FILE = ROOT / "moderation_bot" / "mac_admin_sidecar.log"

DEFAULT_OUTPUT = (os.getenv("MAC_SIDECAR_OUTPUT") or "dm").strip().lower()
DEFAULT_LOOKBACK_DAYS = int(os.getenv("MAC_SIDECAR_LOOKBACK_DAYS", "2"))
DEFAULT_INTERVAL_SECONDS = int(os.getenv("MAC_SIDECAR_INTERVAL_SECONDS", "300"))
EMPTY_RECHECK_SECONDS = int(os.getenv("MAC_SIDECAR_EMPTY_RECHECK_SECONDS", str(6 * 3600)))
STATE_MAX_AGE_SECONDS = int(os.getenv("MAC_SIDECAR_STATE_MAX_AGE_SECONDS", str(30 * 86400)))

_BLOCKED_MARKERS = (
    "lookup unavailable",
    "network path",
    "gateway",
    "network segregation",
    "http 403",
    "http 5",
    "authentication failed",
    "authentication is unavailable",
)


def log(text: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {text}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_seconds(iso_value: str) -> float:
    try:
        then = datetime.fromisoformat(iso_value)
    except (TypeError, ValueError):
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds())


def _text_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def _looks_blocked(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _BLOCKED_MARKERS)


def _is_useful(text: str) -> bool:
    """Filter out results that would just add noise (still-blocked or no-op)."""
    if not text:
        return False
    if _looks_blocked(text):
        return False
    low = text.lower()
    if "none found" in low and "rejection reason" in low:
        return False
    return True


def admin_context_for(text: str, create_time) -> str:
    """Mirror scan_send_modbr_triage.build_card's Admin-context logic."""
    try:
        summary = track_review_summary(text, create_time)
        if not summary:
            summary = approval_rejection_summary(text)
    except AdminLookupError as exc:
        return f"⚠️ Admin lookup unavailable: {exc}"
    return summary or ""


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def save_state(state: dict) -> None:
    # Prune old entries so this file doesn't grow forever.
    pruned = {
        mid: entry
        for mid, entry in state.items()
        if _age_seconds(entry.get("checked_at", "")) < STATE_MAX_AGE_SECONDS
    }
    STATE_FILE.write_text(json.dumps(pruned, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_owner_open_id(t: str) -> str:
    res = api(
        "https://open.larksuite.com/open-apis/contact/v3/users/batch_get_id?"
        + urllib.parse.urlencode({"user_id_type": "open_id"}),
        "POST",
        {"emails": [EMAIL]},
        t,
    )
    open_id = ((res.get("data") or {}).get("user_list") or [{}])[0].get("user_id")
    if not open_id:
        raise RuntimeError(f"Could not resolve open_id for {EMAIL}")
    return open_id


def _dm(t: str, open_id: str, text: str) -> dict:
    return api(
        "https://open.larksuite.com/open-apis/im/v1/messages?"
        + urllib.parse.urlencode({"receive_id_type": "open_id"}),
        "POST",
        {"receive_id": open_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        t,
    )


def _format_dm(chat_name: str, mid: str, sender: str, context: str) -> str:
    return (
        "📎 Admin context for a pending moderation request (added by Mac sidecar)\n"
        f"Group: {chat_name}\n"
        f"From: {sender}\n"
        f"Message ID: {mid}\n\n"
        f"{context}"
    )


def _format_group_reply(context: str) -> str:
    return f"📎 SoundOn Admin context (added by Mac sidecar):\n{context}"


def run_once(lookback_days: int = DEFAULT_LOOKBACK_DAYS, output: str = DEFAULT_OUTPUT, dry_run: bool = False) -> dict:
    output = (output or "dm").strip().lower()
    if output not in ("dm", "group"):
        raise ValueError(f"Unsupported output mode: {output!r} (expected 'dm' or 'group')")

    candidates, stats = fetch_unreacted_candidates(days=lookback_days, request_filter=True)
    state = load_state()
    t = token()
    owner_open_id = None
    if output == "dm" and not dry_run:
        owner_open_id = _resolve_owner_open_id(t)

    posted = 0
    skipped_cached = 0
    skipped_blocked = 0
    skipped_no_context = 0
    errors = 0

    for item in candidates:
        mid = item["message_id"]
        txt = item.get("text") or ""
        h = _text_hash(txt)
        entry = state.get(mid)

        if entry and entry.get("hash") == h:
            if entry.get("result") == "posted":
                skipped_cached += 1
                continue
            if entry.get("result") == "empty" and _age_seconds(entry.get("checked_at", "")) < EMPTY_RECHECK_SECONDS:
                skipped_cached += 1
                continue

        try:
            context = admin_context_for(txt, item["message"].get("create_time"))
        except Exception as exc:  # noqa: BLE001 - keep the loop going for other candidates
            errors += 1
            log(f"admin_context_for failed for {mid}: {exc!r}")
            continue

        if _looks_blocked(context):
            skipped_blocked += 1
            continue  # transient — leave state alone so we retry next run

        if not _is_useful(context):
            state[mid] = {"hash": h, "result": "empty", "checked_at": _now_iso()}
            skipped_no_context += 1
            continue

        if dry_run:
            log(f"[dry-run] would post ({output}) for {mid}:\n{context}")
            posted += 1
            continue

        try:
            if output == "dm":
                text_out = _format_dm(item.get("chat_name") or "unknown", mid, item.get("sender") or "unknown", context)
                _dm(t, owner_open_id, text_out)
            else:
                reply(item["chat_id"], mid, _format_group_reply(context))
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log(f"post failed for {mid}: {exc!r}")
            continue

        state[mid] = {"hash": h, "result": "posted", "checked_at": _now_iso(), "output": output}
        posted += 1
        time.sleep(0.3)

    if not dry_run:
        save_state(state)

    summary = {
        "candidates": len(candidates),
        "posted": posted,
        "skipped_cached": skipped_cached,
        "skipped_still_blocked": skipped_blocked,
        "skipped_no_context": skipped_no_context,
        "errors": errors,
        "output_mode": output,
        "dry_run": dry_run,
        "groups": CHATS,
    }
    log(json.dumps(summary, ensure_ascii=False))
    return summary


def run_daemon(interval: int, lookback_days: int, output: str) -> None:
    log(f"mac_admin_sidecar daemon starting: interval={interval}s output={output}")
    while True:
        try:
            run_once(lookback_days=lookback_days, output=output)
        except Exception as exc:  # noqa: BLE001
            log(f"run_once error: {exc!r}\n{traceback.format_exc()}")
        time.sleep(max(30, interval))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daemon", action="store_true", help="loop forever instead of running once (launchd users should use --once instead)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help="seconds between runs in --daemon mode")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="how many days back to scan for pending requests")
    parser.add_argument("--output", choices=("dm", "group"), default=DEFAULT_OUTPUT, help="post as a private DM to the owner, or as a reply in the group")
    parser.add_argument("--dry-run", action="store_true", help="compute and log results but do not post or update state")
    args = parser.parse_args()

    if args.daemon:
        run_daemon(interval=args.interval, lookback_days=args.lookback_days, output=args.output)
    else:
        print(json.dumps(run_once(lookback_days=args.lookback_days, output=args.output, dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
