#!/usr/bin/env python3
"""Configuration for the SoundOn Moderation Helper bot.

Secrets must be supplied via environment variables. This module intentionally
contains no app secrets, access tokens, cookies, or private keys.
"""
from __future__ import annotations

import json
import os
from typing import Dict

DEFAULT_APP_ID = "cli_aab8063b7838dcb5"
DEFAULT_OWNER_EMAIL = "filipe.cairo@bytedance.com"
DEFAULT_CHATS: Dict[str, str] = {
    "oc_3f38d5645c707908bc409eef94c19e16": "Moderação BR",
    "oc_06e164c2626075982b13f21db335acbe": "BR P0 releases",
}


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default or "").strip()


def app_id() -> str:
    return _env("MODERATION_HELPER_APP_ID") or _env("LARK_APP_ID") or _env("BOT_APP_ID") or DEFAULT_APP_ID


def app_secret() -> str:
    value = _env("MODERATION_HELPER_APP_SECRET") or _env("LARK_APP_SECRET") or _env("BOT_SECRET") or _env("APP_SECRET")
    if not value:
        raise RuntimeError(
            "Missing Lark app secret. Set MODERATION_HELPER_APP_SECRET (preferred), "
            "LARK_APP_SECRET, BOT_SECRET, or APP_SECRET in the runtime environment."
        )
    return value


def owner_email() -> str:
    return _env("MODERATION_HELPER_OWNER_EMAIL") or _env("OWNER_EMAIL") or DEFAULT_OWNER_EMAIL


def monitored_chats() -> Dict[str, str]:
    """Return monitored chat_id -> display name mapping.

    Optional env override:
      MODERATION_HELPER_CHATS_JSON='{"chat_id":"Display Name"}'
    """
    raw = _env("MODERATION_HELPER_CHATS_JSON")
    if not raw:
        return dict(DEFAULT_CHATS)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid MODERATION_HELPER_CHATS_JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("MODERATION_HELPER_CHATS_JSON must be a JSON object of chat_id -> display name")
    chats = {str(k).strip(): str(v).strip() for k, v in payload.items() if str(k).strip()}
    return chats or dict(DEFAULT_CHATS)


def primary_chat_id() -> str:
    return _env("MODERATION_HELPER_PRIMARY_CHAT_ID") or next(iter(monitored_chats().keys()))
