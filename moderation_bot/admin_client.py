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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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

UPC_RE = re.compile(r"(?<!\d)(\d{12,13})(?!\d)")
ISRC_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2}[A-Z0-9]{3}\d{7})(?![A-Z0-9])", re.I)
LABELED_ID_RE = re.compile(
    r"\b(album(?:\s*id)?|song(?:\s*id)?|track(?:\s*id)?|user(?:\s*id)?|artist(?:\s*id)?)\s*[:=#-]?\s*(\d{16,20})\b",
    re.I,
)
BARE_ID_RE = re.compile(r"^\s*(\d{16,20})\s*$")
APPROVAL_RE = re.compile(r"\b(approve|approval|aprovar|aprova(?:ç|c)[aã]o|aprovem|liberar|libera(?:ç|c)[aã]o)\b", re.I)

RELEASE_STATUS = {
    0: "Init",
    10: "Under Review",
    20: "Not Approved",
    30: "Approved",
    40: "Delivered",
    50: "Live",
    60: "Takedown",
}

_token_lock = threading.Lock()
_cached_token = ""
_cached_token_at = 0.0
_reason_lock = threading.Lock()
_reason_catalog: Dict[str, str] = {}
_reason_catalog_at = 0.0


class AdminLookupError(RuntimeError):
    """Safe, user-displayable Admin lookup failure."""


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
        _cached_token = token
        _cached_token_at = time.time()
        return token


def _request(path: str, params: Sequence[Tuple[str, Any]], region: str = DEFAULT_REGION) -> dict:
    def send(force_token: bool = False):
        return requests.get(
            f"{ADMIN_BASE_URL}{path}",
            params=list(params),
            headers={
                "x-jwt-token": _get_token(force=force_token),
                "aop-region": region,
                "Content-Type": "application/json",
            },
            timeout=25,
        )

    try:
        response = send()
        if response.status_code == 401:
            response = send(force_token=True)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise AdminLookupError("SoundOn Admin could not be reached from the bot runtime.") from exc
    except ValueError as exc:
        raise AdminLookupError("SoundOn Admin returned an invalid response.") from exc
    if isinstance(payload, dict) and str(payload.get("code")) == "4005":
        raise AdminLookupError("SoundOn Admin rejected this runtime's network path.")
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
    if record.get("releaseStatus") in (30, 40, 50) and not codes:
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
