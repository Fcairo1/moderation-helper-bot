#!/usr/bin/env python3
"""Read-only SoundOn Admin lookups for moderation triage.

Authentication is acquired from the local bytedcli session. Tokens and signed
asset URLs are never persisted by this module.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

import requests


ADMIN_BASE_URL = os.getenv(
    "SOUNDON_ADMIN_BASE_URL",
    "https://sg-musician-admin.bytedance.net/avenue/api",
).rstrip("/")
DEFAULT_REGION = (os.getenv("SOUNDON_DEFAULT_REGION") or "BR").strip().upper()
STARLING_URL = (
    "https://starling-oversea.byteoversea.com/check_and_get_text/"
    "2eaf5320937911eb8b73139f8e44ed11/normal/avenue?lang=en,en"
)
BYTEDCLI_TOKEN_COMMAND = os.getenv(
    "SOUNDON_BYTEDCLI_TOKEN_COMMAND",
    "NPM_CONFIG_REGISTRY=http://bnpm.byted.org bunx @bytedance-dev/bytedcli "
    "--cloud-site i18n-tt auth get-bytecloud-jwt-token",
)
ADMIN_TRANSPORT = (os.getenv("SOUNDON_ADMIN_TRANSPORT") or "auto").strip().lower()

UPC_RE = re.compile(r"(?<!\d)(\d{12,13})(?!\d)")
ISRC_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2}[A-Z0-9]{3}\d{7})(?![A-Z0-9])", re.I)
LABELED_ID_RE = re.compile(
    r"\b(album(?:\s*id)?|song(?:\s*id)?|track(?:\s*id)?|user(?:\s*id)?|artist(?:\s*id)?)\s*[:=#-]?\s*(\d{16,20})\b",
    re.I,
)
BARE_ID_RE = re.compile(r"^\s*(\d{16,20})\s*$")
APPROVAL_RE = re.compile(r"\b(approve|approval|aprovar|aprova(?:ç|c)[aã]o|aprovem|liberar|libera(?:ç|c)[aã]o)\b", re.I)
TRACK_REVIEW_RE = re.compile(
    r"\b(tracks?|songs?|faixas?|m[uú]sicas?|audios?|[áa]udios?|isrc|moder(?:ate|ation)|moderar|rejected|rejeitad[oa]s?)\b",
    re.I,
)

RELEASE_STATUS = {
    0: "Init",
    10: "Under Review",
    20: "Not Approved",
    30: "Approved",
    40: "Delivered",
    50: "Live",
    60: "Takedown",
}

APPROVED_AUDIT_TYPES = {4}
REJECTED_AUDIT_TYPES = {5}

_token_lock = threading.Lock()
_cached_token = ""
_cached_token_at = 0.0
_reason_lock = threading.Lock()
_reason_catalog: Dict[str, str] = {}
_reason_catalog_at = 0.0
_gateway_block_lock = threading.Lock()
_gateway_blocked_until = 0.0
_requests_session_local = threading.local()
GATEWAY_BLOCK_CACHE_SECONDS = max(60, int(os.getenv("SOUNDON_ADMIN_GATEWAY_BLOCK_CACHE_SECONDS", "600")))


class AdminLookupError(RuntimeError):
    """Safe, user-displayable Admin lookup failure."""


def _looks_like_jwt(value: str) -> bool:
    token = (value or "").strip()
    return token.startswith("eyJ") and token.count(".") == 2


def _compact_text(value: Any, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _response_error_snippet(body: str) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except ValueError:
        return _compact_text(text)
    if not isinstance(payload, dict):
        return _compact_text(text)
    parts = []
    for key in ("code", "error", "msg", "message", "result"):
        value = payload.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={_compact_text(value, 120)}")
    base = payload.get("baseResp")
    if isinstance(base, dict):
        for key in ("errorCode", "errorMessage"):
            value = base.get(key)
            if value not in (None, "", 0, "0"):
                parts.append(f"baseResp.{key}={_compact_text(value, 120)}")
    if parts:
        return "; ".join(parts)
    return _compact_text(text)


def _is_gateway_blocked_body(body: str) -> bool:
    text = (body or "").lower()
    if "network_segregation" in text or "operations gateway" in text:
        return True
    try:
        payload = json.loads(body or "{}")
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    if str(payload.get("code")) == "4005":
        return True
    haystack = " ".join(str(payload.get(key) or "") for key in ("error", "msg", "message", "result")).lower()
    return "network_segregation" in haystack or "operations gateway" in haystack


def _is_gateway_block_cached() -> bool:
    with _gateway_block_lock:
        return time.time() < _gateway_blocked_until


def _cache_gateway_block() -> None:
    global _gateway_blocked_until
    with _gateway_block_lock:
        _gateway_blocked_until = time.time() + GATEWAY_BLOCK_CACHE_SECONDS

def _get_token(force: bool = False) -> str:
    global _cached_token, _cached_token_at
    with _token_lock:
        if not force and _cached_token and time.time() - _cached_token_at < 600:
            return _cached_token
        try:
            result = subprocess.run(
                BYTEDCLI_TOKEN_COMMAND,
                shell=True,
                executable="/bin/bash",
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdminLookupError("Admin authentication is unavailable on this machine.") from exc
        token = (result.stdout or "").strip()
        if result.returncode != 0 or not token:
            raise AdminLookupError("Admin authentication failed. Refresh the local bytedcli login.")
        if not _looks_like_jwt(token):
            stderr_text = _compact_text(result.stderr)
            message = "Admin authentication command did not return a JWT."
            if stderr_text:
                message += f" stderr: {stderr_text}"
            raise AdminLookupError(message)
        _cached_token = token
        _cached_token_at = time.time()
        return token


def _requests_session() -> requests.Session:
    session = getattr(_requests_session_local, "session", None)
    if session is None:
        session = requests.Session()
        _requests_session_local.session = session
    return session


def _requests_transport(url: str, token_value: str, region: str) -> Tuple[int, str]:
    response = _requests_session().get(
        url,
        headers={
            "x-jwt-token": token_value,
            "aop-region": region,
            "Content-Type": "application/json",
        },
        timeout=25,
    )
    return response.status_code, response.text


def _curl_transport(url: str, token_value: str, region: str) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            [
                "curl",
                "-sS",
                "--max-time",
                "25",
                "--write-out",
                "\n%{http_code}",
                "--config",
                "-",
                url,
            ],
            input=(
                f'header = "x-jwt-token: {token_value}"\n'
                f'header = "aop-region: {region}"\n'
                'header = "Content-Type: application/json"\n'
            ),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdminLookupError("Native curl transport is unavailable on the bot runtime.") from exc
    body, separator, status_text = (result.stdout or "").rpartition("\n")
    if not separator or not status_text.isdigit():
        raise AdminLookupError("Native curl could not reach SoundOn Admin.")
    status = int(status_text)
    if status == 0:
        raise AdminLookupError("Native curl could not reach SoundOn Admin.")
    return status, body


def _send_transport(url: str, token_value: str, region: str) -> Tuple[int, str, str]:
    request_error = None
    if ADMIN_TRANSPORT in ("auto", "requests"):
        try:
            status, body = _requests_transport(url, token_value, region)
            if ADMIN_TRANSPORT == "requests" or status < 400 or status == 401:
                return status, body, "requests"
        except requests.RequestException as exc:
            request_error = exc
            if ADMIN_TRANSPORT == "requests":
                raise AdminLookupError("Python HTTPS could not reach SoundOn Admin.") from exc
    if ADMIN_TRANSPORT in ("auto", "curl"):
        try:
            status, body = _curl_transport(url, token_value, region)
            return status, body, "curl"
        except AdminLookupError as curl_exc:
            if request_error is not None:
                raise AdminLookupError(
                    "Both Python HTTPS and native curl failed from the bot runtime."
                ) from curl_exc
            raise
    raise AdminLookupError(f"Unsupported SOUNDON_ADMIN_TRANSPORT: {ADMIN_TRANSPORT}")


def _request(path: str, params: Sequence[Tuple[str, Any]], region: str = DEFAULT_REGION) -> dict:
    if _is_gateway_block_cached():
        raise AdminLookupError("SoundOn Admin gateway is blocked from this runtime; retrying after cache window.")

    url = f"{ADMIN_BASE_URL}{path}?{urlencode(list(params), doseq=True)}"
    token_value = _get_token()
    status, body, transport = _send_transport(url, token_value, region)
    if status in (401, 403) and not _is_gateway_blocked_body(body):
        token_value = _get_token(force=True)
        status, body, transport = _send_transport(url, token_value, region)
    if status >= 400:
        detail = _response_error_snippet(body)
        if _is_gateway_blocked_body(body):
            _cache_gateway_block()
            raise AdminLookupError(
                f"SoundOn Admin rejected this runtime's network path via {transport} (gateway/network segregation)."
            )
        message = f"SoundOn Admin returned HTTP {status} via {transport}."
        if detail:
            message += f" {detail}"
        raise AdminLookupError(message)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise AdminLookupError(f"SoundOn Admin returned invalid JSON via {transport}.") from exc
    if (
        isinstance(payload, dict)
        and str(payload.get("code")) == "4005"
        and transport == "requests"
        and ADMIN_TRANSPORT == "auto"
    ):
        status, body = _curl_transport(url, token_value, region)
        transport = "curl"
        if status >= 400:
            detail = _response_error_snippet(body)
            if _is_gateway_blocked_body(body):
                _cache_gateway_block()
                raise AdminLookupError(
                    "SoundOn Admin rejected this runtime's network path via curl (gateway/network segregation)."
                )
            message = f"SoundOn Admin returned HTTP {status} via curl."
            if detail:
                message += f" {detail}"
            raise AdminLookupError(message)
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise AdminLookupError("SoundOn Admin returned invalid JSON via curl.") from exc
    if isinstance(payload, dict) and str(payload.get("code")) == "4005":
        _cache_gateway_block()
        raise AdminLookupError(
            f"SoundOn Admin rejected this runtime's network path via {transport} (gateway code 4005)."
        )
    base = payload.get("baseResp") if isinstance(payload, dict) else None
    if isinstance(base, dict) and base.get("errorCode") not in (None, 0, "0"):
        raise AdminLookupError(base.get("errorMessage") or "SoundOn Admin returned an error.")
    return payload


def one_search(search_key: str, region: str = DEFAULT_REGION) -> dict:
    return _request("/search/one", [("searchKey", search_key)], region)


def search_entity(entity: int, filters: Sequence[Tuple[str, str]], region: str = DEFAULT_REGION) -> dict:
    params: List[Tuple[str, Any]] = [("entity", entity), ("offset", 0), ("count", 20)]
    params.extend(filters)
    return _request("/search/entity", params, region)


def audit_history(category: int, target_id: str, region: str = DEFAULT_REGION) -> dict:
    return _request(
        "/audit/list",
        [("offset", 0), ("count", 100), ("category", category), ("targetId", target_id)],
        region,
    )


def _timestamp(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    text_value = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text_value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _first_timestamp(record: dict, *keys: str) -> Optional[float]:
    for key in keys:
        parsed = _timestamp(record.get(key))
        if parsed is not None:
            return parsed
    return None


def review_status(record: dict) -> Tuple[str, str]:
    """Return a stable status key and a user-facing Admin review label."""
    raw = None
    source_key = ""
    for key in ("reviewStatus", "auditStatus", "moderationStatus", "releaseStatus", "status"):
        if record.get(key) not in (None, ""):
            raw = record[key]
            source_key = key
            break
    numeric = None
    try:
        numeric = int(raw)
    except (TypeError, ValueError):
        pass
    if source_key == "releaseStatus":
        if numeric in (30, 40, 50):
            return "approved", "Approved"
        if numeric == 20:
            return "not_approved", "Not Approved"
        if numeric == 10:
            return "under_review", "Under Review"
        if numeric == 0:
            return "to_be_reviewed", "To Be Reviewed"
    # Track search records use status: 0 queue pending, 1 reviewing, 2 pass,
    # 3 reject. This is distinct from the album releaseStatus enum above.
    if source_key in ("reviewStatus", "auditStatus", "moderationStatus", "status"):
        if numeric == 0:
            return "to_be_reviewed", "To Be Reviewed"
        if numeric == 1:
            return "under_review", "Under Review"
        if numeric == 2:
            return "approved", "Approved"
        if numeric == 3:
            return "not_approved", "Not Approved"
    normalized = re.sub(r"[^a-z]+", " ", str(raw or "").lower()).strip()
    if normalized in ("approved", "pass", "passed"):
        return "approved", "Approved"
    if normalized in ("under review", "reviewing", "in review"):
        return "under_review", "Under Review"
    if normalized in ("to be review", "to be reviewed", "pending review", "waiting review", "init"):
        return "to_be_reviewed", "To Be Reviewed"
    if normalized in ("not approved", "rejected", "reject", "failed"):
        return "not_approved", "Not Approved"
    return "unknown", str(raw if raw not in (None, "") else "Unknown")


def _reason_translations() -> Dict[str, str]:
    global _reason_catalog, _reason_catalog_at
    with _reason_lock:
        if _reason_catalog and time.time() - _reason_catalog_at < 21600:
            return _reason_catalog
        try:
            response = requests.get(STARLING_URL, timeout=15)
            response.raise_for_status()
            data = ((response.json().get("message") or {}).get("Data") or {})
            catalog = {
                key.removeprefix("reject_reason_"): str(value)
                for key, value in data.items()
                if key.startswith("reject_reason_") and value
            }
            if catalog:
                _reason_catalog = catalog
                _reason_catalog_at = time.time()
        except Exception as exc:
            print(f"admin rejection-reason catalog unavailable: {exc!r}", flush=True)
        return _reason_catalog


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _reason_codes_from(value: Any) -> List[str]:
    obj = _json_object(value)
    raw = obj.get("reasonList") or obj.get("rejectReasons") or []
    if not raw and obj.get("rejectReason") is not None:
        raw = [obj.get("rejectReason")]
    if not isinstance(raw, list):
        raw = [raw]
    return [str(item) for item in raw if item not in (None, "", 0, "0")]


def translate_reasons(codes: Iterable[str]) -> List[str]:
    catalog = _reason_translations()
    seen = set()
    reasons = []
    for code in codes:
        code = str(code)
        if code in seen:
            continue
        seen.add(code)
        reasons.append(catalog.get(code) or f"Rejection reason code {code}")
    return reasons


def extract_identifiers(text: str) -> List[Tuple[str, str]]:
    text = text or ""
    found: List[Tuple[str, str]] = []
    for match in LABELED_ID_RE.finditer(text):
        label = match.group(1).lower().replace(" ", "")
        kind = "song" if label.startswith(("song", "track")) else label.removesuffix("id")
        found.append((kind, match.group(2)))
    for match in UPC_RE.finditer(text):
        found.append(("upc", match.group(1)))
    for match in ISRC_RE.finditer(text):
        found.append(("isrc", match.group(1).upper()))
    bare = BARE_ID_RE.match(text)
    if bare:
        found.append(("id", bare.group(1)))
    unique = []
    seen = set()
    for item in found:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def is_approval_request(text: str) -> bool:
    return bool(APPROVAL_RE.search(text or ""))


def looks_like_lookup(text: str) -> bool:
    stripped = (text or "").strip()
    identifiers = extract_identifiers(stripped)
    if not identifiers:
        return False
    residual = stripped
    residual = LABELED_ID_RE.sub("", residual)
    residual = UPC_RE.sub("", residual)
    residual = ISRC_RE.sub("", residual)
    residual = re.sub(r"\b(upc|isrc|album|song|track|artist|user|id|lookup|admin|please|check|find)\b", "", residual, flags=re.I)
    return not residual.strip(" /:-#?,.")


def _first_list(payload: dict, *keys: str) -> List[dict]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list) and value:
            return [item for item in value if isinstance(item, dict)]
    return []


def _enrich_album(album: dict, region: str) -> dict:
    album_id = str(album.get("albumId") or album.get("objectId") or "")
    if not album_id:
        return album
    detailed = search_entity(
        2,
        [("filters[albumFilter][albumId][0]", album_id)],
        region,
    )
    rows = _first_list(detailed, "albumList")
    return rows[0] if rows else album


def _enrich_song(song: dict, region: str) -> dict:
    song_id = str(song.get("songId") or song.get("objectId") or "")
    if not song_id:
        return song
    detailed = search_entity(
        1,
        [("filters[songFilter][songId][0]", song_id)],
        region,
    )
    rows = _first_list(detailed, "songList")
    return rows[0] if rows else song


def _fallback_id_lookup(identifier: str, region: str) -> Tuple[str, Optional[dict]]:
    attempts = (
        ("album", 2, "filters[albumFilter][albumId][0]", "albumList"),
        ("song", 1, "filters[songFilter][songId][0]", "songList"),
        ("artist", 3, "filters[artistFilter][artistId][0]", "artistList"),
        ("user", 4, "filters[userFilter][userId][0]", "userList"),
    )
    for kind, entity, field, result_key in attempts:
        rows = _first_list(search_entity(entity, [(field, identifier)], region), result_key)
        if rows:
            return kind, rows[0]
    return "", None


def lookup(identifier: str, region: str = DEFAULT_REGION) -> dict:
    identifier = (identifier or "").strip()
    extracted = extract_identifiers(identifier)
    search_key = extracted[0][1] if extracted else identifier
    if not search_key:
        raise AdminLookupError("Provide a UPC, ISRC, album ID, song ID, artist ID, or user ID.")
    payload = one_search(search_key, region)
    albums = _first_list(payload, "albumList")
    songs = _first_list(payload, "songList", "trackList")
    artists = _first_list(payload, "artistList")
    users = _first_list(payload, "userList")
    kind = ""
    record: Optional[dict] = None
    if albums:
        kind, record = "album", _enrich_album(albums[0], str(albums[0].get("region") or region))
    elif songs:
        kind, record = "song", _enrich_song(songs[0], str(songs[0].get("region") or region))
    elif artists:
        kind, record = "artist", artists[0]
    elif users:
        kind, record = "user", users[0]
    elif search_key.isdigit() and len(search_key) >= 16:
        kind, record = _fallback_id_lookup(search_key, region)
    if not record:
        return {"found": False, "identifier": search_key, "region": region, "raw": payload}

    actual_region = str(record.get("region") or region)
    reasons = rejection_reasons(kind, record, actual_region)
    return {
        "found": True,
        "identifier": search_key,
        "kind": kind,
        "record": record,
        "region": actual_region,
        "rejectionReasons": reasons,
    }


def rejection_reasons(kind: str, record: dict, region: str) -> List[str]:
    codes = _reason_codes_from(record.get("extra"))
    if review_status(record)[0] == "approved" and not codes:
        return []
    targets: List[Tuple[int, str]] = []
    if kind == "song":
        song_id = str(record.get("songId") or record.get("objectId") or "")
        if song_id:
            targets.append((2, song_id))
    elif kind == "album":
        album_id = str(record.get("albumId") or record.get("objectId") or "")
        if album_id:
            targets.append((1, album_id))
            songs = search_entity(
                1,
                [("filters[songFilter][albumId][0]", album_id)],
                region,
            )
            for song in _first_list(songs, "songList"):
                codes.extend(_reason_codes_from(song.get("extra")))
                song_id = str(song.get("songId") or song.get("objectId") or "")
                if song_id:
                    targets.append((2, song_id))
    for category, target_id in targets:
        history = audit_history(category, target_id, region)
        for audit in _first_list(history, "auditList"):
            codes.extend(_reason_codes_from(audit.get("extra")))
    return translate_reasons(codes)


def _audit_action(audit: dict) -> str:
    try:
        audit_type = int(audit.get("auditType"))
    except (TypeError, ValueError):
        audit_type = None
    if audit_type in APPROVED_AUDIT_TYPES:
        return "approved"
    if audit_type in REJECTED_AUDIT_TYPES:
        return "rejected"
    label = " ".join(
        str(audit.get(key) or "")
        for key in ("auditTypeName", "operation", "action", "status", "result")
    ).lower()
    if any(word in label for word in ("not approved", "reject", "fail")):
        return "rejected"
    if any(word in label for word in ("approved", "approve", "pass")):
        return "approved"
    return ""


def _audit_time(audit: dict) -> float:
    return _first_timestamp(
        audit,
        "updateTime",
        "updatedAt",
        "createTime",
        "createdAt",
        "auditTime",
        "operationTime",
    ) or 0.0


def latest_moderation_operation(record: dict, kind: str, region: str) -> dict:
    """Find the latest approve/reject operation across album and track levels."""
    targets: List[Tuple[str, int, str]] = []
    song_id = str(record.get("songId") or "")
    album_id = str(record.get("albumId") or "")
    if kind == "song" and song_id:
        targets.append(("track", 2, song_id))
    if album_id:
        targets.append(("album", 1, album_id))
    if kind == "album":
        album_id = str(record.get("albumId") or record.get("objectId") or "")
        if album_id and not any(target[2] == album_id for target in targets):
            targets.append(("album", 1, album_id))
        if album_id:
            songs = search_entity(1, [("filters[songFilter][albumId][0]", album_id)], region)
            for song in _first_list(songs, "songList"):
                sibling_song_id = str(song.get("songId") or song.get("objectId") or "")
                if sibling_song_id:
                    targets.append(("track", 2, sibling_song_id))

    operations = []
    for level, category, target_id in targets:
        for audit in _first_list(audit_history(category, target_id, region), "auditList"):
            action = _audit_action(audit)
            if action:
                operations.append(
                    {
                        "level": level,
                        "targetId": target_id,
                        "action": action,
                        "time": _audit_time(audit),
                        "reasonCodes": _reason_codes_from(audit.get("extra")),
                        "audit": audit,
                    }
                )
    if not operations:
        return {}
    latest = max(operations, key=lambda item: item["time"])
    if latest["action"] == "rejected" and not latest["reasonCodes"]:
        # Admin sometimes records the reason on the sibling level. Use the most
        # recent rejected sibling operation only; never surface an older stale reason.
        sibling_rejections = [
            item for item in operations
            if item["action"] == "rejected" and item["level"] != latest["level"] and item["reasonCodes"]
        ]
        if sibling_rejections:
            sibling = max(sibling_rejections, key=lambda item: item["time"])
            latest["reasonCodes"] = sibling["reasonCodes"]
            latest["reasonLevel"] = sibling["level"]
    latest["reasons"] = translate_reasons(latest.get("reasonCodes") or [])
    return latest


def _submission_time(record: dict) -> Optional[float]:
    return _first_timestamp(
        record,
        "applyTime",
        "submissionTime",
        "submitTime",
        "submittedAt",
        "auditSubmitTime",
        "createTime",
        "createdAt",
    )


def track_review_summary(text: str, message_time_ms: Any = None) -> Optional[str]:
    """Build compact per-track review guidance for automatic triage cards."""
    identifiers = extract_identifiers(text)
    if not identifiers:
        return None
    if not TRACK_REVIEW_RE.search(text or "") and not any(kind == "song" for kind, _ in identifiers):
        return None
    message_time = _timestamp(message_time_ms) or time.time()
    lines = []
    for _, identifier in identifiers[:12]:
        try:
            result = lookup(identifier)
        except AdminLookupError as exc:
            lines.append(f"• `{identifier}` — Admin lookup unavailable: {exc}")
            continue
        if not result.get("found"):
            lines.append(f"• `{identifier}` — not found in Admin")
            continue
        record = result["record"]
        kind = result["kind"]
        status_key, status_label = review_status(record)
        display_id = str(record.get("songId") or record.get("albumId") or identifier)
        if status_key == "approved":
            continue
        if status_key == "under_review":
            lines.append(f"• `{display_id}` — **Under Review**")
            continue
        if status_key == "to_be_reviewed":
            submitted = _submission_time(record)
            if submitted is None:
                lines.append(f"• `{display_id}` — **To Be Reviewed** (submission time unavailable)")
            elif message_time - submitted <= 86400:
                lines.append(f"• `{display_id}` — **Reaching Queue**")
            else:
                lines.append(f"• `{display_id}` — **Not sent to the queue — possible issue**")
            continue
        if status_key == "not_approved":
            operation = latest_moderation_operation(record, kind, result["region"])
            if operation.get("action") == "approved":
                # The operation log is authoritative when search status lags.
                continue
            reasons = operation.get("reasons") or result.get("rejectionReasons") or []
            reason_text = "; ".join(reasons) if reasons else "reason not found in album or track operation log"
            level = operation.get("reasonLevel") or operation.get("level")
            suffix = f" ({level} log)" if level else ""
            lines.append(f"• `{display_id}` — **Not Approved:** {reason_text}{suffix}")
            continue
        lines.append(f"• `{display_id}` — Review Status: **{status_label}**")
    if not lines:
        return None
    return "🔎 **Admin track review**\n" + "\n".join(lines)


def _value(record: dict, *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if value not in (None, "", []):
            if isinstance(value, list):
                return ", ".join(str(item) for item in value)
            return str(value)
    return "N/A"


def _admin_link(kind: str, record: dict) -> str:
    if kind == "album":
        album_id = _value(record, "albumId", "objectId")
        return f"https://sg-musician-admin.bytedance.net/avenue/content/album/new?albumId={album_id}"
    if kind == "song":
        song_id = _value(record, "songId", "objectId")
        return f"https://sg-musician-admin.bytedance.net/avenue/content/song/new/?songId={song_id}"
    if kind == "user":
        return f"https://sg-musician-admin.bytedance.net/avenue/content/account/new/?userId={_value(record, 'userId', 'objectId')}"
    if kind == "artist":
        return f"https://sg-musician-admin.bytedance.net/avenue/content/artist/new/?artistId={_value(record, 'artistId', 'objectId')}"
    return ""


def format_lookup(result: dict) -> str:
    if not result.get("found"):
        return f"No SoundOn Admin result found for {result.get('identifier', 'that identifier')}."
    record = result["record"]
    kind = result["kind"]
    status_value = record.get("releaseStatus")
    status = RELEASE_STATUS.get(status_value, _value(record, "releaseStatus", "status"))
    lines = [
        f"🎵 SoundOn Admin — {kind.title()}",
        f"Title: {_value(record, 'title', 'stageName', 'name')}",
        f"Artist(s): {_value(record, 'artistList', 'displayArtists', 'artistName')}",
        f"Account: {_value(record.get('userBase') or {}, 'stageName', 'legalName')}",
        f"Region: {result.get('region') or 'N/A'}",
        f"Release status: {status}",
        f"UPC: {_value(record, 'upc', 'upcCode', 'UPC')}",
        f"ISRC: {_value(record, 'isrc', 'ISRC')}",
        f"Album ID: {_value(record, 'albumId')}",
        f"Song ID: {_value(record, 'songId')}",
        f"User ID: {_value(record, 'userId')}",
        f"Release date: {_value(record, 'formattedReleaseTime', 'releaseTime')}",
        f"Genre: {_value(record, 'mainGenre')} / {_value(record, 'subGenre')}",
        f"Language: {_value(record, 'language')}",
    ]
    reasons = result.get("rejectionReasons") or []
    lines.append("Rejection reason: " + ("; ".join(reasons) if reasons else "None found"))
    artwork = (record.get("coverUrls") or {}).get("origin") or record.get("coverUrl")
    if artwork:
        lines.append(f"Artwork: {artwork}")
    link = _admin_link(kind, record)
    if link:
        lines.append(f"Admin: {link}")
    return "\n".join(lines)[:3900]


def approval_rejection_summary(text: str) -> Optional[str]:
    if not is_approval_request(text):
        return None
    identifiers = extract_identifiers(text)
    if not identifiers:
        return "⚠️ Admin rejection reason: identifier not found in the request"
    try:
        result = lookup(identifiers[0][1])
    except AdminLookupError as exc:
        return f"⚠️ Admin rejection reason: lookup unavailable — {exc}"
    if not result.get("found"):
        return "⚠️ Admin rejection reason: release not found"
    reasons = result.get("rejectionReasons") or []
    if reasons:
        return "🚫 **Admin rejection reason:** " + "; ".join(reasons)
    status_value = result["record"].get("releaseStatus")
    status = RELEASE_STATUS.get(status_value, str(status_value or "unknown"))
    return f"ℹ️ **Admin rejection reason:** none found (release status: {status})"
