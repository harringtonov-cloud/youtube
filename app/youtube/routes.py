"""HTTP-маршруты вкладки «YouTube API»."""

from __future__ import annotations

import asyncio
import html
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..urlutils import InvalidYouTubeUrl, extract_video_id, is_valid_video_id
from .http import ApiError
from .oauth import OAuthManager
from .service import YouTubeService

Endpoint = Callable[[Request], Awaitable[Response]]

# Понятные пояснения к типовым ошибкам Google. Остальные показываются как пришли.
REASON_TEXT = {
    "quotaExceeded": "Суточная квота YouTube API исчерпана. Она обнуляется в полночь по тихоокеанскому времени",
    "dailyLimitExceeded": "Суточный лимит YouTube API исчерпан",
    "commentsDisabled": "Владелец отключил комментарии под этим видео",
    "videoNotFound": "Видео не найдено, удалено или недоступно",
    "forbidden": "Google отказал в доступе (403). Частые причины: у API-ключа стоят ограничения (по сайту/IP или не включён YouTube Data API v3), "
                 "ключ из другого проекта, либо видео приватное",
    "accessNotConfigured": "YouTube Data API v3 не включён в проекте этого ключа",
    "keyInvalid": "API-ключ неверный: проверьте YOUTUBE_API_KEY",
    "badRequest": "Google отклонил запрос (400). Проверьте API-ключ",
    "insufficientPermissions": "Недостаточно прав у текущего входа. Выйдите и войдите заново",
    "processingFailure": "YouTube не смог обработать запрос, попробуйте позже",
    "invalid_client": "Неверные GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET",
    "access_denied": "Доступ отклонён",
    "network": "Нет связи с серверами Google",
}


def api_error_response(exc: ApiError) -> JSONResponse:
    status = exc.status if 400 <= exc.status < 600 else 502
    text = REASON_TEXT.get(exc.reason, exc.message)
    if exc.reason in {"forbidden", "badRequest", "accessNotConfigured"} and exc.message and exc.message not in text:
        text += f". Ответ Google: «{exc.message[:300]}»"  # настоящая причина, чтобы не гадать
    return JSONResponse({"error": text, "reason": exc.reason, "detail": exc.message}, status_code=status)


def build_routes(
    service: YouTubeService,
    oauth: OAuthManager,
    authed: Callable[[Endpoint], Endpoint],
    json_body: Callable[[Request], Awaitable[dict[str, Any]]],
) -> list[Route]:
    def handled(fn: Endpoint) -> Endpoint:
        """Единая обработка ошибок API для всех эндпоинтов."""
        async def wrapper(request: Request) -> Response:
            try:
                return await fn(request)
            except ApiError as exc:
                return api_error_response(exc)
            except InvalidYouTubeUrl as exc:
                return JSONResponse({"error": str(exc), "reason": "bad_url"}, status_code=400)
        wrapper.__name__ = fn.__name__
        return authed(wrapper)

    def video_id_from(value: Any) -> str:
        value = str(value or "").strip()
        return value if is_valid_video_id(value) else extract_video_id(value)

    @handled
    async def status(request: Request) -> Response:
        return JSONResponse(await service.status())

    @handled
    async def device_start(request: Request) -> Response:
        await oauth.start_device()
        return JSONResponse(await service.status())

    @handled
    async def device_cancel(request: Request) -> Response:
        await oauth.cancel_device()
        return JSONResponse(await service.status())

    @handled
    async def web_start(request: Request) -> Response:
        return JSONResponse({"url": oauth.web_start()})

    @handled
    async def logout(request: Request) -> Response:
        await oauth.logout()
        return JSONResponse(await service.status())

    @handled
    async def export_tokens(request: Request) -> Response:
        return JSONResponse(await asyncio.to_thread(oauth.export_tokens))

    async def oauth_callback(request: Request) -> Response:
        # Без cookie панели: Google возвращает пользователя кросс-сайтовым переходом.
        # Защита от подделки — одноразовый state, созданный авторизованным администратором.
        q = request.query_params
        if q.get("error"):
            msg = html.escape(q.get("error", ""))
            return RedirectResponse(f"/?yt_error={msg}#youtube", status_code=303)
        try:
            await oauth.web_finish(q.get("code", ""), q.get("state", ""))
        except ApiError as exc:
            return HTMLResponse(
                f"<meta charset=utf-8><p>Не удалось завершить вход: {html.escape(exc.message)}</p>"
                '<p><a href="/">Вернуться в панель</a></p>', status_code=400,
            )
        return RedirectResponse("/?yt=connected#youtube", status_code=303)

    @handled
    async def video(request: Request) -> Response:
        vid = video_id_from(request.query_params.get("url") or request.query_params.get("id"))
        info = await service.video(vid)
        info["my_rating"] = None
        info["my_rating_error"] = None
        try:
            info["my_rating"] = await service.my_rating(vid)
        except ApiError as exc:  # оценка — дополнение, не причина ронять ответ
            info["my_rating_error"] = exc.message
        return JSONResponse(info)

    @handled
    async def comments(request: Request) -> Response:
        vid = video_id_from(request.query_params.get("video"))
        data = await service.comments(
            vid, request.query_params.get("page") or None, request.query_params.get("order", "relevance")
        )
        return JSONResponse(data)

    @handled
    async def rate(request: Request) -> Response:
        d = await json_body(request)
        return JSONResponse(await service.rate(video_id_from(d.get("video")), str(d.get("rating", ""))))

    @handled
    async def post_comment(request: Request) -> Response:
        d = await json_body(request)
        return JSONResponse(await service.post_comment(video_id_from(d.get("video")), d.get("text")), status_code=201)

    @handled
    async def reply(request: Request) -> Response:
        d = await json_body(request)
        parent = str(d.get("parent", ""))
        if not parent or len(parent) > 100:
            raise ApiError(400, "bad_request", "Не указан комментарий для ответа")
        return JSONResponse(
            await service.reply_comment(video_id_from(d.get("video")), parent, d.get("text")), status_code=201
        )

    @handled
    async def delete_comment(request: Request) -> Response:
        cid = request.path_params["cid"]
        if not cid or len(cid) > 100:
            raise ApiError(400, "bad_request", "Некорректный id комментария")
        await service.delete_comment(video_id_from(request.query_params.get("video")), cid)
        return JSONResponse({"deleted": cid})

    @handled
    async def update_video(request: Request) -> Response:
        d = await json_body(request)
        return JSONResponse(
            await service.update_video(video_id_from(d.get("video")), d.get("title"), d.get("description"))
        )

    @handled
    async def watch(request: Request) -> Response:
        from asyncio import to_thread
        if request.method == "POST":
            d = await json_body(request)
            return JSONResponse(await service.add_watch(video_id_from(d.get("video"))), status_code=201)
        return JSONResponse(await to_thread(service.db.watch_list))

    @handled
    async def watch_item(request: Request) -> Response:
        vid = request.path_params["vid"]
        if not is_valid_video_id(vid):
            raise ApiError(400, "bad_request", "Некорректный ID видео")
        if request.method == "DELETE":
            return JSONResponse({"removed": await asyncio.to_thread(service.db.watch_remove, vid)})
        return JSONResponse(await asyncio.to_thread(service.db.list_snapshots, vid))

    @handled
    async def watch_refresh(request: Request) -> Response:
        return JSONResponse(await service.poll_once())

    @handled
    async def actions(request: Request) -> Response:
        try:
            limit = min(max(int(request.query_params.get("limit", "50")), 1), 200)
        except ValueError:
            limit = 50
        return JSONResponse(await asyncio.to_thread(service.db.list_yt_actions, limit))

    return [
        Route("/api/yt/status", status),
        Route("/api/yt/auth/device/start", device_start, methods=["POST"]),
        Route("/api/yt/auth/device/cancel", device_cancel, methods=["POST"]),
        Route("/api/yt/auth/web/start", web_start),
        Route("/api/yt/auth/logout", logout, methods=["POST"]),
        Route("/api/yt/auth/export", export_tokens, methods=["POST"]),
        Route("/oauth/callback", oauth_callback),
        Route("/api/yt/video", video),
        Route("/api/yt/comments", comments),
        Route("/api/yt/rate", rate, methods=["POST"]),
        Route("/api/yt/comment", post_comment, methods=["POST"]),
        Route("/api/yt/reply", reply, methods=["POST"]),
        Route("/api/yt/comment/{cid}", delete_comment, methods=["DELETE"]),
        Route("/api/yt/video/update", update_video, methods=["POST"]),
        Route("/api/yt/watch", watch, methods=["GET", "POST"]),
        Route("/api/yt/watch/refresh", watch_refresh, methods=["POST"]),
        Route("/api/yt/watch/{vid}", watch_item, methods=["GET", "DELETE"]),
        Route("/api/yt/actions", actions),
    ]
