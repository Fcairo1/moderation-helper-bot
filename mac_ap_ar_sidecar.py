#!/usr/bin/env python3
"""Mac-side AP/AR takedown alert companion for the Moderation Helper bot.

This is intentionally separate from the sandbox daemon. It runs on a Mac that
can reach SoundOn Admin, polls the AP / AR Takedown Alert Bot chat, extracts BR
UPCs from "New Deleted AP/AR Releases" cards, resolves takedown history through
Admin, and sends Filipe a table-style PM card.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _load_dotenv(path: Path) -> None:
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

from moderation_bot.command_handler import EMAIL, api, token  # noqa: E402
from moderation_bot import config as moderation_config  # noqa: E402
from moderation_bot.admin_client import AdminLookupError, audit_history, lookup  # noqa: E402

BRT = ZoneInfo("America/Sao_Paulo")
STATE_FILE = ROOT / "moderation_bot/mac_ap_ar_sidecar_state.json"
LOG_FILE = ROOT / "moderation_bot/mac_ap_ar_sidecar.log"
DEFAULT_INTERVAL_SECONDS = max(60, int(os.getenv("MODERATION_HELPER_AP_AR_SIDECAR_INTERVAL_SECONDS", "300")))
DEFAULT_PAGE_SIZE = max(5, min(50, int(os.getenv("MODERATION_HELPER_AP_AR_SIDECAR_PAGE_SIZE", "20"))))
DEFAULT_ALERT_CHAT_ID = getattr(
    moderation_config,
    "takedown_alert_chat_id",
    lambda: os.getenv("MODERATION_HELPER_TAKEDOWN_ALERT_CHAT_ID", "oc_924bed04f6dce9a994fbdfd350a50844"),
)()


def log(text: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {text}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_lark_cli_json(args: list[str]) -> dict:
    proc = subprocess.run(
        ["lark-cli"] + args,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "lark-cli failed").strip())
    payload = json.loads(proc.stdout)
    if not payload.get("ok"):
        raise RuntimeError(json.dumps(payload.get("error") or payload, ensure_ascii=False))
    return payload


def _fetch_recent_alert_messages(chat_id: str, page_size: int) -> list[dict]:
    payload = _run_lark_cli_json(
        [
            "im",
            "+chat-messages-list",
            "--chat-id",
            chat_id,
            "--sort",
            "desc",
            "--page-size",
            str(page_size),
            "--format",
            "json",
            "--as",
            "user",
        ]
    )
    return ((payload.get("data") or {}).get("messages") or [])


def _is_takedown_alert_text(text: str) -> bool:
    return "New Deleted AP/AR Releases" in (text or "") and "### BR" in (text or "")


def _extract_br_upcs(text: str) -> list[str]:
    if not _is_takedown_alert_text(text):
        return []
    start = text.find("### BR")
    if start < 0:
        return []
    end_candidates = [value for value in (text.find("### SPLA", start), text.find("### US", start)) if value >= 0]
    end = min(end_candidates) if end_candidates else len(text)
    section = text[start:end]
    seen = set()
    upcs = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        parts = [part.strip() for part in line.split("|")[1:-1]]
        if len(parts) < 2:
            continue
        upc = parts[0]
        market = parts[1]
        if upc.isdigit() and 12 <= len(upc) <= 14 and market == "BR" and upc not in seen:
            seen.add(upc)
            upcs.append(upc)
    return upcs


def _audit_extra(audit: dict) -> dict:
    extra = audit.get("extra")
    if isinstance(extra, dict):
        return extra
    if not extra:
        return {}
    try:
        payload = json.loads(extra)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _audit_time(audit: dict) -> float:
    for key in ("updateTime", "updatedAt", "createTime", "createdAt", "auditTime", "operationTime"):
        value = audit.get(key)
        if value in (None, ""):
            continue
        try:
            number = float(value)
            return number / 1000 if number > 10_000_000_000 else number
        except (TypeError, ValueError):
            continue
    return 0.0


def _latest_takedown_context(record: dict, region: str = "BR") -> dict:
    album_id = str(record.get("albumId") or record.get("objectId") or "")
    if not album_id:
        return {}
    audits = []
    for audit in (audit_history(1, album_id, region).get("auditList") or []):
        item = dict(audit)
        item["time"] = _audit_time(audit)
        item["extraParsed"] = _audit_extra(audit)
        audits.append(item)
    takedowns = [item for item in audits if str(item.get("auditType")) == "6"]
    if not takedowns:
        return {}
    takedown = max(takedowns, key=lambda item: item["time"])
    context = {
        "time": takedown["time"],
        "operator": str(takedown.get("operator") or "").strip(),
        "reason": str(takedown["extraParsed"].get("reason") or "").strip(),
        "notes": str(takedown["extraParsed"].get("notes") or "").strip(),
        "stores": takedown["extraParsed"].get("stores") or [],
    }
    if context["operator"].lower() != "system":
        return context
    claim_candidates = []
    for audit in audits:
        if audit["time"] >= takedown["time"]:
            continue
        if str(audit.get("auditType")) != "15":
            continue
        reason = str(audit["extraParsed"].get("reason") or "").strip()
        if not reason:
            continue
        if takedown["time"] - audit["time"] > 60:
            continue
        claim_candidates.append(audit)
    if not claim_candidates:
        if not context["notes"]:
            context["notes"] = "Infringement claim"
        return context
    claim = max(claim_candidates, key=lambda item: item["time"])
    claim_reason = str(claim["extraParsed"].get("reason") or "").strip()
    claim_operator = str(claim.get("operator") or "").strip()
    context["operator"] = claim_operator or context["operator"]
    context["reason"] = claim_reason or context["reason"]
    context["notes"] = context["notes"] or "Infringement claim"
    return context


def _format_brt_timestamp(timestamp_value: float) -> str:
    if not timestamp_value:
        return "—"
    return datetime.fromtimestamp(float(timestamp_value), tz=timezone.utc).astimezone(BRT).strftime("%d %b %Y %H:%M")


def _lookup_takedown_row(upc: str) -> dict:
    result = lookup(upc, "BR")
    if not result.get("found"):
        return {"upc": upc, "title": "Not found in Admin", "removed_by": "—", "date_brt": "—", "reason": "—", "notes": "Not found in Admin", "platforms": "—"}
    record = result.get("record") or {}
    context = _latest_takedown_context(record, "BR")
    if not context:
        return {"upc": upc, "title": str(record.get("title") or "—"), "removed_by": "—", "date_brt": "—", "reason": "—", "notes": "No takedown operation found", "platforms": "—"}
    stores = context.get("stores") or []
    return {
        "upc": upc,
        "title": str(record.get("title") or "—"),
        "removed_by": str(context.get("operator") or "—"),
        "date_brt": _format_brt_timestamp(context.get("time") or 0),
        "reason": str(context.get("reason") or "—"),
        "notes": str(context.get("notes") or "—"),
        "platforms": str(len(stores)) if stores else "—",
    }


def _build_card(rows: list[dict], checked_at: str, source_message_id: str) -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "fill"},
        "header": {
            "title": {"tag": "plain_text", "content": "BR Takedown Ops"},
            "subtitle": {"tag": "plain_text", "content": f"AP/AR Takedown Alert Bot · {checked_at}"},
            "template": "grey",
            "icon": {"tag": "standard_icon", "token": "table_colorful"},
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "elements": [
                {"tag": "markdown", "content": f"Source message: {source_message_id}\nRows: {len(rows)}\nLogic applied: system takedowns are attributed to the preceding Content Safety Claim Status Update when present."},
                {
                    "tag": "table",
                    "freeze_first_column": True,
                    "page_size": min(max(len(rows), 1), 10),
                    "row_height": "middle",
                    "header_style": {"text_align": "left", "background_style": "grey", "bold": True, "lines": 1},
                    "columns": [
                        {"name": "upc", "display_name": "UPC", "data_type": "text", "width": "140px"},
                        {"name": "title", "display_name": "Album Title", "data_type": "text", "width": "170px"},
                        {"name": "removed_by", "display_name": "Removed By", "data_type": "text", "width": "110px"},
                        {"name": "date_brt", "display_name": "Date (BRT)", "data_type": "text", "width": "140px"},
                        {"name": "reason", "display_name": "Reason", "data_type": "text", "width": "220px"},
                        {"name": "notes", "display_name": "Notes", "data_type": "text", "width": "190px"},
                        {"name": "platforms", "display_name": "Platforms", "data_type": "text", "width": "90px"},
                    ],
                    "rows": rows,
                },
                {"tag": "markdown", "text_size": "caption", "content": '<font color="grey">PM only. Brazil time (BRT) used for all timestamps.</font>'},
            ],
        },
    }


def _resolve_owner_open_id(t: str) -> str:
    data = api(
        "https://open.larksuite.com/open-apis/contact/v3/users/batch_get_id?" + urllib.parse.urlencode({"user_id_type": "open_id"}),
        "POST",
        {"emails": [EMAIL]},
        t,
    )
    user_list = ((data.get("data") or {}).get("user_list") or [])
    open_id = user_list[0].get("user_id") if user_list else ""
    if not open_id:
        raise RuntimeError(f"Could not resolve owner open_id for {EMAIL}")
    return open_id


def _send_dm_card(t: str, open_id: str, card: dict) -> dict:
    return api(
        "https://open.larksuite.com/open-apis/im/v1/messages?" + urllib.parse.urlencode({"receive_id_type": "open_id"}),
        "POST",
        {"receive_id": open_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        t,
    )


def run_once(chat_id: str = DEFAULT_ALERT_CHAT_ID, page_size: int = DEFAULT_PAGE_SIZE, dry_run: bool = False) -> dict:
    messages = _fetch_recent_alert_messages(chat_id, page_size)
    state = _load_state()
    t = token()
    owner_open_id = None if dry_run else _resolve_owner_open_id(t)

    processed = 0
    posted = 0
    skipped_cached = 0
    skipped_non_alert = 0
    errors = 0

    for message in reversed(messages):
        message_id = str(message.get("message_id") or "")
        rendered = str(message.get("content") or "")
        if not _is_takedown_alert_text(rendered):
            skipped_non_alert += 1
            continue
        digest = _text_hash(rendered)
        if state.get(message_id, {}).get("hash") == digest and state.get(message_id, {}).get("result") == "posted":
            skipped_cached += 1
            continue
        upcs = _extract_br_upcs(rendered)
        if not upcs:
            state[message_id] = {"hash": digest, "result": "empty", "checked_at": _now_iso()}
            continue
        try:
            rows = [_lookup_takedown_row(upc) for upc in upcs]
        except AdminLookupError as exc:
            errors += 1
            log(f"Admin lookup blocked for {message_id}: {exc}")
            continue
        except Exception as exc:
            errors += 1
            log(f"Unexpected lookup error for {message_id}: {exc!r}")
            traceback.print_exc()
            continue
        checked_at = datetime.now(BRT).strftime("%d %b %Y %H:%M BRT")
        card = _build_card(rows, checked_at, message_id)
        processed += 1
        if dry_run:
            log(f"[dry-run] would post AP/AR card for {message_id} with {len(rows)} rows")
            continue
        result = _send_dm_card(t, owner_open_id, card)
        state[message_id] = {
            "hash": digest,
            "result": "posted",
            "checked_at": _now_iso(),
            "card_message_id": ((result.get("data") or {}).get("message_id") or ""),
            "row_count": len(rows),
            "upcs": upcs,
        }
        posted += 1
        time.sleep(0.3)

    if not dry_run:
        _save_state(state)

    summary = {
        "chat_id": chat_id,
        "messages_scanned": len(messages),
        "processed": processed,
        "posted": posted,
        "skipped_cached": skipped_cached,
        "skipped_non_alert": skipped_non_alert,
        "errors": errors,
        "dry_run": dry_run,
    }
    log(json.dumps(summary, ensure_ascii=False))
    return summary


def run_daemon(interval: int, chat_id: str, page_size: int) -> None:
    while True:
        try:
            run_once(chat_id=chat_id, page_size=page_size, dry_run=False)
        except Exception as exc:
            log(f"daemon iteration failed: {exc!r}")
            traceback.print_exc()
        time.sleep(max(60, interval))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daemon", action="store_true", help="loop forever instead of running once (launchd users should omit this — the plist calls the script once per interval instead)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help="seconds between runs in --daemon mode")
    parser.add_argument("--chat-id", default=DEFAULT_ALERT_CHAT_ID, help="AP/AR Takedown Alert Bot group chat_id")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="how many recent messages to inspect each run")
    parser.add_argument("--dry-run", action="store_true", help="compute and log results but do not send the PM card")
    args = parser.parse_args()

    if args.daemon:
        run_daemon(interval=args.interval, chat_id=args.chat_id, page_size=args.page_size)
    else:
        print(json.dumps(run_once(chat_id=args.chat_id, page_size=args.page_size, dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
