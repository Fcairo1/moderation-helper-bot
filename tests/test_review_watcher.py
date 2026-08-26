import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moderation_bot import review_watcher


class ReviewWatcherTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tempdir.name) / "review_watch_state.json"
        self.state_patch = patch.object(review_watcher, "STATE_FILE", self.state_file)
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.tempdir.cleanup()

    @patch.object(review_watcher, "lookup")
    def test_waiting_track_is_registered(self, lookup):
        lookup.return_value = {
            "found": True,
            "kind": "song",
            "region": "BR",
            "record": {"songId": "7677923664977479696", "title": "Waiting Track", "status": 0},
        }
        count = review_watcher.register_review_watches(
            "Please moderate songId=7677923664977479696",
            "om_source",
            "om_card",
            "Moderação BR",
        )
        self.assertEqual(count, 1)
        self.assertIn("7677923664977479696", review_watcher.active_watches())

    @patch.object(review_watcher, "lookup")
    def test_transition_to_under_review_alerts_once_and_removes_watch(self, lookup):
        lookup.side_effect = [
            {
                "found": True,
                "kind": "song",
                "region": "BR",
                "record": {"songId": "7677923664977479696", "title": "Waiting Track", "status": 0},
            },
            {
                "found": True,
                "kind": "song",
                "region": "BR",
                "record": {"songId": "7677923664977479696", "title": "Waiting Track", "status": 1},
            },
        ]
        with patch.object(review_watcher, "POLL_SECONDS", 60):
            review_watcher.register_review_watches(
                "Please moderate songId=7677923664977479696",
                "om_source",
                "om_card",
                "Moderação BR",
            )
        entry = review_watcher.active_watches()["7677923664977479696"]
        alerts = []
        stats = review_watcher.poll_once(
            lambda watch, result: alerts.append((watch, result)),
            now=entry["nextCheckAt"] + 1,
        )
        self.assertEqual(stats["alerted"], 1)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(review_watcher.active_watches(), {})

    @patch.object(review_watcher, "lookup")
    def test_approved_track_is_not_registered(self, lookup):
        lookup.return_value = {
            "found": True,
            "kind": "song",
            "region": "BR",
            "record": {"songId": "7677923664977479696", "status": 2},
        }
        count = review_watcher.register_review_watches(
            "Approve songId=7677923664977479696",
            "om_source",
            "om_card",
            "Moderação BR",
        )
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
