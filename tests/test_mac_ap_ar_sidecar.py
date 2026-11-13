import unittest
from datetime import datetime
from unittest.mock import patch

import mac_ap_ar_sidecar as sidecar


class SummaryBucketTests(unittest.TestCase):
    def test_takedown_by_content_safety_is_not_false_positive(self):
        row = {"upc": "5064080316661", "title": "A", "taken_down": True,
               "reason": "CONTENT_SAFETY_VIOLATION", "notes": "—", "removed_by": "system"}
        self.assertNotEqual(sidecar._summary_bucket(row), "🟡 False positive")
        self.assertEqual(sidecar._summary_bucket(row), sidecar.OTHER_TAKEDOWN_BUCKET)

    def test_takedown_with_empty_reason_is_not_false_positive(self):
        row = {"upc": "1", "title": "A", "taken_down": True, "reason": "—", "notes": "—", "removed_by": "system"}
        self.assertEqual(sidecar._summary_bucket(row), sidecar.OTHER_TAKEDOWN_BUCKET)

    def test_no_takedown_evidence_is_false_positive(self):
        row = {"upc": "1", "title": "A", "taken_down": False, "reason": "—",
               "notes": "No takedown operation found", "removed_by": "—"}
        self.assertEqual(sidecar._summary_bucket(row), "🟡 False positive")

    def test_existing_categories_still_apply_to_takedowns(self):
        base = {"upc": "1", "title": "A", "taken_down": True, "removed_by": "x"}
        self.assertEqual(sidecar._summary_bucket({**base, "reason": "INFRINGEMENT_CONTENT", "notes": "—"}), "🔴 Infringing content")
        self.assertEqual(sidecar._summary_bucket({**base, "reason": "User request", "notes": "—"}), "🔵 User Request / User Auto-processed")
        self.assertEqual(sidecar._summary_bucket({**base, "reason": "Confirm TD", "notes": "—"}), "🟣 Confirmed takedown")

    def test_content_safety_claim_is_infringing_content(self):
        base = {"upc": "1", "title": "A", "taken_down": True, "removed_by": "system", "notes": "—"}
        for reason in ("content_safety_claim_v2", "CONTENT_SAFETY_CLAIM_V2", "Content Safety Claim"):
            self.assertEqual(sidecar._summary_bucket({**base, "reason": reason}), "🔴 Infringing content")
        # should also match if it's in removed_by
        self.assertEqual(sidecar._summary_bucket({"upc": "1", "title": "A", "taken_down": True, "removed_by": "content_safety_claim_v2", "reason": "NORMAL", "notes": "—"}), "🔴 Infringing content")
        # other content-safety reasons are still uncategorised
        self.assertEqual(sidecar._summary_bucket({**base, "reason": "CONTENT_SAFETY_VIOLATION"}), sidecar.OTHER_TAKEDOWN_BUCKET)

    def test_user_self_processed_takedown_goes_to_user_request_bucket(self):
        # Real Admin example: operator is the user's UID, reason NORMAL, notes
        # start with "Auto-processed user request" followed by the user's text.
        row = {"upc": "1", "title": "A", "taken_down": True, "removed_by": "7213961611492852737", "reason": "NORMAL",
               "notes": 'Auto-processed user request: "Outros Estou expandindo meu catálogo de músicas. (mais profissional), userEmail: x@gmail.com"'}
        self.assertEqual(sidecar._summary_bucket(row), "🔵 User Request / User Auto-processed")
        # free text typed by the user must not push it into another bucket
        row["notes"] = 'Auto-processed user request: "this is not an infringement, content safety claim"'
        self.assertEqual(sidecar._summary_bucket(row), "🔵 User Request / User Auto-processed")

    def test_card_rows_exclude_internal_fields(self):
        row = {"upc": "1", "title": "A", "removed_by": "x", "date_brt": "d", "reason": "r", "notes": "n", "platforms": "2", "taken_down": True}
        card = sidecar._build_card([row], "20 Oct 2026 10:00 BRT", "om_1")
        table = next(el for el in card["body"]["elements"] if el.get("tag") == "table")
        self.assertNotIn("taken_down", table["rows"][0])
        self.assertEqual(set(table["rows"][0]), {"upc", "title", "removed_by", "date_brt", "reason", "notes", "platforms"})

    def test_summary_shows_reason_and_operator_for_other_takedowns(self):
        rows = [
            {"upc": "5064080316661", "title": "Song A", "taken_down": True, "reason": "CONTENT_SAFETY_VIOLATION", "notes": "—", "removed_by": "system"},
            {"upc": "2", "title": "Song B", "taken_down": False, "reason": "—", "notes": "No takedown operation found", "removed_by": "—"},
        ]
        text = sidecar._build_summary_text(rows, "20 Oct 2026 10:00 BRT")
        self.assertIn("🟠 Other takedown\n• 5064080316661 - Song A (CONTENT_SAFETY_VIOLATION / system)", text)
        self.assertIn("🟡 False positive\n• 2 - Song B", text)


class LookupTakedownRowTests(unittest.TestCase):
    def test_status_takedown_without_audit_entry_counts_as_taken_down(self):
        record = {"title": "Song", "albumId": "1", "releaseStatus": 60}
        with patch.object(sidecar, "lookup", return_value={"found": True, "record": record}), \
             patch.object(sidecar, "_latest_takedown_context", return_value={}):
            row = sidecar._lookup_takedown_row("672079650327")
        self.assertTrue(row["taken_down"])
        self.assertNotEqual(sidecar._summary_bucket(row), "🟡 False positive")

    def test_live_release_without_takedown_entry_is_false_positive(self):
        record = {"title": "Song", "albumId": "1", "releaseStatus": 50}
        with patch.object(sidecar, "lookup", return_value={"found": True, "record": record}), \
             patch.object(sidecar, "_latest_takedown_context", return_value={}), \
             patch.object(sidecar, "log"):
            row = sidecar._lookup_takedown_row("3")
        self.assertFalse(row["taken_down"])
        self.assertEqual(sidecar._summary_bucket(row), "🟡 False positive")

    def test_takedown_entry_marks_row_taken_down(self):
        record = {"title": "Song", "albumId": "1", "releaseStatus": 60}
        context = {"time": 1790792095.0, "operator": "system", "reason": "SOMETHING_ELSE", "notes": "", "stores": [1, 4]}
        with patch.object(sidecar, "lookup", return_value={"found": True, "record": record}), \
             patch.object(sidecar, "_latest_takedown_context", return_value=context):
            row = sidecar._lookup_takedown_row("4")
        self.assertTrue(row["taken_down"])
        self.assertEqual(sidecar._summary_bucket(row), sidecar.OTHER_TAKEDOWN_BUCKET)


class TemporaryRecipientTests(unittest.TestCase):
    def _message(self, *, message_id: str = "om_1", create_time: str = "1760515200000") -> dict:
        return {
            "message_id": message_id,
            "content": "New Deleted AP/AR Releases\n### BR\n| 123456789012 | BR |",
            "create_time": create_time,
        }

    def _row(self) -> dict:
        return {
            "upc": "123456789012",
            "title": "Album",
            "removed_by": "system",
            "date_brt": "14 Oct 2026 10:00",
            "reason": "INFRINGEMENT_CONTENT",
            "notes": "—",
            "platforms": "2",
            "taken_down": True,
        }

    def test_temp_recipient_cutoff_is_inclusive_through_30_nov_brt(self):
        self.assertTrue(sidecar._temp_recipient_is_active(datetime(2026, 11, 30, 23, 59, tzinfo=sidecar.BRT)))
        self.assertFalse(sidecar._temp_recipient_is_active(datetime(2026, 12, 1, 0, 0, tzinfo=sidecar.BRT)))

    def test_run_once_sends_only_missing_recipient(self):
        message = self._message(create_time="1760515200000")
        digest = sidecar._text_hash(message["content"])
        state = {
            sidecar.STATE_META_KEY: {sidecar.SECONDARY_RECIPIENT_ROLLOUT_STARTED_AT_KEY: "2025-10-14T00:00:00+00:00"},
            "om_1": {
                "hash": digest,
                "result": "posted",
                "card_message_id": "owner-card",
                "summary_message_id": "owner-summary",
            },
        }
        send_calls = []

        def fake_send_card(_token, open_id, _card):
            send_calls.append(("card", open_id))
            return {"data": {"message_id": f"{open_id}-card"}}

        def fake_send_text(_token, open_id, _text):
            send_calls.append(("summary", open_id))
            return {"data": {"message_id": f"{open_id}-summary"}}

        with patch.object(sidecar, "_fetch_recent_alert_messages", return_value=[message]), \
             patch.object(sidecar, "_load_state", return_value=state), \
             patch.object(sidecar, "_save_state") as save_state, \
             patch.object(sidecar, "token", return_value="token"), \
             patch.object(sidecar, "_resolve_owner_open_id", return_value="owner_open"), \
             patch.object(sidecar, "_resolve_temp_ap_ar_recipient_open_id", return_value="temp_open"), \
             patch.object(sidecar, "_lookup_takedown_row", return_value=self._row()), \
             patch.object(sidecar, "_send_dm_card", side_effect=fake_send_card), \
             patch.object(sidecar, "_send_dm_text", side_effect=fake_send_text), \
             patch.object(sidecar, "_current_brt_datetime", return_value=datetime(2026, 10, 15, 12, 0, tzinfo=sidecar.BRT)), \
             patch.object(sidecar.time, "sleep"):
            sidecar.run_once(page_size=1, dry_run=False)

        self.assertEqual(send_calls, [("card", "temp_open"), ("summary", "temp_open")])
        saved_state = save_state.call_args.args[0]
        recipients = saved_state["om_1"]["recipients"]
        self.assertEqual(recipients[sidecar.OWNER_RECIPIENT_KEY]["card"]["message_id"], "owner-card")
        self.assertEqual(recipients[sidecar.OWNER_RECIPIENT_KEY]["summary"]["message_id"], "owner-summary")
        self.assertEqual(recipients[sidecar.TEMP_AP_AR_RECIPIENT_KEY]["card"]["message_id"], "temp_open-card")
        self.assertEqual(recipients[sidecar.TEMP_AP_AR_RECIPIENT_KEY]["summary"]["message_id"], "temp_open-summary")

    def test_temp_recipient_failure_does_not_block_owner(self):
        message = self._message()
        saved_states = []

        def fake_send_card(_token, open_id, _card):
            if open_id == "temp_open":
                raise RuntimeError("temp card failed")
            return {"data": {"message_id": "owner-card"}}

        def fake_send_text(_token, open_id, _text):
            if open_id == "temp_open":
                raise RuntimeError("temp summary failed")
            return {"data": {"message_id": "owner-summary"}}

        with patch.object(sidecar, "_fetch_recent_alert_messages", return_value=[message]), \
             patch.object(sidecar, "_load_state", return_value={sidecar.STATE_META_KEY: {sidecar.SECONDARY_RECIPIENT_ROLLOUT_STARTED_AT_KEY: "2025-10-14T00:00:00+00:00"}}), \
             patch.object(sidecar, "_save_state", side_effect=lambda state: saved_states.append(state)), \
             patch.object(sidecar, "token", return_value="token"), \
             patch.object(sidecar, "_resolve_owner_open_id", return_value="owner_open"), \
             patch.object(sidecar, "_resolve_temp_ap_ar_recipient_open_id", return_value="temp_open"), \
             patch.object(sidecar, "_lookup_takedown_row", return_value=self._row()), \
             patch.object(sidecar, "_send_dm_card", side_effect=fake_send_card), \
             patch.object(sidecar, "_send_dm_text", side_effect=fake_send_text), \
             patch.object(sidecar, "_current_brt_datetime", return_value=datetime(2026, 10, 15, 12, 0, tzinfo=sidecar.BRT)), \
             patch.object(sidecar, "log") as log_mock, \
             patch.object(sidecar.time, "sleep"):
            summary = sidecar.run_once(page_size=1, dry_run=False)

        self.assertEqual(summary["posted"], 1)
        state = saved_states[-1]
        owner_state = state["om_1"]["recipients"][sidecar.OWNER_RECIPIENT_KEY]
        temp_state = state["om_1"]["recipients"][sidecar.TEMP_AP_AR_RECIPIENT_KEY]
        self.assertEqual(owner_state["card"]["status"], "sent")
        self.assertEqual(owner_state["summary"]["status"], "sent")
        self.assertEqual(temp_state["card"]["status"], "failed")
        self.assertEqual(temp_state["summary"]["status"], "failed")
        self.assertTrue(any("temp_ap_ar_recipient" in str(call.args[0]) or sidecar.TEMP_AP_AR_RECIPIENT_NAME in str(call.args[0]) for call in log_mock.call_args_list))


if __name__ == "__main__":
    unittest.main()
