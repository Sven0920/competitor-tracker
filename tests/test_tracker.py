"""不访问 Google Play、iTunes 或飞书的单元测试。"""
import os
import unittest

import competitor_tracker as ct


class RegionTrackingTest(unittest.TestCase):
    def test_merge_keeps_regions_missed_this_run(self):
        known = {"app": {"name": "Soft", "regions": ["tr", "ph"]}}
        sightings = {}
        ct.note_known_sighting(sightings, "app", "tr", "iOS", "Soft", "https://example/app", "Homa")
        expansions, updates = ct.apply_region_sightings(known, sightings)
        self.assertEqual(expansions, [])
        self.assertEqual(updates["app"], ["ph", "tr"])
        self.assertEqual(known["app"]["regions"], ["ph", "tr"])

    def test_new_core_market_is_expansion_not_a_new_game(self):
        known = {"app": {"name": "Soft", "regions": ["tr"]}}
        sightings = {}
        ct.note_known_sighting(sightings, "app", "us", "Android", "Soft", "https://play/app", "Peak")
        ct.note_known_sighting(sightings, "app", "tr", "Android", "Soft", "https://play/app", "Peak")
        expansions, updates = ct.apply_region_sightings(known, sightings)
        self.assertEqual(len(expansions), 1)
        self.assertEqual(expansions[0]["new_core"], ["us"])
        self.assertEqual(expansions[0]["platform"], "Android")
        self.assertEqual(updates["app"], ["tr", "us"])

    def test_another_core_market_alerts_again(self):
        known = {"app": {"name": "Live", "regions": ["us", "tr"]}}
        sightings = {}
        ct.note_known_sighting(sightings, "app", "gb", "iOS", "Live", "https://apple/app", "Voodoo")
        expansions, _ = ct.apply_region_sightings(known, sightings)
        self.assertEqual(expansions[0]["new_core"], ["gb"])

    def test_test_market_only_does_not_alert(self):
        known = {"app": {"name": "Soft", "regions": ["tr"]}}
        sightings = {}
        ct.note_known_sighting(sightings, "app", "br", "iOS", "Soft", "https://apple/app", "Homa")
        expansions, updates = ct.apply_region_sightings(known, sightings)
        self.assertEqual(expansions, [])
        self.assertEqual(updates["app"], ["br", "tr"])

    def test_board_regions_follow_the_baseline(self):
        games = [{"app_id": "app", "regions": ["tr"], "name": "Soft"}]
        ct.apply_region_updates(games, {"app": ["tr", "us"], "missing": ["us"]})
        self.assertEqual(games[0]["regions"], ["tr", "us"])

    def test_expansion_payload_is_separate_from_new_games(self):
        payload = ct.build_region_expansion_payload([{
            "developer": "Peak",
            "platform": "Android",
            "name": "Soft",
            "url": "https://play/app",
            "regions": ["tr", "us"],
            "new_core": ["us"],
        }])
        title = payload["card"]["header"]["title"]["content"]
        body = payload["card"]["elements"][0]["content"]
        self.assertIn("地区扩大", title)
        self.assertIn("新进 US", body)
        self.assertNotIn("发现", title)

    def test_no_feishu_post_without_webhook(self):
        os.environ.pop("FEISHU_WEBHOOK", None)
        self.assertFalse(ct.post_feishu({"msg_type": "text"}, "should-not-print"))


if __name__ == "__main__":
    unittest.main()
