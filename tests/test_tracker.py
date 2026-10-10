"""不访问 Google Play、iTunes 或飞书的单元测试。"""
import json
import os
import tempfile
import unittest
from unittest import mock
from urllib.parse import unquote

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


def _play_script(key, data):
    payload = json.dumps(data)
    # 真实页面是 sideChannel: {}});  多一个 } 就匹配不上库里的正则
    suffix = "sideChannel: " + "{}}" + ");</script>"
    return f"<script>AF_initDataCallback({{key: '{key}', hash: '1', data:{payload}, {suffix}"


def _wrapped_app(app_id, title, developer, icon="https://example/icon"):
    # 首屏卡片外面还有一层列表，字段在内层：id 在 [0][0][0]
    record = [None] * 15
    record[0] = [app_id]
    record[1] = [None, None, None, [None, None, icon]]
    record[3] = title
    record[14] = developer
    return [record]


def _developer_ds(apps, token):
    token_slot = [None, None, None, [None, token]]
    cluster = [apps, token_slot]
    level0 = [None] * 23
    level0[22] = cluster
    return [[None, [level0]]]


def _flat_app(app_id, title, developer):
    item = [None] * 13
    item[2] = title
    item[4] = [[[developer]]]
    item[12] = [app_id]
    return item


def _pagination_raw(apps, token):
    page = [None] * 8
    page[0] = apps
    page[7] = [None, token] if token else []
    inner = [[page]]
    outer = [["wrb.fr", "qnKhOb", json.dumps(inner), None, None]]
    return ")]}'\n" + json.dumps(outer)


class FreshReleaseTest(unittest.TestCase):
    def test_recent_and_preorder_are_not_backfill(self):
        now = ct.datetime(2026, 10, 10, tzinfo=ct.timezone.utc)
        self.assertFalse(ct.is_backfill_release("2026-10-01", now=now))
        self.assertFalse(ct.is_backfill_release("Oct 20, 2026", now=now))
        self.assertFalse(ct.is_backfill_release("2026-09-11", now=now))  # 正好 29 天

    def test_old_and_undated_are_backfill_but_ancient_stays_off_the_board(self):
        now = ct.datetime(2026, 10, 10, tzinfo=ct.timezone.utc)
        self.assertTrue(ct.is_backfill_release("2026-08-01", now=now))
        self.assertTrue(ct.is_backfill_release(None, now=now))
        self.assertTrue(ct.is_backfill_release("未知日期", now=now))
        self.assertTrue(ct.is_stale_release("2020-01-01"))
        self.assertFalse(ct.is_stale_release(None))
        self.assertFalse(ct.is_stale_release("未知日期"))

    def test_feishu_skips_backfill(self):
        records = [
            {"developer": "Homa", "platform": "iOS", "name": "New", "url": "https://a", "regions": ["us"], "genre": "Puzzle", "backfill": False},
            {"developer": "Homa", "platform": "Android", "name": "Old", "url": "https://b", "regions": ["tr"], "backfill": True},
            {"developer": "Peak", "platform": "Android", "name": "Undated", "url": "https://c", "regions": ["ph"], "backfill": True},
        ]
        payload = ct.build_new_games_payload(records)
        body = payload["card"]["elements"][0]["content"]
        self.assertIn("New", body)
        self.assertNotIn("Old", body)
        self.assertNotIn("Undated", body)
        self.assertIn("发现 1 款", payload["card"]["header"]["title"]["content"])
        self.assertIsNone(ct.build_new_games_payload([records[1]]))


class StorePriceAndRatingsTest(unittest.TestCase):
    def test_us_listing_keeps_dollar_price(self):
        def fake_app(app_id, lang, country):
            self.assertEqual(country, "us")
            return {"inAppProductPrice": "$0.99 - $4.99 per item", "realInstalls": 8, "installs": "10+"}

        with mock.patch.object(ct, "app", fake_app):
            details, iap = ct.android_store_details("com.x", ["ph"], ct.PlayGuard())
        self.assertEqual(iap, "$0.99 - $4.99 per item")
        self.assertEqual(details["realInstalls"], 8)

    def test_missing_us_listing_is_marked_and_does_not_trip_breaker(self):
        def fake_app(app_id, lang, country):
            if country == "us":
                raise ct.NotFoundError("no us listing")
            return {"inAppProductPrice": "₱135.00 - ₱6,850.00 per item", "realInstalls": 2, "installs": "1+"}

        guard = ct.PlayGuard()
        with mock.patch.object(ct, "app", fake_app):
            _details, iap = ct.android_store_details("com.x", ["ph"], guard)
        self.assertTrue(iap.startswith("非美国商店价"))
        self.assertIn("₱135.00", iap)
        self.assertEqual(guard.failed, 0)

    def test_play_error_does_not_relabel_a_local_price(self):
        def fake_app(app_id, lang, country):
            if country == "us":
                raise RuntimeError("limited")
            return {"inAppProductPrice": "₱10", "realInstalls": 1, "installs": "1+"}

        guard = ct.PlayGuard()
        with mock.patch.object(ct, "with_retry", lambda fn, tries=3, delay=1: fn()):
            with mock.patch.object(ct, "app", fake_app):
                _details, iap = ct.android_store_details("com.x", ["ph"], guard)
        self.assertEqual(iap, "₱10")
        self.assertEqual(guard.failed, 1)

    def test_failed_refresh_keeps_existing_installs_and_price(self):
        data = {"games": [{
            "platform": "Android",
            "app_id": "com.keep",
            "real_installs": 50,
            "installs": "50+",
            "iap_info": "$0.99 - $9.99 per item",
            "found_date": "2026-10-01",
            "regions": ["ph"],
        }]}
        guard = ct.PlayGuard()

        def fake_app(app_id, lang, country):
            raise ct.NotFoundError("gone")

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snaps.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({}, fh)
            with mock.patch.object(ct, "INSTALLS_HISTORY_FILE", path):
                with mock.patch.object(ct, "app", fake_app):
                    ct.update_velocity(data, guard)
        self.assertEqual(data["games"][0]["real_installs"], 50)
        self.assertEqual(data["games"][0]["iap_info"], "$0.99 - $9.99 per item")

    def test_ios_rating_refresh_does_not_guess_iap(self):
        games = [
            {"platform": "iOS", "app_id": "1", "ratings": 0, "iap_info": "未知"},
            {"platform": "iOS", "app_id": "2", "ratings": 4, "iap_info": "未知"},
            {"platform": "Android", "app_id": "a", "ratings": 9, "iap_info": "$1"},
        ]

        class Resp:
            def json(self):
                return {"results": [{
                    "trackId": 1,
                    "userRatingCount": 12,
                    "features": ["iosUniversal"],
                }]}

        updated, failed = ct.refresh_ios_ratings(games, get=lambda url: Resp())
        self.assertEqual((updated, failed), (1, 0))
        self.assertEqual(games[0]["ratings"], 12)
        self.assertEqual(games[0]["iap_info"], "未知")
        self.assertNotIn("features", games[0])
        self.assertEqual(games[1]["ratings"], 4)
        self.assertEqual(games[2]["iap_info"], "$1")

        def down(url):
            raise RuntimeError("itunes down")

        updated, failed = ct.refresh_ios_ratings(games, get=down)
        self.assertEqual(games[0]["ratings"], 12)
        self.assertEqual(failed, 1)


class DeveloperCatalogTest(unittest.TestCase):
    def test_parse_developer_page_and_token(self):
        html = _play_script("ds:3", _developer_ds(
            [_wrapped_app("com.homa.one", "One", "Homa")],
            "T" * 24,
        ))
        apps, token = ct.parse_developer_page(html)
        self.assertEqual(apps[0]["appId"], "com.homa.one")
        self.assertEqual(apps[0]["title"], "One")
        self.assertEqual(apps[0]["developer"], "Homa")
        self.assertEqual(apps[0]["icon"], "https://example/icon")
        self.assertEqual(token, "T" * 24)

    def test_parse_pagination_page(self):
        raw = _pagination_raw([_flat_app("com.homa.two", "Two", "Homa")], "U" * 24)
        apps, token = ct.parse_developer_pagination(raw)
        self.assertEqual([(a["appId"], a["title"], a["developer"]) for a in apps], [
            ("com.homa.two", "Two", "Homa"),
        ])
        self.assertEqual(token, "U" * 24)

    def test_catalog_prefers_developer_page_over_search(self):
        html = _play_script("ds:3", _developer_ds(
            [_wrapped_app("com.homa.one", "One", "Homa")],
            None,
        ))
        searched = []
        apps, source = ct.android_apps_for_country(
            "Homa", "us", ct.PlayGuard(),
            search_fn=lambda name, country: searched.append((name, country)) or [],
            get=lambda url: html,
        )
        self.assertEqual(source, "developer")
        self.assertEqual(searched, [])
        self.assertEqual(apps[0]["appId"], "com.homa.one")
        self.assertIn("id=Homa", ct.developer_page_url("Homa", "us"))

    def test_empty_developer_page_falls_back_to_filtered_search(self):
        html = _play_script("ds:3", _developer_ds([], None))

        def search(name, country):
            return [
                {"appId": "com.homa.mine", "title": "Mine", "developer": "Homa", "icon": ""},
                {"appId": "com.other.noise", "title": "Noise", "developer": "Other Studio", "icon": ""},
            ]

        apps, source = ct.android_apps_for_country(
            "Homa", "ph", ct.PlayGuard(), search_fn=search, get=lambda url: html,
        )
        self.assertEqual(source, "search")
        self.assertEqual([a["appId"] for a in apps], ["com.homa.mine"])

    def test_pagination_failure_keeps_first_page(self):
        html = _play_script("ds:3", _developer_ds(
            [_wrapped_app("com.homa.one", "One", "Homa")],
            "T" * 30,
        ))

        def post(url, body):
            self.assertIn("qnKhOb", url)
            decoded = json.loads(unquote(body[len("f.req="):]))
            self.assertIn("T" * 30, decoded[0][0][1])
            raise RuntimeError("rate limited")

        guard = ct.PlayGuard()
        apps = ct.fetch_developer_catalog("Homa", "us", guard, get=lambda url: html, post=post)
        self.assertEqual([a["appId"] for a in apps], ["com.homa.one"])
        self.assertEqual(guard.failed, 1)

    def test_breaker_stops_later_play_calls(self):
        guard = ct.PlayGuard(max_fails=2)
        calls = {"get": 0, "search": 0}

        def get(url):
            calls["get"] += 1
            raise RuntimeError("limited")

        def search(name, country):
            calls["search"] += 1
            raise RuntimeError("limited")

        with self.assertRaises(RuntimeError):
            ct.android_apps_for_country("Homa", "us", guard, search_fn=search, get=get)
        self.assertTrue(guard.tripped)
        with self.assertRaises(ct.PlayStopped):
            ct.android_apps_for_country("Peak", "us", guard, search_fn=search, get=get)
        self.assertEqual(calls, {"get": 1, "search": 1})

    def test_open_breaker_does_not_refresh_or_wipe_installs(self):
        guard = ct.PlayGuard()
        guard.tripped = True
        data = {"games": [{
            "platform": "Android",
            "app_id": "com.keep",
            "real_installs": 50,
            "installs": "50+",
            "found_date": "2026-10-01",
            "regions": ["us"],
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snaps.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"com.keep": [{"d": "2026-10-01", "v": 40}]}, f)
            with mock.patch.object(ct, "INSTALLS_HISTORY_FILE", path):
                with mock.patch.object(ct, "app", side_effect=AssertionError("should not call Play")):
                    ct.update_velocity(data, guard)
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
        self.assertEqual(data["games"][0]["real_installs"], 50)
        self.assertIn("com.keep", saved)
        self.assertGreaterEqual(data["games"][0]["velocity"], 0)


if __name__ == "__main__":
    unittest.main()
