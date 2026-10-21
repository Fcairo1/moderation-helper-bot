import unittest
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


if __name__ == "__main__":
    unittest.main()
