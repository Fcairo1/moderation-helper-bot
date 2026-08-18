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

## Git hygiene

GitHub is the source of truth for source code. Runtime processes may write only ignored runtime files such as logs, PID files, heartbeat, card registry/state JSON, and local `.env` files.

Do not commit:

- app secrets or access tokens;
- OAuth/JWT/cookie/key files;
- logs and PID files;
- runtime card/reaction state;
- caches or virtual environments.
