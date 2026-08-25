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

    @patch.object(triage, "track_review_summary", return_value=None)
    @patch.object(triage, "approval_rejection_summary", return_value=None)
    def test_four_track_card_does_not_duplicate_message(self, _approval, _track):
        ids = (
            "7677923664977479696",
            "7677931739407648769",
            "7677950441648654337",
            "7677936442996606977",
        )
        nodes = [{"tag": "text", "text": "Pode moderar essas faixas?"}]
        for song_id in ids:
            url = f"https://sg-musician-admin.bytedance.net/avenue/content/song/new?songId={song_id}"
            nodes.append({"tag": "a", "text": url, "href": url})
        rich_message = {
            "message_id": "om_four_tracks",
            "create_time": "1787621895000",
            "body": {"content": json.dumps({"content": [nodes]})},
        }
        card = triage.build_card(rich_message, "Requester", "Moderação BR")
        rendered = "\n".join(
            (element.get("text") or {}).get("content", "")
            for element in card["elements"]
            if element.get("tag") == "div"
        )
        for song_id in ids:
            self.assertEqual(rendered.count(song_id), 1)


if __name__ == "__main__":
    unittest.main()
