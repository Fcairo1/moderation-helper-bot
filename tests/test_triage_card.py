import json
import unittest
from unittest.mock import patch

import scan_send_modbr_triage as triage


def message(text):
    return {
        "message_id": "om_test",
        "create_time": "1787621895000",
        "body": {"content": json.dumps({"text": text}, ensure_ascii=False)},
    }


class TriageCardTests(unittest.TestCase):
    @patch.object(triage, "approval_rejection_summary", return_value="🚫 **Admin rejection reason:** Unauthorized sample")
    def test_approval_card_includes_admin_reason(self, _summary):
        card = triage.build_card(message("Please approve UPC 795005370745"), "Requester", "BR")
        contents = [
            (element.get("text") or {}).get("content", "")
            for element in card["elements"]
            if element.get("tag") == "div"
        ]
        self.assertTrue(any("Unauthorized sample" in content for content in contents))

    @patch.object(triage, "approval_rejection_summary", return_value=None)
    def test_non_approval_card_stays_compact(self, _summary):
        card = triage.build_card(message("Artwork issue UPC 795005370745"), "Requester", "BR")
        contents = [
            (element.get("text") or {}).get("content", "")
            for element in card["elements"]
            if element.get("tag") == "div"
        ]
        self.assertFalse(any("Admin rejection reason" in content for content in contents))


if __name__ == "__main__":
    unittest.main()
