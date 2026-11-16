"""Тестовые сценарии. Каждый работает ТОЛЬКО с локальной песочницей.

Исключение CheckFailed — это результат теста (провал проверки), повторять его
бессмысленно. Любые другие ошибки (таймауты, падение браузера) считаются
сбоями инфраструктуры — раннер перезапустит браузер и повторит сценарий.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from .browser import BrowserHost, assert_loopback
from .config import Settings
from .sandbox.store import COMMENT_LIMIT, MAX_COMMENT_LEN, SandboxStore

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page


class CheckFailed(Exception):
    """Проверка теста не прошла (функциональный провал, не сбой инфраструктуры)."""


LogFn = Callable[[str, str], Awaitable[None]]  # (level, message)
SandboxChanged = Callable[[str], Awaitable[None]]


@dataclass
class ScenarioContext:
    run_id: str
    scenario: str
    video_id: str
    settings: Settings
    host: BrowserHost
    store: SandboxStore
    shots_dir: Path
    log: LogFn
    notify_sandbox: SandboxChanged
    blocked: list[str]

    # --- проверки -------------------------------------------------------------
    async def check(self, condition: bool, message: str) -> None:
        if not condition:
            await self.log("error", f"✗ {message}")
            raise CheckFailed(message)
        await self.log("ok", f"✓ {message}")

    # --- песочница ------------------------------------------------------------
    async def reset_sandbox(self) -> None:
        self.store.reset(self.video_id)
        await self.notify_sandbox(self.video_id)

    def counters(self) -> dict[str, Any]:
        return self.store.counters(self.video_id)

    # --- браузер --------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def browser_context(self) -> AsyncIterator[BrowserContext]:
        bctx = await self.host.new_context(self.blocked)
        try:
            yield bctx
        except BaseException:
            await self._failure_shots(bctx)
            raise
        finally:
            with contextlib.suppress(Exception):
                await bctx.close()

    async def _failure_shots(self, bctx: BrowserContext) -> None:
        for i, page in enumerate(list(bctx.pages)):
            try:
                await self.screenshot(page, f"failure-{i}")
            except Exception:
                pass

    async def open_watch(self, bctx: BrowserContext) -> Page:
        url = f"{self.settings.sandbox_base_url}/sandbox/watch?v={self.video_id}"
        assert_loopback(url)
        page = await bctx.new_page()
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_selector("body[data-ready='1']")
        return page

    async def screenshot(self, page: Page, label: str) -> None:
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{self.scenario}-{label}")
        await page.screenshot(path=str(self.shots_dir / f"{safe}.png"))


# ============================================================================
# Сценарии
# ============================================================================
async def scenario_views(ctx: ScenarioContext) -> str:
    """Просмотр засчитывается один раз на сессию и только после порога воспроизведения."""
    await ctx.reset_sandbox()
    threshold = ctx.settings.sandbox_view_threshold_ms
    base = ctx.counters()["views"]
    wait_ms = threshold + 8000

    async with ctx.browser_context() as a:
        page = await ctx.open_watch(a)
        await ctx.log("info", "Страница песочницы открыта, запускаю воспроизведение")
        await page.click("#play-btn")
        await page.wait_for_function(
            "() => document.querySelector('#view-status').dataset.state === 'counted'",
            timeout=wait_ms,
        )
        await ctx.notify_sandbox(ctx.video_id)
        views = ctx.counters()["views"]
        await ctx.check(views == base + 1, f"Просмотр засчитан после порога {threshold} мс (счётчик {views})")
        shown = (await page.text_content("#views-count") or "").strip()
        await ctx.check(shown == str(views), f"Счётчик в интерфейсе ({shown}) совпадает с сервером ({views})")
        await ctx.screenshot(page, "counted")

        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector("body[data-ready='1']")
        await page.click("#play-btn")
        await page.wait_for_function(
            "() => ['duplicate','counted','rejected'].includes(document.querySelector('#view-status').dataset.state)",
            timeout=wait_ms,
        )
        await ctx.check(
            ctx.counters()["views"] == base + 1,
            "Повторный просмотр в той же сессии не засчитан (защита от дублей)",
        )

    async with ctx.browser_context() as b:
        page = await ctx.open_watch(b)
        probe = (
            "async (path) => { const r = await fetch(path, {method:'POST',"
            "headers:{'content-type':'application/json'},body:JSON.stringify({vid:document.body.dataset.vid})});"
            "return r.json(); }"
        )
        res = await page.evaluate(probe, "/sandbox/api/view")
        await ctx.check(
            res.get("counted") is False and res.get("reason") == "not_played",
            "Просмотр без воспроизведения отклонён сервером",
        )
        await page.evaluate(probe, "/sandbox/api/play")
        res = await page.evaluate(probe, "/sandbox/api/view")
        await ctx.check(
            res.get("counted") is False and res.get("reason") == "too_early",
            "Просмотр раньше порога отклонён (время проверяет сервер, а не клиент)",
        )
        await page.click("#play-btn")
        await page.wait_for_function(
            "() => document.querySelector('#view-status').dataset.state === 'counted'",
            timeout=wait_ms,
        )
        await ctx.notify_sandbox(ctx.video_id)
        await ctx.check(ctx.counters()["views"] == base + 2, "Новая сессия засчитана отдельно (итого +2)")
    return "Просмотры: порог, дедупликация и серверная проверка работают"


async def scenario_likes(ctx: ScenarioContext) -> str:
    """Лайк: установка/снятие, сохранение состояния, независимость сессий."""
    await ctx.reset_sandbox()
    async with ctx.browser_context() as a:
        page = await ctx.open_watch(a)
        await ctx.check((await page.text_content("#likes-count") or "").strip() == "0", "Начальное число лайков — 0")

        await page.click("#like-btn")
        await page.wait_for_selector("#like-btn[aria-pressed='true']:not([disabled])")
        await ctx.notify_sandbox(ctx.video_id)
        await ctx.check(ctx.counters()["likes"] == 1, "Лайк поставлен: на сервере 1")
        await ctx.check((await page.text_content("#likes-count") or "").strip() == "1", "Интерфейс показывает 1")
        await ctx.screenshot(page, "liked")

        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector("body[data-ready='1']")
        pressed = await page.get_attribute("#like-btn", "aria-pressed")
        await ctx.check(pressed == "true", "После перезагрузки лайк сохранён для сессии")

        await page.click("#like-btn")
        await page.wait_for_selector("#like-btn[aria-pressed='false']:not([disabled])")
        await ctx.notify_sandbox(ctx.video_id)
        await ctx.check(ctx.counters()["likes"] == 0, "Лайк снят: на сервере 0")

        await page.click("#like-btn", click_count=3, delay=0)  # быстрые клики не должны ломать счётчик
        await page.wait_for_selector("#like-btn:not([disabled])")
        likes = ctx.counters()["likes"]
        await ctx.check(likes in (0, 1), f"Быстрые клики не дают некорректный счётчик (сейчас {likes})")

    await ctx.reset_sandbox()
    async with ctx.browser_context() as b1, ctx.browser_context() as b2:
        for bctx in (b1, b2):
            page = await ctx.open_watch(bctx)
            await page.click("#like-btn")
            await page.wait_for_selector("#like-btn[aria-pressed='true']:not([disabled])")
        await ctx.notify_sandbox(ctx.video_id)
        await ctx.check(ctx.counters()["likes"] == 2, "Две разные сессии = 2 лайка, одна сессия = не более 1")
    return "Лайки: идемпотентность, сохранение и независимость сессий работают"


async def scenario_comments(ctx: ScenarioContext) -> str:
    """Комментарии: отправка, валидация, защита от XSS, ограничение частоты."""
    await ctx.reset_sandbox()
    async with ctx.browser_context() as a:
        page = await ctx.open_watch(a)

        await page.fill("#comment-input", "Привет из автотеста")
        await page.click("#comment-submit")
        await page.wait_for_selector(".comment-item")
        await ctx.notify_sandbox(ctx.video_id)
        await ctx.check(ctx.counters()["comments"] == 1, "Комментарий сохранён на сервере")
        text = await page.text_content(".comment-item .text")
        await ctx.check(text == "Привет из автотеста", "Комментарий отображается в списке")
        await ctx.screenshot(page, "comment-posted")

        await page.fill("#comment-input", "   ")
        await page.click("#comment-submit")
        await page.wait_for_selector("#comment-error[data-code='empty']")
        await ctx.check(ctx.counters()["comments"] == 1, "Пустой комментарий отклонён")

        await page.fill("#comment-input", "x" * (MAX_COMMENT_LEN + 1))
        await page.click("#comment-submit")
        await page.wait_for_selector("#comment-error[data-code='too_long']")
        await ctx.check(ctx.counters()["comments"] == 1, f"Комментарий длиннее {MAX_COMMENT_LEN} символов отклонён")

        payload = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>'
        await page.fill("#comment-input", payload)
        await page.click("#comment-submit")
        await page.wait_for_function("() => document.querySelectorAll('.comment-item').length === 2")
        await ctx.notify_sandbox(ctx.video_id)
        shown = await page.text_content(".comment-item:last-child .text")
        xss = await page.evaluate("typeof window.__xss")
        await ctx.check(shown == payload, "HTML в комментарии показан как текст")
        await ctx.check(xss == "undefined", "Вредоносный скрипт из комментария не выполнился (XSS-защита)")

        sent = 2
        blocked = False
        for i in range(COMMENT_LIMIT + 2):
            await page.fill("#comment-input", f"Сообщение {i}")
            await page.click("#comment-submit")
            await page.wait_for_function(
                "() => !document.querySelector('#comment-submit').disabled"
            )
            code = await page.get_attribute("#comment-error", "data-code")
            if code == "rate_limited":
                blocked = True
                break
            sent += 1
        await ctx.notify_sandbox(ctx.video_id)
        await ctx.check(blocked, f"Ограничение частоты сработало после {sent} комментариев (лимит {COMMENT_LIMIT})")
        await ctx.check(sent == COMMENT_LIMIT, f"Лимит применён ровно на {COMMENT_LIMIT}-м комментарии")
    return "Комментарии: валидация, XSS-защита и лимит частоты работают"


async def scenario_recovery(ctx: ScenarioContext) -> str:
    """Автовосстановление: сбой бэкенда (повторы на странице) и падение браузера (перезапуск)."""
    await ctx.reset_sandbox()
    ctx.store.arm_flaky(2)
    await ctx.log("warn", "Бэкенд песочницы: следующие 2 запроса вернут 503")
    async with ctx.browser_context() as a:
        page = await ctx.open_watch(a)
        await page.click("#like-btn")
        await page.wait_for_selector("#like-btn[aria-pressed='true']:not([disabled])", timeout=15000)
        await ctx.notify_sandbox(ctx.video_id)
        retries = await page.get_attribute("#net-status", "data-retries")
        await ctx.check(ctx.store.flaky_hits == 2, f"Сервер отказал {ctx.store.flaky_hits} раз(а) подряд")
        await ctx.check(retries == "2", "Страница сама повторила запрос и восстановилась")
        await ctx.check(ctx.counters()["likes"] == 1, "Действие в итоге выполнено ровно один раз")

    # Падение браузера: используем отдельный экземпляр, чтобы не мешать другим прогонам.
    isolated = BrowserHost(ctx.settings)
    try:
        await isolated.ensure()
        probe_ctx = await isolated.new_context([])
        page = await probe_ctx.new_page()
        await page.goto("about:blank")
        await ctx.log("warn", "Имитирую аварийное завершение браузера")
        assert isolated._browser is not None
        await isolated._browser.close()
        await ctx.check(not isolated.connected, "Браузер остановлен (имитация сбоя)")
        await isolated.ensure()
        await ctx.check(isolated.connected and isolated.restarts == 1, "Браузер автоматически перезапущен")
        new_ctx = await isolated.new_context([])
        new_page = await new_ctx.new_page()
        await new_page.goto(f"{ctx.settings.sandbox_base_url}/sandbox/watch?v={ctx.video_id}")
        await new_page.wait_for_selector("body[data-ready='1']")
        await ctx.check(True, "После перезапуска страница песочницы снова открывается")
    finally:
        await isolated.stop()
    return "Автовосстановление: повторы запросов и перезапуск браузера работают"


async def scenario_safety_guard(ctx: ScenarioContext) -> str:
    """Самопроверка безопасности: внешние адреса (в т.ч. YouTube) браузеру недоступны."""
    async with ctx.browser_context() as a:
        page = await ctx.open_watch(a)
        before = len(ctx.blocked)
        probe = (
            "async (u) => { try { await fetch(u, {mode:'no-cors'}); return 'reached'; }"
            " catch (e) { return 'blocked'; } }"
        )
        result = await page.evaluate(probe, "https://example.com/__guard_probe")
        await ctx.check(result == "blocked", "Запрос к внешнему сайту отменён браузерным предохранителем")
        await ctx.check(
            any("example.com/__guard_probe" in u for u in ctx.blocked[before:]),
            "Попытка зафиксирована в списке заблокированных запросов",
        )
        await ctx.check(
            ctx.settings.sandbox_base_url.startswith("http://127.0.0.1"),
            "Песочница работает только на loopback (127.0.0.1)",
        )
    return "Предохранитель активен: наружу (включая YouTube) трафик не уходит"


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    description: str
    func: Callable[[ScenarioContext], Awaitable[str]]


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        Scenario("views", "Просмотры", "Порог воспроизведения, дедупликация, серверная проверка", scenario_views),
        Scenario("likes", "Лайки", "Установка/снятие, сохранение, независимость сессий", scenario_likes),
        Scenario("comments", "Комментарии", "Валидация, XSS-защита, лимит частоты", scenario_comments),
        Scenario("recovery", "Автовосстановление", "Сбой бэкенда и перезапуск браузера", scenario_recovery),
        Scenario("safety_guard", "Предохранитель", "Внешний трафик (YouTube) заблокирован", scenario_safety_guard),
    )
}


def now_ms() -> float:
    return time.monotonic() * 1000
