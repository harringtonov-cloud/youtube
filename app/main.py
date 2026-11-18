"""Веб-панель управления (Starlette): REST API + SSE для логов в реальном времени."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import __version__, keepalive
from .config import get_settings
from .db import Database
from .events import EventBus
from .browser import playwright_available
from .live.routes import build_routes as build_live_routes
from .live.session import LiveBrowser
from .runner import RunManager
from .sandbox.routes import build_routes as build_sandbox_routes
from .sandbox.store import SandboxStore
from .scenarios import SCENARIOS
from .settings_store import build_routes as build_settings_routes
from .settings_store import load as load_project_settings
from .youtube.oauth import OAuthManager
from .youtube.routes import build_routes as build_youtube_routes
from .youtube.service import YouTubeService
from .urlutils import InvalidYouTubeUrl, extract_video_id, is_valid_video_id

APP_DIR = Path(__file__).parent
settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("yt-tester")

bus = EventBus()
db = Database(settings.db_path)
store = SandboxStore(settings.sandbox_view_threshold_ms)
runner = RunManager(settings, db, bus, store)
oauth = OAuthManager(settings, db, bus)
youtube = YouTubeService(settings, db, bus, oauth)
live = LiveBrowser(settings, bus)  # ручной удалённый браузер (не участвует в автотестах)
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

COOKIE_NAME = "yt_admin"
_TOKEN_DIGEST = hashlib.sha256(settings.admin_token.encode()).hexdigest()
_RUN_ID_RE = re.compile(r"^[a-f0-9]{12}$")
_SHOT_RE = re.compile(r"^[A-Za-z0-9_.-]+\.png$")
_login_failures: dict[str, list[float]] = defaultdict(list)
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_S = 300


# --- авторизация --------------------------------------------------------------
def is_authed(request: Request) -> bool:
    cookie = request.cookies.get(COOKIE_NAME, "")
    if cookie and hmac.compare_digest(cookie, _TOKEN_DIGEST):
        return True
    header = request.headers.get("x-admin-token", "")
    return bool(header) and hmac.compare_digest(
        hashlib.sha256(header.encode()).hexdigest(), _TOKEN_DIGEST
    )


Endpoint = Callable[[Request], Awaitable[Response]]


def authed(fn: Endpoint) -> Endpoint:
    async def wrapper(request: Request) -> Response:
        if not is_authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await fn(request)

    wrapper.__name__ = fn.__name__
    return wrapper


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def json_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# --- страницы -----------------------------------------------------------------
async def index(request: Request) -> Response:
    return templates.TemplateResponse(request, "index.html", {"version": __version__})


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok", "version": __version__, "browser_connected": runner.host.connected,
                         "playwright": playwright_available()})


# --- вход/выход ---------------------------------------------------------------
async def login(request: Request) -> Response:
    ip = client_ip(request)
    now = time.time()
    fails = [t for t in _login_failures[ip] if now - t < LOGIN_WINDOW_S]
    _login_failures[ip] = fails
    if len(fails) >= LOGIN_MAX_FAILS:
        return err("Слишком много попыток, подождите несколько минут", 429)
    data = await json_body(request)
    token = str(data.get("token", ""))
    if token and hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), _TOKEN_DIGEST):
        _login_failures.pop(ip, None)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            COOKIE_NAME, _TOKEN_DIGEST, httponly=True, samesite="strict",
            secure=settings.cookie_secure, max_age=60 * 60 * 24 * 30, path="/",
        )
        return resp
    fails.append(now)
    return err("Неверный токен", 401)


async def logout(request: Request) -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@authed
async def me(request: Request) -> Response:
    return JSONResponse({"ok": True, "version": __version__, "playwright": playwright_available()})


# --- сценарии и прогоны -------------------------------------------------------
@authed
async def list_scenarios(request: Request) -> Response:
    return JSONResponse(
        [{"name": s.name, "title": s.title, "description": s.description} for s in SCENARIOS.values()]
    )


@authed
async def parse_url(request: Request) -> Response:
    try:
        return JSONResponse({"video_id": extract_video_id(request.query_params.get("url", ""))})
    except InvalidYouTubeUrl as exc:
        return err(str(exc))


@authed
async def create_run(request: Request) -> Response:
    data = await json_body(request)
    names = data.get("scenarios")
    if not isinstance(names, list):
        return err("Поле scenarios должно быть списком")
    project = await asyncio.to_thread(load_project_settings, db)
    raw_opts = data.get("options") if isinstance(data.get("options"), dict) else {}
    options = {"repeat": raw_opts.get("repeat", project["repeat"])}
    try:
        run = await runner.submit(str(data.get("url", "")), [str(n) for n in names], options)
    except InvalidYouTubeUrl as exc:
        return err(str(exc))
    except ValueError as exc:
        return err(str(exc))
    return JSONResponse(run, status_code=201)


@authed
async def runs_collection(request: Request) -> Response:
    if request.method == "DELETE":
        ids = await asyncio.to_thread(db.clear_finished)
        for rid in ids:
            runner.remove_artifacts(rid)
        return JSONResponse({"deleted": len(ids)})
    try:
        limit = min(max(int(request.query_params.get("limit", "50")), 1), 200)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        return err("Некорректные параметры")
    return JSONResponse(await asyncio.to_thread(db.list_runs, limit, offset))


def _run_id(request: Request) -> str | None:
    rid = request.path_params["run_id"]
    return rid if _RUN_ID_RE.match(rid) else None


@authed
async def run_item(request: Request) -> Response:
    rid = _run_id(request)
    if rid is None:
        return err("Некорректный id", 404)
    run = await asyncio.to_thread(db.get_run, rid)
    if run is None:
        return err("Прогон не найден", 404)
    if request.method == "DELETE":
        if run["status"] in {"queued", "running"}:
            return err("Сначала отмените активный прогон", 409)
        await asyncio.to_thread(db.delete_run, rid)
        runner.remove_artifacts(rid)
        return JSONResponse({"deleted": rid})
    return JSONResponse(run)


@authed
async def run_logs(request: Request) -> Response:
    rid = _run_id(request)
    if rid is None:
        return err("Некорректный id", 404)
    try:
        after = max(int(request.query_params.get("after", "0")), 0)
    except ValueError:
        after = 0
    return JSONResponse(await asyncio.to_thread(db.get_logs, rid, after))


@authed
async def run_cancel(request: Request) -> Response:
    rid = _run_id(request)
    if rid is None:
        return err("Некорректный id", 404)
    if not await runner.cancel(rid):
        return err("Прогон не активен", 409)
    return JSONResponse({"cancelled": rid})


@authed
async def run_screenshots(request: Request) -> Response:
    rid = _run_id(request)
    if rid is None:
        return err("Некорректный id", 404)
    folder = settings.screenshots_dir / rid
    names = sorted(p.name for p in folder.glob("*.png")) if folder.is_dir() else []
    return JSONResponse(names)


@authed
async def run_screenshot_file(request: Request) -> Response:
    rid = _run_id(request)
    name = request.path_params["name"]
    if rid is None or not _SHOT_RE.match(name):
        return err("Не найдено", 404)
    path = (settings.screenshots_dir / rid / name).resolve()
    if settings.screenshots_dir.resolve() not in path.parents or not path.is_file():
        return err("Не найдено", 404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@authed
async def sandbox_stats(request: Request) -> Response:
    vid = request.path_params["vid"]
    if not is_valid_video_id(vid):
        return err("Некорректный ID", 400)
    return JSONResponse(store.counters(vid))


# --- события реального времени (SSE) ------------------------------------------
@authed
async def events(request: Request) -> Response:
    queue = bus.subscribe()

    async def stream() -> AsyncIterator[str]:
        try:
            yield "retry: 3000\n\n"
            yield f"data: {json.dumps({'type': 'hello', 'version': __version__})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# --- сборка приложения --------------------------------------------------------
@contextlib.asynccontextmanager
async def lifespan(app: Starlette) -> AsyncIterator[None]:
    if settings.token_was_generated:
        log.warning("ADMIN_TOKEN не задан. Сгенерированный токен для входа: %s", settings.admin_token)
    await runner.start()
    youtube.start_poller()
    background: list[asyncio.Task[None]] = []
    if await asyncio.to_thread(oauth.bootstrap_from_env):
        log.info("Вход через Google восстановлен из YT_REFRESH_TOKEN")

        async def restore_profile() -> None:
            try:
                await asyncio.to_thread(oauth.fetch_profile)
            except Exception as exc:  # noqa: BLE001 — например, токен отозван: пользователь увидит это в панели
                log.warning("Не удалось подтвердить восстановленный вход: %s", exc)

        background.append(asyncio.create_task(restore_profile()))
    if settings.yt_watch_videos:
        added = await youtube.bootstrap_watch(settings.yt_watch_videos)
        log.info("Список отслеживания: добавлено из YT_WATCH_VIDEOS — %d", added)
    if settings.keepalive_url:
        background.append(asyncio.create_task(keepalive.run(settings.keepalive_url, settings.keepalive_interval_min)))
    log.info("Панель запущена: http://%s:%d  (песочница: %s)", settings.host, settings.port, settings.sandbox_base_url)
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await youtube.stop_poller()
        await live.shutdown()
        await runner.stop()
        db.close()


routes = [
    Route("/", index),
    Route("/api/health", health),
    Route("/api/login", login, methods=["POST"]),
    Route("/api/logout", logout, methods=["POST"]),
    Route("/api/me", me),
    Route("/api/scenarios", list_scenarios),
    Route("/api/parse", parse_url),
    Route("/api/runs", create_run, methods=["POST"]),
    Route("/api/runs", runs_collection, methods=["GET", "DELETE"]),
    Route("/api/runs/{run_id}", run_item, methods=["GET", "DELETE"]),
    Route("/api/runs/{run_id}/logs", run_logs),
    Route("/api/runs/{run_id}/cancel", run_cancel, methods=["POST"]),
    Route("/api/runs/{run_id}/screenshots", run_screenshots),
    Route("/api/runs/{run_id}/screenshots/{name}", run_screenshot_file),
    Route("/api/sandbox/{vid}", sandbox_stats),
    Route("/api/events", events),
    *build_sandbox_routes(settings, store, bus),
    *build_youtube_routes(youtube, oauth, authed, json_body),
    *build_live_routes(live, lambda: load_project_settings(db), authed, json_body),
    *build_settings_routes(db, authed, json_body),
    Mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static"),
]

app = Starlette(routes=routes, lifespan=lifespan)
