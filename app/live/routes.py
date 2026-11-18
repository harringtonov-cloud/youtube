"""HTTP-маршруты удалённого браузера (все требуют входа в панель)."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .session import LiveBrowser, LiveError


def build_routes(
    live: LiveBrowser,
    load_settings: Callable[[], dict[str, Any]],
    authed: Callable[[Any], Any],
    json_body: Callable[[Request], Any],
) -> list[Route]:
    def handled(fn: Callable[[Request], Any]) -> Any:
        async def wrapper(request: Request) -> Response:
            try:
                return await fn(request)
            except LiveError as exc:
                return JSONResponse({"error": str(exc)}, status_code=exc.status)

        wrapper.__name__ = fn.__name__
        return authed(wrapper)

    @handled
    async def status(request: Request) -> Response:
        return JSONResponse(live.state())

    @handled
    async def start(request: Request) -> Response:
        cfg = await asyncio.to_thread(load_settings)
        return JSONResponse(await live.start(int(cfg["frame_quality"]), int(cfg["live_idle_minutes"])))

    @handled
    async def stop(request: Request) -> Response:
        return JSONResponse(await live.stop())

    @handled
    async def navigate(request: Request) -> Response:
        data = await json_body(request)
        return JSONResponse(await live.navigate(data.get("url")))

    @handled
    async def action(request: Request) -> Response:
        data = await json_body(request)
        kind = data.pop("kind", None)
        return JSONResponse(await live.act(kind, data))

    @handled
    async def frame(request: Request) -> Response:
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError:
            after = 0
        data, seq = live.frame()
        live.touch()  # пока экран смотрят, браузер не считается простаивающим
        if data is None or seq <= after:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Frame-Seq": str(seq)},
        )

    return [
        Route("/api/live/status", status, methods=["GET"]),
        Route("/api/live/start", start, methods=["POST"]),
        Route("/api/live/stop", stop, methods=["POST"]),
        Route("/api/live/navigate", navigate, methods=["POST"]),
        Route("/api/live/action", action, methods=["POST"]),
        Route("/api/live/frame", frame, methods=["GET"]),
    ]
