"""Менеджер прогонов: очередь, выполнение сценариев, автовосстановление, события."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import traceback
import uuid
from typing import Any

from .browser import BrowserHost
from .config import Settings
from .db import Database
from .events import EventBus
from .sandbox.store import SandboxStore
from .scenarios import SCENARIOS, CheckFailed, ScenarioContext
from .urlutils import extract_video_id

log = logging.getLogger("yt-tester.runner")

INSTALL_HINT = (
    "Браузер для песочницы недоступен. Если Playwright установлен, выполните на сервере: "
    "playwright install --with-deps chromium"
)


class RunManager:
    def __init__(self, settings: Settings, db: Database, bus: EventBus, store: SandboxStore) -> None:
        self.settings = settings
        self.db = db
        self.bus = bus
        self.store = store
        self.host = BrowserHost(settings)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._user_cancelled: set[str] = set()

    # --- жизненный цикл -----------------------------------------------------
    async def start(self) -> None:
        interrupted = await asyncio.to_thread(self.db.fail_interrupted)
        if interrupted:
            log.warning("Помечено прерванных прогонов после перезапуска: %d", interrupted)
        for i in range(self.settings.max_concurrent_runs):
            self._workers.append(asyncio.create_task(self._worker(), name=f"run-worker-{i}"))

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        await self.host.stop()

    # --- публичный API ------------------------------------------------------
    async def submit(self, url: str, scenario_names: list[str]) -> dict[str, Any]:
        video_id = extract_video_id(url)  # бросает InvalidYouTubeUrl
        names = [n for n in dict.fromkeys(scenario_names) if n in SCENARIOS]
        if not names:
            raise ValueError("Выберите хотя бы один сценарий")
        run_id = uuid.uuid4().hex[:12]
        run = await asyncio.to_thread(self.db.create_run, run_id, url.strip()[:300], video_id, names)
        await self.bus.publish({"type": "run_update", "run": run})
        await self._queue.put(run_id)
        await self._log(run_id, "info", None, f"Прогон поставлен в очередь (видео-метка: {video_id})")
        return run

    async def cancel(self, run_id: str) -> bool:
        run = await asyncio.to_thread(self.db.get_run, run_id)
        if not run or run["status"] not in {"queued", "running"}:
            return False
        self._user_cancelled.add(run_id)
        task = self._tasks.get(run_id)
        if task is not None:
            task.cancel()
        else:  # ещё в очереди: воркер пропустит его
            await self._finalize(run_id, "cancelled", error="Отменено пользователем")
        return True

    def remove_artifacts(self, run_id: str) -> None:
        shutil.rmtree(self.settings.screenshots_dir / run_id, ignore_errors=True)

    # --- внутренности -------------------------------------------------------
    async def _log(self, run_id: str, level: str, scenario: str | None, message: str) -> None:
        try:
            entry = await asyncio.to_thread(self.db.add_log, run_id, level, scenario, message)
        except Exception:  # запись лога не должна ронять прогон
            log.exception("Не удалось записать лог")
            return
        log.log(
            {"error": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO),
            "[%s] %s%s",
            run_id,
            f"{scenario}: " if scenario else "",
            message,
        )
        await self.bus.publish({"type": "log", **entry})

    async def _finalize(self, run_id: str, status: str, summary: dict[str, Any] | None = None,
                        error: str | None = None) -> None:
        await asyncio.to_thread(
            self.db.update_run, run_id, status=status, finished_at=time.time(),
            summary=summary, error=error,
        )
        run = await asyncio.to_thread(self.db.get_run, run_id)
        await self.bus.publish({"type": "run_update", "run": run})

    async def _worker(self) -> None:
        while True:
            run_id = await self._queue.get()
            try:
                run = await asyncio.to_thread(self.db.get_run, run_id)
                if not run or run["status"] != "queued":
                    continue
                task = asyncio.create_task(self._execute(run_id), name=f"run-{run_id}")
                self._tasks[run_id] = task
                try:
                    await task
                except asyncio.CancelledError:
                    if run_id not in self._user_cancelled:
                        raise  # остановка сервера
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Необработанная ошибка прогона %s", run_id)
            finally:
                self._tasks.pop(run_id, None)
                self._user_cancelled.discard(run_id)
                self._queue.task_done()

    async def _execute(self, run_id: str) -> None:
        run = await asyncio.to_thread(self.db.get_run, run_id)
        assert run is not None
        started = time.time()
        await asyncio.to_thread(self.db.update_run, run_id, status="running", started_at=started)
        await self.bus.publish({"type": "run_update", "run": await asyncio.to_thread(self.db.get_run, run_id)})
        await self._log(run_id, "info", None, "Прогон запущен. Браузер работает на сервере (headless)")

        counts = {"passed": 0, "failed": 0, "error": 0, "cancelled": 0}
        names: list[str] = run["scenarios"]
        try:
            try:
                await self.host.ensure()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._log(run_id, "error", None, f"{INSTALL_HINT}. Причина: {exc}")
                await self._finalize(run_id, "error", error=f"{INSTALL_HINT}. Причина: {exc}")
                return

            for name in names:
                result = await self._run_scenario(run_id, run["video_id"], name)
                counts[result] += 1

            total = len(names)
            status = "passed"
            if counts["error"]:
                status = "error"
            elif counts["failed"]:
                status = "failed"
            summary = {**counts, "total": total, "duration_s": round(time.time() - started, 1)}
            await self._log(
                run_id,
                "ok" if status == "passed" else "error",
                None,
                f"Прогон завершён: успешно {counts['passed']} из {total}, провалено {counts['failed']}, "
                f"ошибок {counts['error']}",
            )
            await self._finalize(run_id, status, summary=summary)
        except asyncio.CancelledError:
            await self._log(run_id, "warn", None, "Прогон отменён")
            await self._finalize(
                run_id, "cancelled",
                summary={**counts, "total": len(names), "duration_s": round(time.time() - started, 1)},
                error="Отменено пользователем" if run_id in self._user_cancelled else "Остановка сервера",
            )
            raise
        except Exception as exc:  # последняя линия защиты: прогон не должен «зависнуть»
            await self._log(run_id, "error", None, f"Внутренняя ошибка: {exc}")
            await self._finalize(run_id, "error", error=str(exc))

    async def _run_scenario(self, run_id: str, video_id: str, name: str) -> str:
        scenario = SCENARIOS[name]
        started = time.monotonic()

        async def log_fn(level: str, message: str) -> None:
            await self._log(run_id, level, name, message)

        async def notify(vid: str) -> None:
            await self.bus.publish({"type": "sandbox", **self.store.counters(vid)})

        await self.bus.publish({"type": "scenario_update", "run_id": run_id, "name": name,
                                "status": "running", "attempt": 1})
        await log_fn("info", f"▶ Сценарий «{scenario.title}»: {scenario.description}")

        status, message, attempts = "error", "", 0
        for attempt in range(1, self.settings.max_attempts + 1):
            attempts = attempt
            blocked: list[str] = []
            ctx = ScenarioContext(
                run_id=run_id, scenario=name, video_id=video_id, settings=self.settings,
                host=self.host, store=self.store,
                shots_dir=self.settings.screenshots_dir / run_id,
                log=log_fn, notify_sandbox=notify, blocked=blocked,
            )
            try:
                message = await asyncio.wait_for(scenario.func(ctx), timeout=self.settings.scenario_timeout_s)
                status = "passed"
                await log_fn("ok", f"✔ {message}")
                break
            except CheckFailed as exc:
                status, message = "failed", str(exc)
                await log_fn("error", f"✘ Сценарий провален: {message}")
                break  # функциональный провал — повтор не нужен
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = "таймаут сценария" if isinstance(exc, asyncio.TimeoutError) else f"{type(exc).__name__}: {exc}"
                reason = reason.splitlines()[0][:300] if reason else "неизвестная ошибка"
                status, message = "error", reason
                log.debug("Трейс сбоя сценария:\n%s", traceback.format_exc())
                if attempt >= self.settings.max_attempts:
                    await log_fn("error", f"Сбой (попытка {attempt}/{self.settings.max_attempts}): {reason}. "
                                          "Попытки исчерпаны")
                    break
                delay = min(2 ** attempt, 8)
                await log_fn("warn", f"Сбой (попытка {attempt}/{self.settings.max_attempts}): {reason}. "
                                     f"Восстановление: перезапуск браузера, повтор через {delay} с")
                await self.bus.publish({"type": "scenario_update", "run_id": run_id, "name": name,
                                        "status": "retrying", "attempt": attempt + 1})
                try:
                    await self.host.restart()
                except asyncio.CancelledError:
                    raise
                except Exception as restart_exc:
                    await log_fn("error", f"Не удалось перезапустить браузер: {restart_exc}")
                await asyncio.sleep(delay)

        duration_ms = int((time.monotonic() - started) * 1000)
        await asyncio.to_thread(self.db.add_scenario_result, run_id, name, status, attempts, duration_ms, message)
        await self.bus.publish({"type": "scenario_update", "run_id": run_id, "name": name, "status": status,
                                "attempt": attempts, "duration_ms": duration_ms, "message": message})
        return status
