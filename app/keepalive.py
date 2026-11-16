"""Keep-alive: периодический запрос к собственному публичному адресу.

Бесплатные хостинги (например, Render Free) усыпляют сервис без входящего трафика. Запрос на
публичный адрес проходит через их маршрутизатор и считается входящим трафиком. Если сервис всё же
уснул (перезапуск, обновление), его разбудит любое открытие страницы или внешний пингер (см. README).
"""

from __future__ import annotations

import asyncio
import logging

from .youtube.http import ApiError, call

log = logging.getLogger("yt-tester.keepalive")


async def run(base_url: str, interval_min: float) -> None:
    target = base_url.rstrip("/") + "/api/health"
    log.info("Keep-alive включён: %s каждые %s мин", target, interval_min)
    fails = 0
    while True:
        await asyncio.sleep(interval_min * 60)
        try:
            await asyncio.to_thread(call, "GET", target, timeout=20, retries=0)
            if fails:
                log.info("Keep-alive восстановился")
            fails = 0
        except ApiError as exc:
            fails += 1
            log.warning("Keep-alive не удался (%d подряд): %s", fails, exc)
