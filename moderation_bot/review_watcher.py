#!/usr/bin/env python3
"""Persistent read-only polling for track moderation status transitions."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from moderation_bot.admin_client import (
    AdminLookupError,
    TRACK_REVIEW_RE,
    extract_identifiers,
    lookup,
    review_status,
)


ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "moderation_bot/review_watch_state.json"
POLL_SECONDS = max(60, int(os.getenv("MODERATION_HELPER_REVIEW_POLL_SECONDS", "120")))
MAX_AGE_SECONDS = max(3600, int(os.getenv("MODERATION_HELPER_REVIEW_WATCH_MAX_AGE_SECONDS", "172800")))
MAX_ERROR_BACKOFF_SECONDS = max(POLL_SECONDS, int(os.getenv("MODERATION_HELPER_REVIEW_ERROR_BACKOFF_SECONDS", "900")))

_lock = threading.Lock()


def _load() -> Dict[str, dict]:
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save(state: Dict[str, dict]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def active_watches() -> Dict[str, dict]:
    with _lock:
        return dict(_load())


def register_review_watches(
    text: str,
    source_message_id: str,
    card_message_id: str,
    source_group: str,
) -> int:
    """Register only tracks still waiting for review, plus temporary failures."""
    identifiers = extract_identifiers(text)
    if not identifiers:
        return 0
    if not TRACK_REVIEW_RE.search(text or "") and not any(kind == "song" for kind, _ in identifiers):
        return 0
    now = time.time()
    additions: Dict[str, dict] = {}
    for kind, identifier in identifiers[:12]:
        status_key = "lookup_unavailable"
        canonical_id = identifier
        title = ""
        try:
            result = lookup(identifier)
            if not result.get("found"):
                status_key = "not_found"
            else:
                record = result["record"]
                canonical_id = str(record.get("songId") or identifier)
                title = str(record.get("title") or "")
                status_key, _ = review_status(record)
        except AdminLookupError:
            pass
        # Approved/not-approved are terminal. Under-review is already shown on
        # the newly generated card, so no later transition alert is required.
        if status_key in ("approved", "not_approved", "under_review", "not_found"):
            continue
        additions[canonical_id] = {
            "identifier": canonical_id,
            "sourceMessageId": source_message_id,
            "cardMessageId": card_message_id,
            "sourceGroup": source_group,
            "title": title,
            "createdAt": now,
            "expiresAt": now + MAX_AGE_SECONDS,
            "nextCheckAt": now + POLL_SECONDS,
            "lastStatus": status_key,
            "failures": 0,
        }
    if not additions:
        return 0
    with _lock:
        state = _load()
        state.update(additions)
        _save(state)
    return len(additions)


def poll_once(alert: Callable[[dict, dict], None], now: Optional[float] = None) -> dict:
    now = now or time.time()
    checked = alerted = removed = failed = 0
    with _lock:
        state = _load()
        for identifier, entry in list(state.items()):
            if now >= float(entry.get("expiresAt") or 0):
                state.pop(identifier, None)
                removed += 1
                continue
            if now < float(entry.get("nextCheckAt") or 0):
                continue
            checked += 1
            try:
                result = lookup(identifier)
                if not result.get("found"):
                    entry["lastStatus"] = "not_found"
                    entry["nextCheckAt"] = now + MAX_ERROR_BACKOFF_SECONDS
                    failed += 1
                    continue
                record = result["record"]
                status_key, status_label = review_status(record)
                entry["lastStatus"] = status_key
                entry["lastStatusLabel"] = status_label
                entry["title"] = str(record.get("title") or entry.get("title") or "")
                entry["failures"] = 0
                if status_key == "under_review":
                    alert(entry, result)
                    state.pop(identifier, None)
                    alerted += 1
                elif status_key in ("approved", "not_approved"):
                    state.pop(identifier, None)
                    removed += 1
                else:
                    entry["nextCheckAt"] = now + POLL_SECONDS
            except AdminLookupError as exc:
                failures = int(entry.get("failures") or 0) + 1
                entry["failures"] = failures
                entry["lastError"] = str(exc)
                entry["nextCheckAt"] = now + min(
                    MAX_ERROR_BACKOFF_SECONDS,
                    POLL_SECONDS * (2 ** min(failures - 1, 4)),
                )
                failed += 1
        _save(state)
    return {
        "checked": checked,
        "alerted": alerted,
        "removed": removed,
        "failed": failed,
        "active": len(state),
    }
