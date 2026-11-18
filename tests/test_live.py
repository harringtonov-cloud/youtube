"""Тесты удалённого браузера, настроек и параметров прогона.

Чистые проверки работают везде. Живой тест запускает настоящий Chromium (Playwright) и
локальную HTML-страницу; если браузера нет — он пропускается с понятной причиной.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.events import EventBus
from app.live.session import LiveBrowser, LiveError, normalize_url
from app.runner import sanitize_options
from app import settings_store

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Тестовая страница</title></head>
<body style="margin:0;font:20px sans-serif">
<h1>Привет</h1>
<button id="b" style="font-size:28px;padding:20px 40px"
  onclick="document.getElementById('out').textContent='нажато'">Нажми</button>
<input id="i" type="text" style="font-size:24px" oninput="document.getElementById('echo').textContent=this.value">
<div id="out"></div><div id="echo"></div>
<div style="height:3000px"></div>
<a id="next" href="/second.html">дальше</a>
</body></html>"""
SECOND = """<!doctype html><html><head><meta charset="utf-8"><title>Вторая</title></head>
<body><p>Вторая страница</p></body></html>"""


def _chromium_ready() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "Playwright не установлен"
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            b.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"Chromium недоступен: {str(exc).splitlines()[0][:120]}"


class NormalizeUrlTests(unittest.TestCase):
    def test_adds_https_and_accepts_http(self):
        self.assertEqual(normalize_url("example.com"), "https://example.com")
        self.assertEqual(normalize_url(" http://127.0.0.1:8000/x "), "http://127.0.0.1:8000/x")

    def test_rejects_dangerous_schemes(self):
        for bad in ["javascript:alert(1)", "data:text/html,hi", "file:///etc/passwd", "ftp://x.com", "", "  ", 5]:
            with self.subTest(bad=bad), self.assertRaises(LiveError):
                normalize_url(bad)

    def test_rejects_overlong(self):
        with self.assertRaises(LiveError):
            normalize_url("https://a.com/" + "x" * 2100)


class SanitizeOptionsTests(unittest.TestCase):
    def test_defaults_and_bounds(self):
        self.assertEqual(sanitize_options(None), {"repeat": 1})
        self.assertEqual(sanitize_options({"repeat": 7}), {"repeat": 7})
        for bad in [0, 101, True, "3", None]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sanitize_options({"repeat": bad})

    def test_unknown_keys_are_dropped(self):
        self.assertEqual(sanitize_options({"repeat": 2, "url": "https://youtube.com", "x": 1}), {"repeat": 2})


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = Database(self.tmp / "t.sqlite3")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_then_save_persists(self):
        self.assertEqual(settings_store.load(self.db)["repeat"], 3)
        saved = settings_store.save(self.db, {"repeat": 5, "scenarios": ["views", "playback_repeat"]})
        self.assertEqual(saved["repeat"], 5)
        again = settings_store.load(self.db)
        self.assertEqual(again["repeat"], 5)
        self.assertEqual(again["scenarios"], ["views", "playback_repeat"])

    def test_invalid_patch_changes_nothing(self):
        settings_store.save(self.db, {"repeat": 4})
        for patch in [{"repeat": 0}, {"frame_quality": 5}, {"scenarios": ["nope"]}, {"unknown": 1}, ["x"]]:
            with self.subTest(patch=patch), self.assertRaises(settings_store.SettingsError):
                settings_store.save(self.db, patch)
        self.assertEqual(settings_store.load(self.db)["repeat"], 4)

    def test_corrupt_stored_value_falls_back_to_default(self):
        self.db.kv_set(settings_store.KEY, '{"repeat": "zzz", "frame_quality": 70}')
        loaded = settings_store.load(self.db)
        self.assertEqual(loaded["repeat"], 3)
        self.assertEqual(loaded["frame_quality"], 70)


class RunOptionsStoredTests(unittest.TestCase):
    def test_options_round_trip_and_migration(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            db = Database(tmp / "t.sqlite3")
            run = db.create_run("abcdef012345", "https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ",
                                ["playback_repeat"], {"repeat": 4})
            self.assertEqual(run["options"], {"repeat": 4})
            db.close()
            # повторное открытие (миграция уже применена) не ломает данные
            db2 = Database(tmp / "t.sqlite3")
            self.assertEqual(db2.get_run("abcdef012345")["options"], {"repeat": 4})
            db2.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # без шума в выводе тестов
        pass


class LiveBrowserTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.ok, cls.reason = _chromium_ready()

    async def asyncSetUp(self):
        if not self.ok:
            self.skipTest(self.reason)
        self.tmp = Path(tempfile.mkdtemp())
        site = self.tmp / "site"
        site.mkdir()
        (site / "index.html").write_text(PAGE, encoding="utf-8")
        (site / "second.html").write_text(SECOND, encoding="utf-8")
        handler = functools.partial(_Quiet, directory=str(site))
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.settings = Settings(data_dir=self.tmp / "data", port=8000, action_timeout_ms=8000)
        self.live = LiveBrowser(self.settings, EventBus())

    async def asyncTearDown(self):
        if self.ok:
            await self.live.shutdown()
            self.httpd.shutdown()
            self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _wait_frame(self, after: int = 0, timeout: float = 8.0) -> tuple[bytes, int]:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            data, seq = self.live.frame()
            if data and seq > after:
                return data, seq
            await asyncio.sleep(0.1)
        self.fail("кадр экрана не получен")

    async def test_full_control_cycle(self):
        # до запуска команды отклоняются понятной ошибкой
        with self.assertRaises(LiveError):
            await self.live.navigate(self.base + "/index.html")

        state = await self.live.start(quality=60, idle_minutes=15)
        self.assertTrue(state["running"])
        frame, seq = await self._wait_frame()
        self.assertTrue(frame.startswith(b"\xff\xd8"), "кадр должен быть JPEG")

        state = await self.live.navigate(self.base + "/index.html")
        self.assertEqual(state["title"], "Тестовая страница")
        self.assertEqual(state["url"], self.base + "/index.html")
        self.assertEqual(state["error"], "")

        # клик по кнопке в относительных координатах кадра
        page = self.live._page
        box = await page.locator("#b").bounding_box()
        fx = (box["x"] + box["width"] / 2) / 1000
        fy = (box["y"] + box["height"] / 2) / 700
        await self.live.act("click", {"x": fx, "y": fy})
        self.assertEqual(await page.text_content("#out"), "нажато")

        # ввод текста в поле
        await page.click("#i")
        await self.live.act("type", {"text": "привет"})
        self.assertEqual(await page.text_content("#echo"), "привет")

        # прокрутка меняет позицию страницы
        await self.live.act("scroll", {"dy": 600})
        await asyncio.sleep(0.3)
        self.assertGreater(await page.evaluate("window.scrollY"), 0)

        # переход по ссылке, назад, обновление
        await self.live.navigate(self.base + "/index.html")
        await page.locator("#next").scroll_into_view_if_needed()
        link = await page.locator("#next").bounding_box()
        await self.live.act("click", {"x": (link["x"] + 5) / 1000, "y": (link["y"] + 5) / 700})
        await page.wait_for_url("**/second.html", timeout=8000)
        self.assertIn("second.html", self.live.state()["url"])
        await self.live.act("back")
        self.assertIn("index.html", self.live.state()["url"])
        await self.live.act("reload")
        self.assertIn("index.html", self.live.state()["url"])

        # новый кадр приходит после действий
        _, seq2 = await self._wait_frame(after=seq)
        self.assertGreater(seq2, seq)

        # недопустимые команды отклоняются, браузер продолжает работать
        with self.assertRaises(LiveError):
            await self.live.act("eval", {"code": "1"})
        with self.assertRaises(LiveError):
            await self.live.act("key", {"key": "F12"})
        with self.assertRaises(LiveError):
            await self.live.act("type", {"text": "x" * 600})

        state = await self.live.stop("Остановлено пользователем")
        self.assertFalse(state["running"])
        self.assertEqual(state["note"], "Остановлено пользователем")

    async def test_bad_navigation_is_reported_not_fatal(self):
        await self.live.start()
        state = await self.live.navigate("http://127.0.0.1:1/nothing-here")
        self.assertTrue(state["running"])
        self.assertTrue(state["error"].startswith("Страница не открылась"))
        await self.live.stop()


if __name__ == "__main__":
    unittest.main()
