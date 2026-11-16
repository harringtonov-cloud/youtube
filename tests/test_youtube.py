"""Тесты слоя YouTube API/OAuth против тестового двойника Google (tests/mock_google.py).

Запуск: python -m unittest discover -s tests -t .
"""

import asyncio
import os
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock as umock

os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.youtube import service as service_mod  # noqa: E402
from app.youtube.http import ApiError, call  # noqa: E402
from app.youtube.oauth import OAuthManager  # noqa: E402
from app.youtube.routes import api_error_response  # noqa: E402
from app.youtube.service import YouTubeService, parse_duration, pt_day_start, pt_next_reset  # noqa: E402
from tests.mock_google import API_KEY, CLIENT_ID, CLIENT_SECRET, SCOPE_SSL, SCOPE_YT, MockGoogle  # noqa: E402

OTHER = "dQw4w9WgXcQ"   # чужое видео
OWN = "abcdefghijk"     # видео своего канала, лайки скрыты
NOCOMM = "nocomment_1"  # комментарии отключены


class Base(unittest.IsolatedAsyncioTestCase):
    api_key = ""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.google = MockGoogle()
        base = self.google.start()
        self.settings = Settings(
            data_dir=Path(self.tmp.name),
            google_client_id=CLIENT_ID, google_client_secret=CLIENT_SECRET,
            google_redirect_uri="https://panel.example.com/oauth/callback",
            youtube_api_key=self.api_key,
            google_oauth_base=f"{base}/oauth", google_auth_url=f"{base}/auth",
            youtube_api_base=f"{base}/youtube/v3",
        )
        self.db = Database(Path(self.tmp.name) / "t.sqlite3")
        self.bus = EventBus()
        self.oauth = OAuthManager(self.settings, self.db, self.bus)
        self.svc = YouTubeService(self.settings, self.db, self.bus, self.oauth)
        self._patch = umock.patch.object(service_mod, "WRITE_COOLDOWN_S", 0.0)
        self._patch.start()

    async def asyncTearDown(self):
        self._patch.stop()
        await self.oauth.cancel_device()
        self.google.stop()
        self.db.close()
        self.tmp.cleanup()

    async def login(self, scope: str):
        data = self.google._issue(scope, "REFRESH_T")
        self.oauth._store(data, "test")
        await asyncio.to_thread(self.oauth.fetch_profile)


class ParsingTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(parse_duration("PT3M33S"), 213)
        self.assertEqual(parse_duration("PT1H2M3S"), 3723)
        self.assertEqual(parse_duration("P1DT1H"), 90000)
        self.assertEqual(parse_duration("PT45S"), 45)
        self.assertIsNone(parse_duration(None))
        self.assertIsNone(parse_duration("garbage"))

    def test_pacific_day(self):
        now = time.time()
        start, reset = pt_day_start(now), pt_next_reset(now)
        self.assertLessEqual(start, now)
        self.assertGreater(reset, now)
        self.assertIn(reset - start, (23 * 3600, 24 * 3600, 25 * 3600))  # переход на летнее время

    def test_network_error(self):
        with self.assertRaises(ApiError) as cm:
            call("GET", "http://127.0.0.1:9/x", retries=0, timeout=2)
        self.assertEqual(cm.exception.reason, "network")


class ReadTests(Base):
    api_key = API_KEY

    async def test_video_real_fields_and_hidden_values(self):
        v = await self.svc.video(OTHER)
        self.assertEqual((v["views"], v["likes"], v["comments"]), (1000, 50, 7))
        self.assertEqual(v["duration_s"], 213)
        self.assertEqual(v["thumbnail"], "https://i.ytimg.com/vi/x/hq.jpg")
        own = await self.svc.video(OWN)
        self.assertIsNone(own["likes"], "скрытые лайки не должны превращаться в 0")
        self.assertEqual(own["views"], 200)
        self.assertIsNone((await self.svc.video(NOCOMM))["comments"])

    async def test_unknown_video(self):
        with self.assertRaises(ApiError) as cm:
            await self.svc.video("zzzzzzzzzzz")
        self.assertEqual(cm.exception.reason, "videoNotFound")

    async def test_comments_and_disabled(self):
        self.google.comments[OTHER] = [{"id": "c1", "snippet": {"authorDisplayName": "А", "textDisplay": "привет", "likeCount": 3}}]
        data = await self.svc.comments(OTHER)
        self.assertEqual(data["items"][0]["text"], "привет")
        self.assertEqual(data["items"][0]["likes"], 3)
        with self.assertRaises(ApiError) as cm:
            await self.svc.comments(NOCOMM)
        self.assertEqual(cm.exception.reason, "commentsDisabled")
        self.assertIn("отключил", api_error_response(cm.exception).body.decode())

    async def test_writes_need_login_in_key_mode(self):
        for coro in (self.svc.rate(OTHER, "like"), self.svc.post_comment(OTHER, "hi")):
            with self.assertRaises(ApiError):
                await coro
        self.assertEqual(self.google.count("POST", "/youtube/v3/videos/rate"), 0)

    async def test_quota_error_is_reported_and_logged(self):
        self.google.fail_next = (403, "quotaExceeded", "quota")
        with self.assertRaises(ApiError) as cm:
            await self.svc.video(OTHER)
        self.assertEqual(cm.exception.reason, "quotaExceeded")
        self.assertIn("квота", api_error_response(cm.exception).body.decode().lower())
        last = self.db.list_yt_actions(1)[0]
        self.assertFalse(last["ok"])
        self.assertEqual(last["name"], "videos.list")
        self.assertEqual(self.google.count("GET", "/youtube/v3/videos"), 1, "quotaExceeded не повторяется")

    async def test_quota_estimate_counts_units(self):
        await self.svc.video(OTHER)
        await self.svc.comments(OTHER)
        st = await self.svc.status()
        self.assertEqual(st["quota"]["used_estimate"], 2)
        self.assertTrue(st["quota"]["estimated"])

    async def test_watchlist_polling_stores_real_snapshots(self):
        await self.svc.add_watch(OTHER)
        self.google.videos[OTHER]["statistics"]["viewCount"] = "1500"
        res = await self.svc.poll_once()
        self.assertEqual(res["polled"], 1)
        snaps = self.db.list_snapshots(OTHER)
        self.assertEqual([s["views"] for s in snaps], [1000, 1500])
        item = self.db.watch_list()[0]
        self.assertEqual(item["latest"]["views"], 1500)
        self.assertEqual(item["previous"]["views"], 1000)
        del self.google.videos[OTHER]
        res = await self.svc.poll_once()
        self.assertEqual(res["missing"], [OTHER])
        self.assertEqual(len(self.db.list_snapshots(OTHER)), 2, "для пропавшего видео снимки не выдумываются")


class DeviceFlowTests(Base):
    async def test_device_login_success(self):
        state = await self.oauth.start_device()
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["user_code"], "ABCD-EFGH")
        await asyncio.wait_for(self.oauth._device_task, 20)
        self.assertEqual(self.oauth.device_state()["status"], "approved")
        self.assertTrue(self.oauth.connected)
        self.assertEqual(self.oauth.profile()["title"], "Мой канал")
        self.assertEqual(self.oauth.granted_scopes(), {SCOPE_YT})
        self.assertGreaterEqual(self.google.device_polls, 3)
        st = await self.svc.status()
        self.assertTrue(st["can_rate"])
        self.assertFalse(st["can_comment"], "device-вход не даёт права на комментарии")

    async def test_device_denied(self):
        self.google.device_mode = "deny"
        await self.oauth.start_device()
        await asyncio.wait_for(self.oauth._device_task, 20)
        self.assertEqual(self.oauth.device_state()["status"], "denied")
        self.assertFalse(self.oauth.connected)

    async def test_wrong_client_id(self):
        self.settings.google_client_id = "bad"
        with self.assertRaises(ApiError) as cm:
            await self.oauth.start_device()
        self.assertEqual(cm.exception.reason, "invalid_client")


class AuthedTests(Base):
    async def test_rate_is_idempotent_and_limited(self):
        await self.login(SCOPE_YT)
        self.assertEqual(await self.svc.my_rating(OTHER), "none")
        r = await self.svc.rate(OTHER, "like")
        self.assertTrue(r["changed"])
        self.assertIn(OTHER, self.google.ratings)
        r = await self.svc.rate(OTHER, "like")
        self.assertFalse(r["changed"])
        self.assertEqual(self.google.count("POST", "/youtube/v3/videos/rate"), 1, "повторный лайк не тратит квоту")
        self.settings.yt_write_limit_per_day = 1
        with self.assertRaises(ApiError) as cm:
            await self.svc.rate(OTHER, "none")
        self.assertEqual(cm.exception.reason, "local_limit")
        self.assertEqual(self.google.count("POST", "/youtube/v3/videos/rate"), 1)
        with self.assertRaises(ApiError):
            await self.svc.rate(OTHER, "dislike")

    async def test_cooldown_blocks_double_click(self):
        await self.login(SCOPE_YT)
        with umock.patch.object(service_mod, "WRITE_COOLDOWN_S", 30.0):
            await self.svc.rate(OTHER, "like")
            with self.assertRaises(ApiError) as cm:
                await self.svc.rate(OTHER, "none")
        self.assertEqual(cm.exception.reason, "cooldown")

    async def test_comments_not_possible_with_device_scope(self):
        await self.login(SCOPE_YT)
        before = len(self.google.calls)
        for coro in (self.svc.post_comment(OTHER, "привет"), self.svc.reply_comment(OTHER, "c1", "x"),
                     self.svc.delete_comment(OTHER, "c1")):
            with self.assertRaises(ApiError) as cm:
                await coro
            self.assertEqual(cm.exception.reason, "insufficient_scope")
        self.assertEqual(len(self.google.calls), before, "без нужных прав запросы к API не отправляются")

    async def test_web_flow_and_comment_lifecycle(self):
        url = self.oauth.web_start()
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
        self.assertEqual(q["scope"], SCOPE_SSL)
        self.assertEqual(q["access_type"], "offline")
        self.assertEqual(q["redirect_uri"], self.settings.google_redirect_uri)
        with self.assertRaises(ApiError) as cm:
            await self.oauth.web_finish("CODE123", "wrong-state")
        self.assertEqual(cm.exception.reason, "bad_state")
        await self.oauth.web_finish("CODE123", q["state"])
        with self.assertRaises(ApiError):  # state одноразовый
            await self.oauth.web_finish("CODE123", q["state"])
        self.assertTrue(self.oauth.has_scope(SCOPE_SSL))
        await asyncio.to_thread(self.oauth.fetch_profile)

        c = await self.svc.post_comment(OTHER, "  Привет из API  ")
        self.assertEqual(c["text"], "Привет из API")
        self.assertEqual(self.google.last_update_body["snippet"]["channelId"], "UC_other")
        self.assertEqual((await self.svc.comments(OTHER))["items"][0]["id"], c["id"])
        rep = await self.svc.reply_comment(OTHER, c["id"], "ответ")
        self.assertEqual(rep["text"], "ответ")
        await self.svc.delete_comment(OTHER, c["id"])
        self.assertEqual((await self.svc.comments(OTHER))["items"], [])
        with self.assertRaises(ApiError):
            await self.svc.post_comment(OTHER, "   ")
        kinds = [a["name"] for a in self.db.list_yt_actions(50) if a["kind"] == "write"]
        self.assertIn("commentThreads.insert", kinds)
        self.assertIn("comments.delete", kinds)

    async def test_update_own_video_preserves_other_fields(self):
        await self.login(SCOPE_SSL)
        out = await self.svc.update_video(OWN, "Новое название", "Новое описание")
        self.assertEqual(out["title"], "Новое название")
        sent = self.google.last_update_body["snippet"]
        self.assertEqual(sent["tags"], ["music", "test"])
        self.assertEqual(sent["categoryId"], "10")
        stored = self.google.videos[OWN]["snippet"]
        self.assertEqual(stored["tags"], ["music", "test"], "теги не должны стираться")
        self.assertEqual(stored["defaultLanguage"], "ru")

    async def test_update_foreign_video_and_validation(self):
        await self.login(SCOPE_SSL)
        puts = self.google.count("PUT", "/youtube/v3/videos")
        with self.assertRaises(ApiError) as cm:
            await self.svc.update_video(OTHER, "x", "y")
        self.assertEqual(cm.exception.reason, "not_owner")
        self.assertEqual(self.google.count("PUT", "/youtube/v3/videos"), puts)
        for title, desc in (("", "d"), ("a<b", "d"), ("t", "d>"), ("x" * 101, "d")):
            with self.assertRaises(ApiError):
                await self.svc.update_video(OWN, title, desc)

    async def test_token_refresh_and_revocation(self):
        self.google.expires_in = 30  # меньше запаса в 60 с → обновление на каждом вызове
        await self.login(SCOPE_YT)
        await self.svc.video(OTHER)
        await self.svc.video(OTHER)
        self.assertGreaterEqual(self.google.count("POST", "/oauth/token"), 2)
        self.assertEqual(self.oauth.tokens()["refresh_token"], "REFRESH_T", "refresh_token сохраняется")
        self.google.refresh_revoked = True
        with self.assertRaises(ApiError) as cm:
            await self.svc.video(OTHER)
        self.assertEqual(cm.exception.reason, "invalid_grant")
        self.assertFalse(self.oauth.connected, "после отзыва доступа токены очищаются")

    async def test_logout_revokes_at_google(self):
        await self.login(SCOPE_YT)
        await self.oauth.logout()
        self.assertEqual(self.google.revoked, ["REFRESH_T"])
        self.assertFalse(self.oauth.connected)
        self.assertIsNone(self.oauth.profile())

    async def test_events_are_published(self):
        q = self.bus.subscribe()
        await self.login(SCOPE_YT)
        await self.svc.video(OTHER)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        self.assertTrue(any(e["type"] == "yt_action" and e["action"]["name"] == "videos.list" for e in events))


if __name__ == "__main__":
    unittest.main()
