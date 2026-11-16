"""Управление headless-браузером на сервере + сетевой «предохранитель».

Браузер ВСЕГДА запускается в headless-режиме на этом же сервере (Chromium из
Playwright, не системный Chrome) и имеет право обращаться только к loopback.
Любой запрос к другим хостам (в том числе youtube.com) отменяется на уровне
контекста и попадает в список blocked.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .config import LOOPBACK_HOSTS, Settings
from .sandbox.routes import KEY_COOKIE

if TYPE_CHECKING:  # Playwright необязателен: вкладка YouTube работает и без него
    from playwright.async_api import Browser, BrowserContext, Playwright, Route

log = logging.getLogger("yt-tester.browser")

_SAFE_SCHEMES = {"data", "blob", "about"}
LAUNCH_ARGS = [
    "--no-sandbox",  # на VPS обычно запуск от root / в контейнере
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--mute-audio",
]


def playwright_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def assert_loopback(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise RuntimeError(f"Запрещённый адрес для браузера: {host!r}. Разрешён только loopback.")


class BrowserHost:
    """Один Playwright + один Chromium с ленивым стартом и перезапуском после сбоя."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()
        self.restarts = 0

    @property
    def connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def ensure(self) -> Browser:
        """Гарантирует живой браузер: запускает или перезапускает при необходимости."""
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._browser is not None:
                self.restarts += 1
                log.warning("Браузер недоступен, перезапуск (#%d)", self.restarts)
                await self._shutdown_locked()
            assert_loopback(self.settings.sandbox_base_url)
            if self._pw is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError:
                    raise RuntimeError(
                        "Библиотека Playwright не установлена на этом сервере (песочница недоступна; "
                        "вкладка «YouTube API» от неё не зависит)"
                    ) from None
                self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True, args=LAUNCH_ARGS)
            log.info("Chromium запущен (headless) на сервере")
            return self._browser

    async def restart(self) -> Browser:
        async with self._lock:
            if self._browser is not None:
                self.restarts += 1
            await self._shutdown_locked()
        return await self.ensure()

    async def _shutdown_locked(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # браузер мог уже упасть
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    async def stop(self) -> None:
        async with self._lock:
            await self._shutdown_locked()

    async def new_context(self, blocked: list[str]) -> BrowserContext:
        """Новый изолированный контекст (= новая «сессия» с чистыми cookie)."""
        browser = await self.ensure()
        context = await browser.new_context(
            viewport={"width": 1000, "height": 800},
            service_workers="block",
            locale="ru-RU",
        )
        context.set_default_timeout(self.settings.action_timeout_ms)
        context.set_default_navigation_timeout(self.settings.action_timeout_ms * 2)

        async def guard(route: Route) -> None:
            url = route.request.url
            parsed = urlparse(url)
            if parsed.scheme in _SAFE_SCHEMES or (parsed.hostname or "").lower() in LOOPBACK_HOSTS:
                await route.continue_()
                return
            blocked.append(url)
            log.warning("Заблокирован внешний запрос: %s", url)
            await route.abort("blockedbyclient")

        await context.route("**/*", guard)
        await context.add_cookies(
            [
                {
                    "name": KEY_COOKIE,
                    "value": self.settings.sandbox_key,
                    "url": self.settings.sandbox_base_url,
                }
            ]
        )
        return context
