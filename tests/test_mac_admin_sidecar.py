import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mac_admin_sidecar as sidecar


class MacAdminSidecarTests(unittest.TestCase):
    def test_looks_blocked_detects_gateway_language(self):
        self.assertTrue(sidecar._looks_blocked("Admin lookup unavailable: SoundOn Admin rejected this runtime's network path via curl (gateway/network segregation)."))
        self.assertTrue(sidecar._looks_blocked("SoundOn Admin returned HTTP 403 via curl."))
        self.assertFalse(sidecar._looks_blocked("🔎 **Admin track review**\n• `123` — **Not Approved:** bad artwork"))

    def test_is_useful_filters_blocked_and_no_reason(self):
        self.assertFalse(sidecar._is_useful(""))
        self.assertFalse(sidecar._is_useful("Admin lookup unavailable: gateway/network segregation"))
        self.assertFalse(sidecar._is_useful("ℹ️ **Admin rejection reason:** none found (release status: Approved)"))
        self.assertTrue(sidecar._is_useful("🚫 **Admin rejection reason:** Unauthorized sample"))
        self.assertTrue(sidecar._is_useful("🔎 **Admin track review**\n• `123` — **Under Review**"))

    def test_admin_context_for_prefers_track_review_summary(self):
        with patch.object(sidecar, "track_review_summary", return_value="🔎 track summary") as track, \
             patch.object(sidecar, "approval_rejection_summary") as approval:
            result = sidecar.admin_context_for("please approve UPC 795005370745", 1000)
        self.assertEqual(result, "🔎 track summary")
        track.assert_called_once()
        approval.assert_not_called()

    def test_admin_context_for_falls_back_to_approval_summary(self):
        with patch.object(sidecar, "track_review_summary", return_value=None), \
             patch.object(sidecar, "approval_rejection_summary", return_value="🚫 rejected reason"):
            result = sidecar.admin_context_for("please approve UPC 795005370745", 1000)
        self.assertEqual(result, "🚫 rejected reason")

    def test_admin_context_for_surfaces_lookup_error(self):
        with patch.object(sidecar, "track_review_summary", side_effect=sidecar.AdminLookupError("gateway blocked")):
            result = sidecar.admin_context_for("UPC 795005370945", 1000)
        self.assertIn("Admin lookup unavailable", result)
        self.assertIn("gateway blocked", result)

    def _candidate(self, mid="om_1", text="please approve UPC 795005370745", chat_name="Moderação BR", chat_id="oc_1"):
        return {
            "message_id": mid,
            "text": text,
            "sender": "Someone",
            "chat_id": chat_id,
            "chat_name": chat_name,
            "message": {"create_time": "1000"},
        }

    def test_run_once_posts_dm_and_records_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch.object(sidecar, "STATE_FILE", state_path), \
                 patch.object(sidecar, "fetch_unreacted_candidates", return_value=([self._candidate()], {})), \
                 patch.object(sidecar, "token", return_value="tok"), \
                 patch.object(sidecar, "_resolve_owner_open_id", return_value="ou_owner"), \
                 patch.object(sidecar, "admin_context_for", return_value="🚫 **Admin rejection reason:** bad artwork"), \
                 patch.object(sidecar, "_dm") as dm, \
                 patch.object(sidecar, "reply") as group_reply:
                summary = sidecar.run_once(output="dm")

            self.assertEqual(summary["posted"], 1)
            self.assertEqual(summary["errors"], 0)
            dm.assert_called_once()
            group_reply.assert_not_called()
            self.assertTrue(state_path.exists())
            state = json.loads(state_path.read_text())
            self.assertEqual(state["om_1"]["result"], "posted")

    def test_run_once_skips_when_still_blocked_and_does_not_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch.object(sidecar, "STATE_FILE", state_path), \
                 patch.object(sidecar, "fetch_unreacted_candidates", return_value=([self._candidate()], {})), \
                 patch.object(sidecar, "token", return_value="tok"), \
                 patch.object(sidecar, "_resolve_owner_open_id", return_value="ou_owner"), \
                 patch.object(sidecar, "admin_context_for", return_value="Admin lookup unavailable: gateway/network segregation"), \
                 patch.object(sidecar, "_dm") as dm:
                summary = sidecar.run_once(output="dm")

            self.assertEqual(summary["posted"], 0)
            self.assertEqual(summary["skipped_still_blocked"], 1)
            dm.assert_not_called()
            # Nothing cached — a still-blocked result must be retried next run,
            # unlike a genuinely empty ("no context to add") result.
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            self.assertNotIn("om_1", state)

    def test_run_once_second_pass_skips_already_posted(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch.object(sidecar, "STATE_FILE", state_path), \
                 patch.object(sidecar, "fetch_unreacted_candidates", return_value=([self._candidate()], {})), \
                 patch.object(sidecar, "token", return_value="tok"), \
                 patch.object(sidecar, "_resolve_owner_open_id", return_value="ou_owner"), \
                 patch.object(sidecar, "admin_context_for", return_value="🚫 **Admin rejection reason:** bad artwork"), \
                 patch.object(sidecar, "_dm") as dm:
                sidecar.run_once(output="dm")
                second = sidecar.run_once(output="dm")

            self.assertEqual(second["posted"], 0)
            self.assertEqual(second["skipped_cached"], 1)
            dm.assert_called_once()  # only from the first pass

    def test_run_once_group_mode_replies_in_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch.object(sidecar, "STATE_FILE", state_path), \
                 patch.object(sidecar, "fetch_unreacted_candidates", return_value=([self._candidate()], {})), \
                 patch.object(sidecar, "token", return_value="tok"), \
                 patch.object(sidecar, "admin_context_for", return_value="🚫 **Admin rejection reason:** bad artwork"), \
                 patch.object(sidecar, "reply") as group_reply, \
                 patch.object(sidecar, "_dm") as dm:
                summary = sidecar.run_once(output="group")

            self.assertEqual(summary["posted"], 1)
            group_reply.assert_called_once_with("oc_1", "om_1", sidecar._format_group_reply("🚫 **Admin rejection reason:** bad artwork"))
            dm.assert_not_called()

    def test_run_once_dry_run_does_not_post_or_write_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch.object(sidecar, "STATE_FILE", state_path), \
                 patch.object(sidecar, "fetch_unreacted_candidates", return_value=([self._candidate()], {})), \
                 patch.object(sidecar, "token", return_value="tok"), \
                 patch.object(sidecar, "admin_context_for", return_value="🚫 **Admin rejection reason:** bad artwork"), \
                 patch.object(sidecar, "_dm") as dm:
                summary = sidecar.run_once(output="dm", dry_run=True)

            self.assertEqual(summary["posted"], 1)
            dm.assert_not_called()
            self.assertFalse(state_path.exists())

    def test_run_once_rejects_unsupported_output(self):
        with self.assertRaises(ValueError):
            sidecar.run_once(output="webhook")


if __name__ == "__main__":
    unittest.main()
