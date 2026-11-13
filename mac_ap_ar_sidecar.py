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
from datetime import date, datetime, timezone
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
RELEASE_STATUS_TAKEDOWN = "60"  # Admin releaseStatus 60 == "Takedown"
STATE_FILE = ROOT / "moderation_bot/mac_ap_ar_sidecar_state.json"
LOG_FILE = ROOT / "moderation_bot/mac_ap_ar_sidecar.log"
DEFAULT_INTERVAL_SECONDS = max(60, int(os.getenv("MODERATION_HELPER_AP_AR_SIDECAR_INTERVAL_SECONDS", "300")))
DEFAULT_PAGE_SIZE = max(5, min(50, int(os.getenv("MODERATION_HELPER_AP_AR_SIDECAR_PAGE_SIZE", "20"))))
DEFAULT_ALERT_CHAT_ID = getattr(
    moderation_config,
    "takedown_alert_chat_id",
    lambda: os.getenv("MODERATION_HELPER_TAKEDOWN_ALERT_CHAT_ID", "oc_924bed04f6dce9a994fbdfd350a50844"),
)()
OWNER_RECIPIENT_KEY = "owner"
TEMP_AP_AR_RECIPIENT_KEY = "temp_ap_ar_recipient"
STATE_META_KEY = "__meta__"
SECONDARY_RECIPIENT_ROLLOUT_STARTED_AT_KEY = "secondary_recipient_rollout_started_at"
TEMP_AP_AR_RECIPIENT_EMAIL = "bernardo.sanchez@bytedance.com"
TEMP_AP_AR_RECIPIENT_NAME = "Bernardo Sanchez"
# Temporary AP/AR recipient is allowed through 30 Nov 2026 BRT (inclusive).
TEMP_AP_AR_RECIPIENT_CUTOFF_DATE_BRT = date(2026, 11, 30)


def log(text: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {text}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def _current_brt_datetime() -> datetime:
    return datetime.now(BRT)


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
    # The release status is authoritative: if Admin says the album is in
    # Takedown status it IS taken down, even when no type-6 takedown entry is
    # found in the audit log (e.g. removals done by the Content Safety Center).
    status_taken_down = str(record.get("releaseStatus")) == RELEASE_STATUS_TAKEDOWN
    if not context:
        if status_taken_down:
            return {"upc": upc, "title": str(record.get("title") or "—"), "removed_by": "—", "date_brt": "—", "reason": "—", "notes": "Status is Takedown (no takedown entry in audit log)", "platforms": "—", "taken_down": True}
        log(f"no takedown evidence for {upc}: releaseStatus={record.get('releaseStatus')!r}")
        return {"upc": upc, "title": str(record.get("title") or "—"), "removed_by": "—", "date_brt": "—", "reason": "—", "notes": "No takedown operation found", "platforms": "—", "taken_down": False}
    stores = context.get("stores") or []
    return {
        "upc": upc,
        "taken_down": True,
        "title": str(record.get("title") or "—"),
        "removed_by": str(context.get("operator") or "—"),
        "date_brt": _format_brt_timestamp(context.get("time") or 0),
        "reason": str(context.get("reason") or "—"),
        "notes": str(context.get("notes") or "—"),
        "platforms": str(len(stores)) if stores else "—",
    }


def _build_card(rows: list[dict], checked_at: str, source_message_id: str) -> dict:
    card_columns = [
        "upc",
        "title",
        "removed_by",
        "date_brt",
        "reason",
        "notes",
        "platforms",
    ]
    card_rows = [{key: row.get(key, "—") for key in card_columns} for row in rows]
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
                    "rows": card_rows,
                },
                {"tag": "markdown", "text_size": "caption", "content": '<font color="grey">PM only. Brazil time (BRT) used for all timestamps.</font>'},
            ],
        },
    }


def _resolve_open_id_for_email(t: str, email: str, label: str) -> str:
    data = api(
        "https://open.larksuite.com/open-apis/contact/v3/users/batch_get_id?" + urllib.parse.urlencode({"user_id_type": "open_id"}),
        "POST",
        {"emails": [email]},
        t,
    )
    user_list = ((data.get("data") or {}).get("user_list") or [])
    open_id = user_list[0].get("user_id") if user_list else ""
    if not open_id:
        raise RuntimeError(f"Could not resolve {label} open_id for {email}")
    return open_id


def _resolve_owner_open_id(t: str) -> str:
    return _resolve_open_id_for_email(t, EMAIL, "owner")


def _resolve_temp_ap_ar_recipient_open_id(t: str) -> str:
    return _resolve_open_id_for_email(t, TEMP_AP_AR_RECIPIENT_EMAIL, TEMP_AP_AR_RECIPIENT_NAME)


def _send_dm_card(t: str, open_id: str, card: dict) -> dict:
    return api(
        "https://open.larksuite.com/open-apis/im/v1/messages?" + urllib.parse.urlencode({"receive_id_type": "open_id"}),
        "POST",
        {"receive_id": open_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
        t,
    )


def _send_dm_text(t: str, open_id: str, text: str) -> dict:
    return api(
        "https://open.larksuite.com/open-apis/im/v1/messages?" + urllib.parse.urlencode({"receive_id_type": "open_id"}),
        "POST",
        {"receive_id": open_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
        t,
    )


OTHER_TAKEDOWN_BUCKET = "🟠 Other takedown"
USER_REQUEST_BUCKET = "🔵 User Request / User Auto-processed"


def _summary_bucket(row: dict) -> str:
    # A release that was actually taken down is never a false positive, no
    # matter who removed it or what the reason text says.
    if not row.get("taken_down"):
        return "🟡 False positive"
    reason = str(row.get("reason") or "").strip().lower()
    notes = str(row.get("notes") or "").strip().lower()
    combined = f"{reason} {notes}"
    # User self-processed takedowns: Admin shows the user's UID as the operator
    # and the notes start with "Auto-processed user request: <free text>". The
    # free text is typed by the user, so check this before any keyword rule.
    if notes.startswith("auto-processed user request") or "self process" in combined.replace("_", " ").replace("-", " "):
        return USER_REQUEST_BUCKET
    # Content Safety claims (e.g. "content_safety_claim_v2") are infringement
    # claims, so they belong with Infringing content.
    removed_by = str(row.get("removed_by") or "").strip().lower()
    if "content safety claim" in combined.replace("_", " ").replace("-", " ") or "content safety claim" in removed_by.replace("_", " ").replace("-", " "):
        return "🔴 Infringing content"
    if "infringing" in combined or "infringement" in combined:
        return "🔴 Infringing content"
    if "request" in combined:
        return USER_REQUEST_BUCKET
    if reason == "confirm td":
        return "🟣 Confirmed takedown"
    return OTHER_TAKEDOWN_BUCKET


def _summary_line(bucket: str, row: dict) -> str:
    line = f"• {row.get('upc') or '-'} - {row.get('title') or '-'}"
    if bucket == OTHER_TAKEDOWN_BUCKET:
        # Show who/why so uncategorised takedowns (e.g. Content Safety) are visible.
        details = [str(row.get(key)) for key in ("reason", "removed_by") if row.get(key) not in (None, "", "—")]
        if details:
            line += f" ({' / '.join(details)})"
    return line


def _build_summary_text(rows: list[dict], checked_at: str) -> str:
    # checked_at is formatted as "08 Oct 2026 14:22 BRT"; the copy-paste
    # summary only needs the date label.
    date_label = " ".join(checked_at.split()[:3])
    buckets = {
        "🟡 False positive": [],
        USER_REQUEST_BUCKET: [],
        "🟣 Confirmed takedown": [],
        "🔴 Infringing content": [],
        OTHER_TAKEDOWN_BUCKET: [],
    }
    for row in rows:
        buckets[_summary_bucket(row)].append(row)

    parts = [f"📋 BR Takedowns – {date_label}"]
    for bucket, bucket_rows in buckets.items():
        if not bucket_rows:
            continue
        parts.append("")
        parts.append(bucket)
        parts.extend(_summary_line(bucket, row) for row in bucket_rows)
    return "\n".join(parts)


def _parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _message_timestamp_seconds(message: dict) -> float:
    for key in ("create_time", "createTime"):
        value = message.get(key)
        if value in (None, ""):
            continue
        try:
            number = float(value)
            return number / 1000 if number > 10_000_000_000 else number
        except (TypeError, ValueError):
            continue
    nested = message.get("message") or {}
    if isinstance(nested, dict):
        for key in ("create_time", "createTime"):
            value = nested.get(key)
            if value in (None, ""):
                continue
            try:
                number = float(value)
                return number / 1000 if number > 10_000_000_000 else number
            except (TypeError, ValueError):
                continue
    return 0.0


def _recipient_component_sent(component: dict | None) -> bool:
    return isinstance(component, dict) and component.get("status") == "sent"


def _normalize_recipient_component(component: dict | None, fallback_message_id: str = "") -> dict:
    if isinstance(component, dict):
        return dict(component)
    if fallback_message_id:
        return {"status": "sent", "message_id": fallback_message_id}
    return {}


def _normalize_message_state(entry: dict | None) -> dict:
    if not isinstance(entry, dict):
        return {"recipients": {}}
    normalized = dict(entry)
    recipients = normalized.get("recipients")
    if isinstance(recipients, dict):
        normalized["recipients"] = {key: dict(value) if isinstance(value, dict) else {} for key, value in recipients.items()}
        return normalized

    owner_entry = {}
    if normalized.get("result") == "posted":
        owner_entry = {
            "card": _normalize_recipient_component(normalized.get("card"), str(normalized.get("card_message_id") or "")),
            "summary": _normalize_recipient_component(normalized.get("summary"), str(normalized.get("summary_message_id") or "")),
        }
        if not owner_entry["card"]:
            owner_entry["card"] = {"status": "sent", "message_id": str(normalized.get("card_message_id") or "")}
        if not owner_entry["summary"]:
            owner_entry["summary"] = {"status": "sent", "message_id": str(normalized.get("summary_message_id") or "")}
    normalized["recipients"] = {OWNER_RECIPIENT_KEY: owner_entry} if owner_entry else {}
    return normalized


def _recipient_is_fully_sent(recipient_state: dict | None) -> bool:
    if not isinstance(recipient_state, dict):
        return False
    return _recipient_component_sent(recipient_state.get("card")) and _recipient_component_sent(recipient_state.get("summary"))


def _temp_recipient_is_active(now_brt: datetime | None = None) -> bool:
    current = now_brt or _current_brt_datetime()
    return current.date() <= TEMP_AP_AR_RECIPIENT_CUTOFF_DATE_BRT


def _message_is_new_enough_for_temp_recipient(message_timestamp: float, rollout_started_at: str) -> bool:
    if not message_timestamp:
        return False
    rollout_dt = _parse_iso_datetime(rollout_started_at)
    if rollout_dt is None:
        return False
    return message_timestamp >= rollout_dt.timestamp()


def _record_recipient_skip(recipient_state: dict, *, status: str, at: str) -> None:
    recipient_state.setdefault("status", status)
    recipient_state.setdefault("updated_at", at)


def _send_message_components(t: str, recipient_label: str, open_id: str, card: dict, summary_text: str, recipient_state: dict, message_id: str) -> bool:
    sent_any = False
    if not _recipient_component_sent(recipient_state.get("card")):
        try:
            card_result = _send_dm_card(t, open_id, card)
            recipient_state["card"] = {
                "status": "sent",
                "message_id": ((card_result.get("data") or {}).get("message_id") or ""),
                "sent_at": _now_iso(),
            }
            sent_any = True
        except Exception as exc:
            recipient_state["card"] = {
                "status": "failed",
                "message_id": "",
                "error": repr(exc),
                "updated_at": _now_iso(),
            }
            log(f"AP/AR card send failed for {message_id} -> {recipient_label}: {exc!r}")
    if not _recipient_component_sent(recipient_state.get("summary")):
        try:
            summary_result = _send_dm_text(t, open_id, summary_text)
            recipient_state["summary"] = {
                "status": "sent",
                "message_id": ((summary_result.get("data") or {}).get("message_id") or ""),
                "sent_at": _now_iso(),
            }
            sent_any = True
        except Exception as exc:
            recipient_state["summary"] = {
                "status": "failed",
                "message_id": "",
                "error": repr(exc),
                "updated_at": _now_iso(),
            }
            log(f"AP/AR summary send failed for {message_id} -> {recipient_label}: {exc!r}")
    return sent_any


def run_once(chat_id: str = DEFAULT_ALERT_CHAT_ID, page_size: int = DEFAULT_PAGE_SIZE, dry_run: bool = False) -> dict:
    messages = _fetch_recent_alert_messages(chat_id, page_size)
    state = _load_state()
    meta = state.setdefault(STATE_META_KEY, {}) if isinstance(state, dict) else {}
    rollout_started_at = str(meta.get(SECONDARY_RECIPIENT_ROLLOUT_STARTED_AT_KEY) or "")
    if not rollout_started_at:
        rollout_started_at = _now_iso()
        if not dry_run:
            meta[SECONDARY_RECIPIENT_ROLLOUT_STARTED_AT_KEY] = rollout_started_at

    t = token()
    owner_open_id = None if dry_run else _resolve_owner_open_id(t)
    now_brt = _current_brt_datetime()
    temp_recipient_active = _temp_recipient_is_active(now_brt)
    temp_recipient_open_id = None
    temp_recipient_unresolved = False
    if not dry_run and temp_recipient_active:
        # The owner's alerts must never depend on the temporary recipient: if
        # his open_id can't be resolved (wrong email, outside the bot's contact
        # scope, API error), log it and send to the owner only on this run.
        try:
            temp_recipient_open_id = _resolve_temp_ap_ar_recipient_open_id(t)
        except Exception as exc:
            temp_recipient_unresolved = True
            log(f"{TEMP_AP_AR_RECIPIENT_NAME} could not be resolved; sending to owner only this run: {exc!r}")

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
        entry = _normalize_message_state(state.get(message_id) or {})
        if entry.get("hash") != digest:
            entry = {"recipients": {}}
        recipients = entry.setdefault("recipients", {})
        owner_state = recipients.setdefault(OWNER_RECIPIENT_KEY, {})
        temp_state = recipients.setdefault(TEMP_AP_AR_RECIPIENT_KEY, {})

        message_timestamp = _message_timestamp_seconds(message)
        # While he is unresolved he is not a deliverable recipient, so a message
        # that is complete for the owner stays cached (no repeated Admin lookups).
        temp_should_receive = (
            temp_recipient_active
            and not temp_recipient_unresolved
            and _message_is_new_enough_for_temp_recipient(message_timestamp, rollout_started_at)
        )
        if not temp_recipient_active:
            _record_recipient_skip(temp_state, status="expired", at=_now_iso())
        elif not temp_recipient_unresolved and not temp_should_receive:
            _record_recipient_skip(temp_state, status="pre_rollout", at=_now_iso())

        if entry.get("hash") == digest and _recipient_is_fully_sent(owner_state) and (not temp_should_receive or _recipient_is_fully_sent(temp_state)):
            skipped_cached += 1
            state[message_id] = entry
            continue

        upcs = _extract_br_upcs(rendered)
        if not upcs:
            entry.update({"hash": digest, "result": "empty", "checked_at": _now_iso()})
            state[message_id] = entry
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

        checked_at = now_brt.strftime("%d %b %Y %H:%M BRT")
        card = _build_card(rows, checked_at, message_id)
        summary_text = _build_summary_text(rows, checked_at)
        processed += 1
        entry.update(
            {
                "hash": digest,
                "checked_at": _now_iso(),
                "row_count": len(rows),
                "upcs": upcs,
                "message_create_time": message_timestamp,
            }
        )

        if dry_run:
            log(f"[dry-run] would post AP/AR card and summary for {message_id} with {len(rows)} rows")
            state[message_id] = entry
            continue

        sent_any = False
        sent_any = _send_message_components(t, "owner", owner_open_id, card, summary_text, owner_state, message_id) or sent_any
        if temp_should_receive:
            sent_any = _send_message_components(t, TEMP_AP_AR_RECIPIENT_NAME, temp_recipient_open_id, card, summary_text, temp_state, message_id) or sent_any

        entry["result"] = "posted" if _recipient_is_fully_sent(owner_state) else "partial"
        state[message_id] = entry
        if sent_any:
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
