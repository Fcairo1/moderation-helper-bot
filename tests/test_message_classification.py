import json
import unittest

from moderation_bot.admin_client import extract_identifiers
from moderation_bot.command_handler import is_forced_request, is_probable_request, msg_text


class MessageClassificationTests(unittest.TestCase):
    def test_approval_variants_are_forced_requests(self):
        for text in (
            "Please approve this track",
            "Consegue aprovar esse lançamento?",
            "Pode moderar essas faixas?",
            "The song was rejected",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_forced_request(text))
                self.assertTrue(is_probable_request(text))

    def test_rich_post_does_not_duplicate_links_or_locales(self):
        content = json.dumps(
            {
                "en_us": {
                    "title": "",
                    "content": [[
                        {"tag": "text", "text": "Please moderate these tracks:"},
                        {
                            "tag": "a",
                            "text": "song link",
                            "href": "https://sg-musician-admin.bytedance.net/avenue/content/song/new?songId=7677923664977479696",
                        },
                    ]],
                },
                "pt_br": {
                    "content": [[{"tag": "text", "text": "duplicate locale"}]],
                },
            }
        )
        rendered = msg_text(content)
        self.assertEqual(rendered.count("7677923664977479696"), 1)
        self.assertNotIn("duplicate locale", rendered)

    def test_link_node_uses_one_value(self):
        content = json.dumps(
            {
                "content": [[{
                    "tag": "a",
                    "text": "https://example.test/?songId=7677923664977479696",
                    "href": "https://example.test/?songId=7677923664977479696",
                }]]
            }
        )
        self.assertEqual(msg_text(content).count("7677923664977479696"), 1)

    def test_four_track_post_extracts_each_song_once(self):
        song_ids = (
            "7677923664977479696",
            "7677931739407648769",
            "7677950441648654337",
            "7677936442996606977",
        )
        nodes = [{"tag": "text", "text": "Pode moderar essas faixas?"}]
        for song_id in song_ids:
            url = f"https://sg-musician-admin.bytedance.net/avenue/content/song/new?aop_region=BR&songId={song_id}"
            nodes.append({"tag": "a", "text": url, "href": url})
        rendered = msg_text(json.dumps({"content": [nodes]}))
        for song_id in song_ids:
            self.assertEqual(rendered.count(song_id), 1)
        self.assertEqual(
            extract_identifiers(rendered),
            [("song", song_id) for song_id in song_ids],
        )


if __name__ == "__main__":
    unittest.main()
