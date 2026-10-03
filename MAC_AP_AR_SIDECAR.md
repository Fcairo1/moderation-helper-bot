# Mac AP/AR Sidecar

## Why this exists

The sandbox daemon is kept as-is.

SoundOn Admin enrichment for AP/AR takedown alerts needs the same approved Mac network path that already powers the existing moderation Admin sidecar. This companion script runs locally on Filipe's Mac, polls the AP / AR Takedown Alert Bot group, resolves BR UPC takedown history through SoundOn Admin, and sends a private Lark card to Filipe.

## Scope

- Trigger source: `AP / AR Takedown Alert Bot`
- Chat ID: `oc_924bed04f6dce9a994fbdfd350a50844`
- Filter: BR rows only from `New Deleted AP/AR Releases`
- Output: PM card to Filipe only
- Timestamp display: BRT (`America/Sao_Paulo`)
- Attribution rule: if the latest takedown is by `system`, look for the immediately preceding `Content Safety Claim Status Update` within the same operation window and attribute the removal to that operator/reason when present

## Files

- Script: `mac_ap_ar_sidecar.py`
- LaunchAgent: `deploy/com.soundon.moderation-ap-ar-sidecar.plist`
- State file: `moderation_bot/mac_ap_ar_sidecar_state.json`
- Logs: `moderation_bot/mac_ap_ar_sidecar.log`, `moderation_bot/mac_ap_ar_sidecar.launchd.log`

## Local install / reload

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.soundon.moderation-ap-ar-sidecar.plist" 2>/dev/null || true
cp deploy/com.soundon.moderation-ap-ar-sidecar.plist "$HOME/Library/LaunchAgents/"
launchctl load -w "$HOME/Library/LaunchAgents/com.soundon.moderation-ap-ar-sidecar.plist"
```

## Dry run

```bash
cd /Users/bytedance/moderation-helper-bot
/usr/bin/python3 mac_ap_ar_sidecar.py --dry-run
```

## Live one-shot run

```bash
cd /Users/bytedance/moderation-helper-bot
/usr/bin/python3 mac_ap_ar_sidecar.py
```

## Notes

- This is intentionally separate from `mac_admin_sidecar.py` so the existing moderation-request flow stays untouched.
- The LaunchAgent runs the script once every 300 seconds; the script itself is idempotent via its state file.
- If Admin is temporarily blocked locally, the sidecar leaves the message uncommitted in state so a later run can retry.
