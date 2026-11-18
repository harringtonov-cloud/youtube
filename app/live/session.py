"""Удалённый браузер: настоящий Chromium на сервере, экран и управление из панели.

* Экран — реальные кадры браузера (JPEG) из потока CDP Page.startScreencast.
* Управление — Playwright: клик, ввод текста, клавиши, прокрутка, назад/вперёд/обновить.

Браузер ничего не делает сам: только то, что вы нажали в панели. Автотесты используют
отдельные контексты и только собственный тестовый плеер (см. scenarios.py).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

from ..browser import BrowserHost
from ..config import Settings
from ..events import EventBus

log = logging.getLogger("yt-tester.live")

VIEW_W = 1000
VIEW_H = 700
MAX_TEXT = 500
ALLOWED_KEYS = frozenset({
    "Enter", "Backspace", "Tab", "Escape", "Delete", "Space", "Home", "End",
    "PageUp", "PageDown", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
})
ALLOWED_ACTIONS = frozenset({"click", "scroll", "type", "key", "back", "forward", "reload"})
_BAD_SCHEMES = re.compile(r"^(javascript|data|file|mailto|about|blob|vbscript)\s*:", re.IGNORECASE)


class LiveError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def normalize_url(raw: Any) -> str:
    """Принимает только http/https. Без схемы подставляет https://."""
    if not isinstance(raw, str) or not raw.strip():
        raise LiveError("Введите адрес сайта")
    value = raw.strip()
    if len(value) > 2000:
        raise LiveError("Адрес слишком длинный")
    if "://" not in value:
        if _BAD_SCHEMES.match(value):
            raise LiveError("Разрешены только адреса http:// и https://")
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise LiveError("Разрешены только адреса http:// и https://")
    if not parsed.hostname:
        raise LiveError("В адресе не указан сайт")
    return value


def _fraction(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LiveError("Некорректная координата")
    return min(max(float(value), 0.0), 1.0)


def _scroll(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LiveError("Некорректная прокрутка")
    return int(min(max(value, -3000), 3000))


def _short(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return (lines[0] if lines else type(exc).__name__)[:200]


class LiveBrowser:
    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self.s = settings
        self.bus = bus
        self.host = BrowserHost(settings)
        self._lock = asyncio.Lock()
        self._context: Any = None
        self._page: Any = None
        self._cdp: Any = None
        self._frame: bytes | None = None
        self._seq = 0
        self._last_activity = 0.0
        self._watchdog: asyncio.Task[None] | None = None
        self._acks: set[asyncio.Task[None]] = set()
        self.url = ""
        self.title = ""
        self.loading = False
        self.error = ""
        self.note = ""

    # --- состояние ------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        return {
            "running": self._page is not None,
            "url": self.url,
            "title": self.title,
            "loading": self.loading,
            "frame_seq": self._seq,
            "error": self.error,
            "note": self.note,
            "viewport": {"width": VIEW_W, "height": VIEW_H},
        }

    def frame(self) -> tuple[bytes | None, int]:
        return self._frame, self._seq

    def touch(self) -> None:
        self._last_activity = time.time()

    async def _publish(self) -> None:
        await self.bus.publish({"type": "live_state", **self.state()})

    def _require(self) -> Any:
        if self._page is None:
            raise LiveError("Удалённый браузер не запущен. Нажмите «Запустить браузер»", 409)
        return self._page

    # --- запуск и остановка -----------------------------------------------------
    async def start(self, quality: int = 60, idle_minutes: int = 15) -> dict[str, Any]:
        async with self._lock:
            if self._page is not None:
                return self.state()
            self.error = ""
            self.note = ""
            try:
                self._context = await self.host.new_context([], guarded=False)
                page = await self._context.new_page()
                await page.set_viewport_size({"width": VIEW_W, "height": VIEW_H})
                page.on("framenavigated", self._on_navigated)
                self._page = page
                cdp = await self._context.new_cdp_session(page)
                cdp.on("Page.screencastFrame", self._on_frame)
                self._cdp = cdp
                await cdp.send("Page.startScreencast", {
                    "format": "jpeg", "quality": int(quality),
                    "maxWidth": VIEW_W, "maxHeight": VIEW_H, "everyNthFrame": 1,
                })
                self._frame = await page.screenshot(type="jpeg", quality=int(quality))
                self._seq += 1
                self.url = page.url
                self.title = await page.title()
                self.touch()
            except Exception as exc:  # noqa: BLE001 — любая ошибка запуска уходит пользователю
                log.error("Не удалось запустить удалённый браузер: %s", exc)
                await self._teardown_locked()
                self.error = f"Не удалось запустить браузер: {_short(exc)}"
                await self._publish()
                raise LiveError(self.error, 503) from exc
            self._watchdog = asyncio.create_task(self._watch_idle(idle_minutes))
            log.info("Удалённый браузер запущен (Chromium, headless), качество экрана %d", quality)
        await self._publish()
        return self.state()

    async def stop(self, note: str = "Остановлено") -> dict[str, Any]:
        async with self._lock:
            was_running = self._page is not None
            self.error = ""
            await self._teardown_locked()
            if was_running:
                self.note = note
                log.info("Удалённый браузер остановлен: %s", note)
        await self._publish()
        return self.state()

    async def _teardown_locked(self) -> None:
        cdp, context = self._cdp, self._context
        self._page = None
        self._cdp = None
        self._context = None
        self._frame = None
        self.url = ""
        self.title = ""
        self.loading = False
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
        if cdp is not None:
            try:
                await cdp.send("Page.stopScreencast")
            except Exception:  # noqa: BLE001 — браузер мог уже упасть
                pass
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass

    async def _watch_idle(self, idle_minutes: int) -> None:
        limit = max(int(idle_minutes), 1) * 60
        while self._page is not None:
            await asyncio.sleep(15)
            if self._page is not None and time.time() - self._last_activity > limit:
                await self.stop("Остановлено из-за простоя (экономия ресурсов Codespace)")
                return

    async def shutdown(self) -> None:
        await self.stop("Сервер остановлен")
        await self.host.stop()

    # --- события Playwright ------------------------------------------------------
    def _on_navigated(self, frame: Any) -> None:
        if self._page is not None and frame == self._page.main_frame:
            self.url = frame.url

    def _on_frame(self, params: dict[str, Any]) -> None:
        try:
            self._frame = base64.b64decode(params["data"])
            self._seq += 1
        except (KeyError, TypeError, ValueError):
            return
        cdp, sid = self._cdp, params.get("sessionId")
        if cdp is not None and sid is not None:
            task = asyncio.get_running_loop().create_task(self._ack(cdp, sid))
            self._acks.add(task)
            task.add_done_callback(self._acks.discard)

    @staticmethod
    async def _ack(cdp: Any, sid: Any) -> None:
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": sid})
        except Exception:  # noqa: BLE001 — кадр уже не нужен, если браузер закрыт
            pass

    async def _after(self, page: Any) -> None:
        if page is not self._page:
            return
        self.url = page.url
        try:
            self.title = await page.title()
        except Exception:  # noqa: BLE001
            self.title = ""
        await self._publish()

    # --- команды --------------------------------------------------------------
    async def navigate(self, raw_url: Any) -> dict[str, Any]:
        url = normalize_url(raw_url)
        page = self._require()
        self.touch()
        self.error = ""
        self.loading = True
        await self._publish()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.s.action_timeout_ms * 3)
        except Exception as exc:  # noqa: BLE001 — страница может не открыться, браузер остаётся
            self.error = f"Страница не открылась: {_short(exc)}"
            log.warning("Переход на %s не удался: %s", url, exc)
        finally:
            self.loading = False
        await self._after(page)
        return self.state()

    async def act(self, kind: Any, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if kind not in ALLOWED_ACTIONS:
            raise LiveError("Неизвестное действие")
        payload = payload or {}
        page = self._require()
        self.touch()
        self.error = ""
        self.loading = kind in ("back", "forward", "reload")
        timeout = self.s.action_timeout_ms * 3
        try:
            if kind == "click":
                await page.mouse.click(_fraction(payload.get("x")) * VIEW_W, _fraction(payload.get("y")) * VIEW_H)
            elif kind == "scroll":
                await page.mouse.move(VIEW_W / 2, VIEW_H / 2)
                await page.mouse.wheel(0, _scroll(payload.get("dy")))
            elif kind == "type":
                text = payload.get("text")
                if not isinstance(text, str) or not text:
                    raise LiveError("Введите текст")
                if len(text) > MAX_TEXT:
                    raise LiveError(f"Текст длиннее {MAX_TEXT} символов")
                await page.keyboard.type(text)
            elif kind == "key":
                key = payload.get("key")
                if not isinstance(key, str) or key not in ALLOWED_KEYS:
                    raise LiveError("Эта клавиша не разрешена")
                await page.keyboard.press(key)
            elif kind == "back":
                await page.go_back(wait_until="domcontentloaded", timeout=timeout)
            elif kind == "forward":
                await page.go_forward(wait_until="domcontentloaded", timeout=timeout)
            elif kind == "reload":
                await page.reload(wait_until="domcontentloaded", timeout=timeout)
        except LiveError:
            self.loading = False
            raise
        except Exception as exc:  # noqa: BLE001
            self.error = f"Действие не выполнено: {_short(exc)}"
            log.warning("Действие %s не выполнено: %s", kind, exc)
        self.loading = False
        await self._after(page)
        return self.state()
