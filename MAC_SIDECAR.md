# Mac Admin-lookup sidecar

## Why

The Aime sandbox that runs the main daemon (`moderation_bot/persistent_callback.py`)
is currently blocked from `sg-musician-admin.bytedance.net` by ROW Operations
Gateway network segregation (`HTTP 403`, `code 4005`). A Mac on an approved
network path is not blocked — the same `bytedcli` JWT flow succeeds from here.

`mac_admin_sidecar.py` is a **separate, additive script**. It does not touch the
sandbox, the running daemon, or its state. It:

1. Reads the two monitored Lark groups (read-only), same as the bot.
2. Recomputes the SoundOn Admin context locally for pending requests.
3. Posts the result once per request — as a private DM to you by default, or as
   a reply under the original group message (`--output group`).

If your Mac is off, nothing breaks — the daemon keeps triaging, posting cards,
and handling buttons exactly as it does today. This only fills the "Admin
lookup unavailable" gap when your Mac happens to be on.

## Setup

```bash
cd ~/moderation-helper-bot
python3 -m venv .venv   # optional — the sidecar only needs `requests`,
                          # already used elsewhere in the repo
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # if you don't already have one
```

Fill in `.env`:

```text
MODERATION_HELPER_APP_SECRET=<the app secret>
```

Everything else has a working default (`MODERATION_HELPER_APP_ID`,
`MODERATION_HELPER_OWNER_EMAIL`, the monitored groups).

Optional sidecar-specific overrides (put these in `.env` too):

```text
# dm (private, default) or group (reply under the original message)
MAC_SIDECAR_OUTPUT=dm
# how many days back to scan for pending requests
MAC_SIDECAR_LOOKBACK_DAYS=2
# how often --daemon mode loops (launchd users don't need this — see below)
MAC_SIDECAR_INTERVAL_SECONDS=300
# how long to wait before re-checking a request that had nothing to add yet
MAC_SIDECAR_EMPTY_RECHECK_SECONDS=21600
```

Confirm the Admin lookup actually works from this machine before wiring up the
schedule:

```bash
python3 mac_admin_sidecar.py --dry-run
```

You should see a JSON summary with `"errors": 0`. If `bytedcli`/`bunx` need a
fresh login, run the token command from `admin_client.py`'s
`SOUNDON_BYTEDCLI_TOKEN_COMMAND` manually first.

## Run it once manually

```bash
python3 mac_admin_sidecar.py
```

This posts DMs (or group replies) for any pending request whose Admin context
hasn't been posted yet, then exits.

## Run it on a schedule (launchd)

The `deploy/com.soundon.moderation-admin-sidecar.plist` file runs it every 5
minutes.

```bash
cp deploy/com.soundon.moderation-admin-sidecar.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.soundon.moderation-admin-sidecar.plist
```

Check it's running:

```bash
launchctl list | grep moderation-admin-sidecar
tail -f moderation_bot/mac_admin_sidecar.log
```

Stop it:

```bash
launchctl unload ~/Library/LaunchAgents/com.soundon.moderation-admin-sidecar.plist
```

If you move the repo to a different path, or `which bunx` shows a different
location than what's in the plist's `PATH`, edit
`deploy/com.soundon.moderation-admin-sidecar.plist` (and the copy in
`~/Library/LaunchAgents/`) before loading it.

**Don't put the repo under `~/Desktop`, `~/Documents`, or `~/Downloads`.**
macOS TCC privacy protection silently blocks background (launchd) processes
from reading those folders — you'll see `Operation not permitted` in
`mac_admin_sidecar.launchd.log` even though running the script by hand in
Terminal works fine. A plain folder directly under your home directory (like
`~/moderation-helper-bot`) isn't protected and just works.

## Dedupe / state

`moderation_bot/mac_admin_sidecar_state.json` (gitignored, like the bot's other
runtime state) tracks which pending requests already got a posted result, and
caches "nothing to add yet" for `MAC_SIDECAR_EMPTY_RECHECK_SECONDS` so it isn't
re-querying Admin every 5 minutes for the same still-pending track. Delete the
file to force everything to be re-checked.

## Turning it off

`launchctl unload` the plist (or just don't run the script). The main daemon is
completely unaffected either way.
