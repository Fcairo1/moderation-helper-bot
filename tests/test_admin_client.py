import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
