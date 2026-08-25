# SoundOn Moderation Helper Bot

Standalone source for the Lark Open Platform app used to triage SoundOn BR moderation requests.

- Lark App ID: `cli_aab8063b7838dcb5`
- Runtime: Python 3.9+
- Main daemon: `python3 moderation_bot/persistent_callback.py`
- Optional supervisor: `bash moderation_bot/supervisor.sh`

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` locally or configure the same variable names in the production runtime. Never commit `.env` or secret files.

Required secret:

```text
MODERATION_HELPER_APP_SECRET
```

Useful variables:

```text
MODERATION_HELPER_APP_ID
MODERATION_HELPER_OWNER_EMAIL
MODERATION_HELPER_PRIMARY_CHAT_ID
MODERATION_HELPER_CHATS_JSON
SOUNDON_DEFAULT_REGION
```

`MODERATION_HELPER_CHATS_JSON` can override monitored groups, for example:

```json
{"oc_xxx":"Moderação BR","oc_yyy":"BR P0 releases"}
```

## Commands

```bash
python3 moderation_bot/persistent_callback.py
python3 moderation_bot/watchdog_listener.py
python3 moderation_bot/watchdog.py --once
python3 moderation_bot/daily_digest.py --days 7
python3 scan_send_modbr_triage.py
bash moderation_bot/supervisor.sh
```

### Read-only SoundOn Admin lookups

The bot can enrich approval-request triage cards with the current Admin
rejection reason when the message contains a UPC, ISRC, album ID, or song ID.
Other automatically triggered cards stay compact and do not include artwork or
the full Admin record.

Track moderation requests use the track Review Status:

- `Approved`: no Admin note is added;
- `Under Review`: the card says that it is under review;
- `To Be Reviewed`: submissions up to one day old say `Reaching Queue`; older
  submissions say `Not sent to the queue — possible issue`;
- `Not Approved`: the latest approve/reject operation is checked across both
  album and track logs, and the latest rejection reason is shown.

Explicit approval/moderation language (`approve`, `aprovar`, `moderar`, and
related forms) always qualifies as a request, even if another group member has
already added an unrelated reaction. In addition to real-time events, the
persistent daemon reconciles both monitored groups every five minutes by
default, recovering eligible messages missed during a connection interruption.
Configure this with `MODERATION_HELPER_RECONCILE_SECONDS` and
`MODERATION_HELPER_RECONCILE_LOOKBACK_SECONDS`.

In a direct chat with the configured owner, send a bare identifier or use:

```text
/admin 795005370745
/admin BRABC2400001
/admin albumId=7677777270908438545
/lookup songId=7677777324969445392
```

On-demand responses include release metadata, status, rejection reason,
artwork URL, and an Admin deep link. The integration is strictly read-only.

The bot runtime must have:

- a valid local `bytedcli` login for cloud site `i18n-tt`;
- network access to `https://sg-musician-admin.bytedance.net`;
- `bunx` available for acquiring the ByteCloud JWT.

JWTs and signed artwork URLs are not written to disk by the integration.

## Git hygiene

GitHub is the source of truth for source code. Runtime processes may write only ignored runtime files such as logs, PID files, heartbeat, card registry/state JSON, and local `.env` files.

Do not commit:

- app secrets or access tokens;
- OAuth/JWT/cookie/key files;
- logs and PID files;
- runtime card/reaction state;
- caches or virtual environments.
