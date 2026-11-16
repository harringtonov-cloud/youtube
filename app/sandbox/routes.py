"""HTTP-маршруты песочницы. Доступны только браузеру Playwright (cookie `sbkey`)."""

from __future__ import annotations

import hmac
import json
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from ..config import Settings
from ..events import EventBus
from ..urlutils import is_valid_video_id
from .store import SandboxStore

SID_COOKIE = "sbsid"
KEY_COOKIE = "sbkey"
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def build_routes(settings: Settings, store: SandboxStore, bus: EventBus) -> list[Route]:
    def key_ok(request: Request) -> bool:
        provided = request.cookies.get(KEY_COOKIE, "")
        return hmac.compare_digest(provided.encode(), settings.sandbox_key.encode())

    def deny() -> JSONResponse:
        return JSONResponse({"error": "sandbox_forbidden"}, status_code=403)

    def get_sid(request: Request) -> tuple[str, bool]:
        sid = request.cookies.get(SID_COOKIE, "")
        if 8 <= len(sid) <= 64 and sid.isalnum():
            return sid, False
        return secrets.token_hex(8), True

    async def read_json(request: Request) -> dict[str, Any]:
        try:
            body = await request.body()
            if len(body) > 8192:
                return {}
            data = json.loads(body or b"{}")
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def with_sid(response: Response, sid: str, is_new: bool) -> Response:
        if is_new:
            response.set_cookie(SID_COOKIE, sid, httponly=True, samesite="strict", path="/sandbox")
        return response

    async def publish_counters(vid: str) -> None:
        await bus.publish({"type": "sandbox", **store.counters(vid)})

    async def watch(request: Request) -> Response:
        if not key_ok(request):
            return deny()
        vid = request.query_params.get("v", "")
        if not is_valid_video_id(vid):
            return JSONResponse({"error": "bad_video_id"}, status_code=400)
        sid, is_new = get_sid(request)
        resp = _templates.TemplateResponse(
            request,
            "watch.html",
            {"vid": vid, "threshold_ms": int(store.view_threshold_s * 1000)},
        )
        return with_sid(resp, sid, is_new)

    async def api_stats(request: Request) -> Response:
        if not key_ok(request):
            return deny()
        vid = request.query_params.get("vid", "")
        if not is_valid_video_id(vid):
            return JSONResponse({"error": "bad_video_id"}, status_code=400)
        sid, is_new = get_sid(request)
        resp = JSONResponse(store.stats(vid, sid))
        return with_sid(resp, sid, is_new)

    def make_mutation(
        handler: Callable[[str, str, dict[str, Any]], tuple[int, dict[str, Any]]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            if not key_ok(request):
                return deny()
            data = await read_json(request)
            vid = data.get("vid", "")
            if not isinstance(vid, str) or not is_valid_video_id(vid):
                return JSONResponse({"error": "bad_video_id"}, status_code=400)
            # имитация сбоя бэкенда: первые N мутирующих запросов получают 503
            if store.consume_flaky():
                return JSONResponse({"error": "unavailable"}, status_code=503)
            sid, is_new = get_sid(request)
            status, payload = handler(vid, sid, data)
            resp = JSONResponse(payload, status_code=status)
            with_sid(resp, sid, is_new)
            if status < 400:
                await publish_counters(vid)
            return resp

        return endpoint

    api_play = make_mutation(lambda vid, sid, d: (200, store.play(vid, sid)))
    api_view = make_mutation(lambda vid, sid, d: (200, store.view(vid, sid)))
    api_like = make_mutation(lambda vid, sid, d: (200, store.set_like(vid, sid, bool(d.get("liked")))))
    api_comment = make_mutation(lambda vid, sid, d: store.add_comment(vid, sid, d.get("text")))

    return [
        Route("/sandbox/watch", watch, methods=["GET"]),
        Route("/sandbox/api/stats", api_stats, methods=["GET"]),
        Route("/sandbox/api/play", api_play, methods=["POST"]),
        Route("/sandbox/api/view", api_view, methods=["POST"]),
        Route("/sandbox/api/like", api_like, methods=["POST"]),
        Route("/sandbox/api/comment", api_comment, methods=["POST"]),
    ]
