import unittest
from unittest.mock import Mock, patch

import requests

from moderation_bot import admin_client


class AdminClientTests(unittest.TestCase):
    def test_extracts_supported_identifiers(self):
        text = "Please approve albumId=7677777270908438545 UPC 795005370745 and ISRC BRABC2400001"
        self.assertEqual(
            admin_client.extract_identifiers(text),
            [
                ("album", "7677777270908438545"),
                ("upc", "795005370745"),
                ("isrc", "BRABC2400001"),
            ],
        )

    def test_plain_identifiers_are_lookup_requests(self):
        self.assertTrue(admin_client.looks_like_lookup("795005370745"))
        self.assertTrue(admin_client.looks_like_lookup("album ID 7677777270908438545"))
        self.assertFalse(admin_client.looks_like_lookup("please approve UPC 795005370745"))

    @patch.object(admin_client, "_reason_translations", return_value={"40003": "Unauthorized sample"})
    def test_translates_reason_codes(self, _catalog):
        self.assertEqual(admin_client.translate_reasons(["40003", "40003", "99999"]), ["Unauthorized sample", "Rejection reason code 99999"])

    @patch.object(admin_client, "lookup")
    def test_approval_summary_contains_rejection_reason(self, lookup):
        lookup.return_value = {
            "found": True,
            "record": {"releaseStatus": 20},
            "rejectionReasons": ["Unauthorized sample"],
        }
        summary = admin_client.approval_rejection_summary("Please approve UPC 795005370745")
        self.assertIn("Unauthorized sample", summary)

    def test_non_approval_has_no_card_enrichment(self):
        self.assertIsNone(admin_client.approval_rejection_summary("Artwork issue UPC 795005370745"))

    def test_review_status_mapping(self):
        self.assertEqual(admin_client.review_status({"releaseStatus": 30})[0], "approved")
        self.assertEqual(admin_client.review_status({"releaseStatus": 10})[0], "under_review")
        self.assertEqual(admin_client.review_status({"releaseStatus": 0})[0], "to_be_reviewed")
        self.assertEqual(admin_client.review_status({"releaseStatus": 20})[0], "not_approved")
        self.assertEqual(admin_client.review_status({"status": 2})[0], "approved")
        self.assertEqual(admin_client.review_status({"status": 1})[0], "under_review")
        self.assertEqual(admin_client.review_status({"status": 0})[0], "to_be_reviewed")
        self.assertEqual(admin_client.review_status({"status": 3})[0], "not_approved")

    @patch.object(admin_client, "lookup")
    def test_recent_to_be_reviewed_track_is_reaching_queue(self, lookup):
        submitted_at = 1_787_656_160
        lookup.return_value = {
            "found": True,
            "kind": "song",
            "region": "BR",
            "record": {
                "songId": "7677923664977479696",
                "releaseStatus": 0,
                "submitTime": submitted_at,
            },
        }
        summary = admin_client.track_review_summary(
            "Please moderate track songId=7677923664977479696",
            (submitted_at + 12 * 60 * 60) * 1000,
        )
        self.assertIn("Reaching Queue", summary)

    @patch.object(admin_client, "lookup")
    def test_old_to_be_reviewed_track_flags_queue_issue(self, lookup):
        submitted_at = 1_787_656_160
        lookup.return_value = {
            "found": True,
            "kind": "song",
            "region": "BR",
            "record": {
                "songId": "7677923664977479696",
                "releaseStatus": 0,
                "submitTime": submitted_at,
            },
        }
        summary = admin_client.track_review_summary(
            "Please moderate track songId=7677923664977479696",
            (submitted_at + 25 * 60 * 60) * 1000,
        )
        self.assertIn("Not sent to the queue", summary)

    @patch.object(admin_client, "lookup")
    def test_approved_track_adds_no_card_context(self, lookup):
        lookup.return_value = {
            "found": True,
            "kind": "song",
            "region": "BR",
            "record": {"songId": "7677923664977479696", "releaseStatus": 30},
        }
        self.assertIsNone(
            admin_client.track_review_summary("Approve track songId=7677923664977479696")
        )

    @patch.object(admin_client, "latest_moderation_operation")
    @patch.object(admin_client, "lookup")
    def test_not_approved_uses_latest_operation_reason(self, lookup, latest):
        lookup.return_value = {
            "found": True,
            "kind": "song",
            "region": "BR",
            "record": {"songId": "7677923664977479696", "releaseStatus": 20},
            "rejectionReasons": ["stale reason"],
        }
        latest.return_value = {
            "action": "rejected",
            "level": "album",
            "reasons": ["latest album reason"],
        }
        summary = admin_client.track_review_summary("Rejected track songId=7677923664977479696")
        self.assertIn("latest album reason", summary)
        self.assertNotIn("stale reason", summary)

    @patch.object(admin_client, "_curl_transport", return_value=(200, '{"ok":true}'))
    @patch.object(admin_client, "_requests_transport", side_effect=requests.ConnectionError("blocked"))
    def test_auto_transport_falls_back_to_native_curl(self, _requests, curl):
        with patch.object(admin_client, "ADMIN_TRANSPORT", "auto"):
            status, body, transport = admin_client._send_transport("https://admin.test", "secret", "BR")
        self.assertEqual((status, body, transport), (200, '{"ok":true}', "curl"))
        curl.assert_called_once()

    @patch.object(admin_client.subprocess, "run")
    def test_curl_token_is_passed_over_stdin_not_process_arguments(self, run):
        run.return_value = Mock(stdout='{"ok":true}\n200', returncode=0)
        status, body = admin_client._curl_transport("https://admin.test", "sensitive-token", "BR")
        argv = run.call_args.args[0]
        self.assertNotIn("sensitive-token", " ".join(argv))
        self.assertIn("sensitive-token", run.call_args.kwargs["input"])
        self.assertEqual((status, body), (200, '{"ok":true}'))


if __name__ == "__main__":
    unittest.main()
