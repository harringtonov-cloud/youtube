"""Настройки проекта: сохраняются на сервере (в базе) и переживают перезапуски."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .db import Database
from .scenarios import SCENARIOS

KEY = "project_settings"
DEFAULTS: dict[str, Any] = {
    "repeat": 3,
    "live_idle_minutes": 15,
    "frame_quality": 60,
    "scenarios": ["views", "likes", "comments"],
}
RANGES = {"repeat": (1, 100), "live_idle_minutes": (1, 240), "frame_quality": (30, 90)}


class SettingsError(ValueError):
    pass


def _coerce(name: str, value: Any) -> Any:
    if name in RANGES:
        lo, hi = RANGES[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{name}: нужно целое число")
        number = int(value)
        if not lo <= number <= hi:
            raise SettingsError(f"{name}: допустимо от {lo} до {hi}")
        return number
    if name == "scenarios":
        if not isinstance(value, list) or not all(isinstance(v, str) and v in SCENARIOS for v in value):
            raise SettingsError("scenarios: список известных сценариев")
        return list(dict.fromkeys(value))
    raise SettingsError(f"Неизвестная настройка: {name}")


def load(db: Database) -> dict[str, Any]:
    try:
        stored = json.loads(db.kv_get(KEY) or "{}")
    except ValueError:
        stored = {}
    out = dict(DEFAULTS)
    if isinstance(stored, dict):
        for key in DEFAULTS:
            if key in stored:
                try:
                    out[key] = _coerce(key, stored[key])
                except SettingsError:
                    pass  # испорченное значение в базе — берём значение по умолчанию
    return out


def save(db: Database, patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise SettingsError("Ожидается объект с настройками")
    current = load(db)
    for key, value in patch.items():
        current[key] = _coerce(key, value)  # при ошибке ничего не сохраняется
    db.kv_set(KEY, json.dumps(current, ensure_ascii=False))
    return current


def build_routes(db: Database, authed: Callable[[Any], Any], json_body: Callable[[Request], Any]) -> list[Route]:
    async def get_settings(request: Request) -> Response:
        return JSONResponse(await asyncio.to_thread(load, db))

    async def put_settings(request: Request) -> Response:
        body = await json_body(request)
        try:
            return JSONResponse(await asyncio.to_thread(save, db, body))
        except SettingsError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    return [
        Route("/api/settings", authed(get_settings), methods=["GET"]),
        Route("/api/settings", authed(put_settings), methods=["PUT"]),
    ]
