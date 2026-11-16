"""Сервис YouTube Data API v3: реальные данные, разрешённые операции, квота, история.

Принципы:
  * только официальный API; значения, которых API не отдаёт, возвращаются как None
    (никаких выдуманных чисел);
  * просмотры накрутить через API невозможно — такой операции в API нет, и здесь её нет;
  * операции записи (лайк, комментарии, правка метаданных) выполняются только по явному
    действию пользователя, с суточным лимитом и защитой от двойного нажатия;
  * каждый вызов API пишется в историю с расходом квоты.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings
from ..db import Database
from ..events import EventBus
from .http import ApiError, call
from .oauth import SCOPE_FORCE_SSL, SCOPE_READONLY, SCOPE_YOUTUBE, OAuthManager

log = logging.getLogger("yt-tester.youtube")

# Стоимость методов в единицах квоты (официальная таблица Google).
UNITS = {
    "videos.list": 1, "videos.getRating": 1, "channels.list": 1,
    "commentThreads.list": 1, "videos.rate": 50, "commentThreads.insert": 50,
    "comments.insert": 50, "comments.delete": 50, "videos.update": 50,
}
READ_SCOPES = (SCOPE_READONLY, SCOPE_YOUTUBE, SCOPE_FORCE_SSL)
RATE_SCOPES = (SCOPE_YOUTUBE, SCOPE_FORCE_SSL)
COMMENT_SCOPES = (SCOPE_FORCE_SSL,)
UPDATE_SCOPES = (SCOPE_YOUTUBE, SCOPE_FORCE_SSL)

COMMENT_SCOPE_HINT = (
    "Комментарии требуют право youtube.force-ssl. Его даёт только веб-вход через HTTPS-адрес "
    "(device-вход по коду Google для этого права не разрешает)."
)
WRITE_COOLDOWN_S = 10.0  # защита от двойного нажатия по одной и той же операции
MAX_COMMENT_LEN = 10_000
_PT = ZoneInfo("America/Los_Angeles")
_DURATION_RE = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def pt_day_start(now: float | None = None) -> float:
    """Начало текущих суток по тихоокеанскому времени: в это время Google обнуляет квоту."""
    dt = datetime.fromtimestamp(now if now is not None else time.time(), _PT)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def pt_next_reset(now: float | None = None) -> float:
    dt = datetime.fromtimestamp(pt_day_start(now), _PT) + timedelta(days=1)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def parse_duration(iso: str | None) -> int | None:
    if not iso:
        return None
    m = _DURATION_RE.match(iso)
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_video(item: dict[str, Any]) -> dict[str, Any]:
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    thumbs = snippet.get("thumbnails", {})
    thumb = next((thumbs[k]["url"] for k in ("high", "medium", "default") if k in thumbs), None)
    return {
        "id": item.get("id"),
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "channel_id": snippet.get("channelId"),
        "channel_title": snippet.get("channelTitle"),
        "published_at": snippet.get("publishedAt"),
        "category_id": snippet.get("categoryId"),
        "tags": snippet.get("tags"),
        "default_language": snippet.get("defaultLanguage"),
        "thumbnail": thumb,
        "live": snippet.get("liveBroadcastContent"),
        "duration_s": parse_duration(item.get("contentDetails", {}).get("duration")),
        "privacy": item.get("status", {}).get("privacyStatus"),
        # None = API это поле не вернул (например, владелец скрыл лайки). Не подменяем нулём.
        "views": _int_or_none(stats.get("viewCount")),
        "likes": _int_or_none(stats.get("likeCount")),
        "comments": _int_or_none(stats.get("commentCount")),
    }


def normalize_comment(c: dict[str, Any]) -> dict[str, Any]:
    s = c.get("snippet", {})
    return {
        "id": c.get("id"),
        "author": s.get("authorDisplayName"),
        "author_channel_id": (s.get("authorChannelId") or {}).get("value"),
        "text": s.get("textDisplay") or s.get("textOriginal") or "",
        "likes": _int_or_none(s.get("likeCount")),
        "published_at": s.get("publishedAt"),
        "updated_at": s.get("updatedAt"),
    }


class YouTubeService:
    def __init__(self, settings: Settings, db: Database, bus: EventBus, oauth: OAuthManager) -> None:
        self.s = settings
        self.db = db
        self.bus = bus
        self.oauth = oauth
        self._poller: asyncio.Task[None] | None = None
        self._poll_lock = asyncio.Lock()

    # --- состояние -------------------------------------------------------------
    @property
    def can_read(self) -> bool:
        return self.oauth.connected or bool(self.s.youtube_api_key)

    async def status(self) -> dict[str, Any]:
        used = await asyncio.to_thread(self.db.yt_units_since, pt_day_start())
        writes = await asyncio.to_thread(self.db.yt_writes_since, pt_day_start())
        scopes = sorted(self.oauth.granted_scopes())
        return {
            "api_key": bool(self.s.youtube_api_key),
            "oauth_client": self.oauth.client_configured,
            "web_login_available": self.oauth.web_configured,
            "connected": self.oauth.connected,
            "channel": self.oauth.profile(),
            "scopes": scopes,
            "can_rate": self.oauth.connected and self.oauth.has_scope(*RATE_SCOPES),
            "can_comment": self.oauth.connected and self.oauth.has_scope(*COMMENT_SCOPES),
            "can_update": self.oauth.connected and self.oauth.has_scope(*UPDATE_SCOPES),
            "device": self.oauth.device_state(),
            "quota": {
                "used_estimate": used, "limit": self.s.yt_daily_quota,
                "resets_at": pt_next_reset(), "estimated": True,
            },
            "writes": {"used": writes, "limit": self.s.yt_write_limit_per_day},
        }

    # --- ядро вызова -----------------------------------------------------------
    async def _record(self, kind: str, name: str, video_id: str | None, ok: bool,
                      status: int | None, units: int, detail: str) -> None:
        try:
            action = await asyncio.to_thread(self.db.add_yt_action, kind, name, video_id, ok, status, units, detail)
            await self.bus.publish({"type": "yt_action", "action": action})
        except Exception:  # noqa: BLE001 — журнал не должен ломать операцию
            log.exception("Не удалось записать действие в журнал")

    def _sync_request(self, method: str, path: str, *, params: dict[str, Any] | None,
                      body: Any, need_scopes: tuple[str, ...] | None, require_oauth: bool) -> tuple[int, Any]:
        params = dict(params or {})
        headers: dict[str, str] = {}
        if self.oauth.connected:
            if need_scopes and not self.oauth.has_scope(*need_scopes):
                raise ApiError(
                    403, "insufficient_scope",
                    COMMENT_SCOPE_HINT if need_scopes == COMMENT_SCOPES
                    else "У текущего входа нет нужных прав для этой операции. Выйдите и войдите заново.",
                )
            headers["Authorization"] = f"Bearer {self.oauth.get_access_token()}"
        elif require_oauth or need_scopes:
            raise ApiError(401, "not_authenticated", "Эта операция требует входа через Google")
        elif self.s.youtube_api_key:
            params["key"] = self.s.youtube_api_key
        else:
            raise ApiError(401, "not_configured", "Задайте YOUTUBE_API_KEY или войдите через Google")
        return call(method, f"{self.s.youtube_api_base}/{path}", params=params, headers=headers, json_body=body)

    async def _api(self, name: str, method: str, path: str, *, params: dict[str, Any] | None = None,
                   body: Any = None, video_id: str | None = None, kind: str = "read",
                   need_scopes: tuple[str, ...] | None = None, require_oauth: bool = False,
                   detail: str = "") -> Any:
        units = UNITS[name]
        try:
            status, data = await asyncio.to_thread(
                self._sync_request, method, path, params=params, body=body,
                need_scopes=need_scopes, require_oauth=require_oauth,
            )
        except ApiError as exc:
            # локальные отказы (нет прав/входа) квоту не тратят; ответ с ошибкой от Google — минимум 1 ед.
            spent = 0 if exc.status == 0 or exc.reason in {"insufficient_scope", "not_authenticated", "not_configured", "invalid_grant"} else 1
            await self._record(kind, name, video_id, False, exc.status, spent, f"{exc.reason}: {exc.message}")
            raise
        await self._record(kind, name, video_id, True, status, units, detail)
        return data

    async def _check_write(self, name: str, video_id: str) -> None:
        day = pt_day_start()
        if await asyncio.to_thread(self.db.yt_writes_since, day) >= self.s.yt_write_limit_per_day:
            raise ApiError(
                429, "local_limit",
                f"Достигнут суточный лимит операций записи ({self.s.yt_write_limit_per_day}). "
                "Он защищает аккаунт и квоту; изменить: YT_WRITE_LIMIT_PER_DAY",
            )
        last = await asyncio.to_thread(self.db.yt_last_write, name, video_id)
        if last and time.time() - last < WRITE_COOLDOWN_S:
            raise ApiError(429, "cooldown", "Эта операция только что выполнялась. Подождите несколько секунд")

    # --- чтение ---------------------------------------------------------------------
    async def videos(self, ids: list[str], parts: str = "snippet,statistics,contentDetails,status") -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(ids), 50):  # API принимает до 50 id за запрос
            chunk = ids[i:i + 50]
            data = await self._api(
                "videos.list", "GET", "videos", params={"part": parts, "id": ",".join(chunk)},
                video_id=chunk[0] if len(chunk) == 1 else None,
                detail=f"{len(chunk)} видео",
            )
            out.extend(normalize_video(it) for it in (data or {}).get("items", []))
        return out

    async def video(self, video_id: str) -> dict[str, Any]:
        items = await self.videos([video_id])
        if not items:
            raise ApiError(404, "videoNotFound", "Видео не найдено, удалено или недоступно этому аккаунту")
        return items[0]

    async def my_rating(self, video_id: str) -> str | None:
        """like / dislike / none — оценка вошедшего пользователя. None — не определить."""
        if not (self.oauth.connected and self.oauth.has_scope(*READ_SCOPES)):
            return None
        data = await self._api(
            "videos.getRating", "GET", "videos/getRating", params={"id": video_id},
            video_id=video_id, require_oauth=True,
        )
        items = (data or {}).get("items") or []
        return items[0].get("rating") if items else None

    async def comments(self, video_id: str, page_token: str | None = None, order: str = "relevance") -> dict[str, Any]:
        if order not in {"relevance", "time"}:
            raise ApiError(400, "bad_request", "order: relevance или time")
        data = await self._api(
            "commentThreads.list", "GET", "commentThreads",
            params={"part": "snippet,replies", "videoId": video_id, "maxResults": 20, "order": order,
                    "textFormat": "plainText", "pageToken": page_token},
            video_id=video_id,
        )
        threads = []
        for t in (data or {}).get("items", []):
            top = t.get("snippet", {}).get("topLevelComment", {})
            item = normalize_comment(top)
            item["reply_count"] = _int_or_none(t.get("snippet", {}).get("totalReplyCount")) or 0
            item["replies"] = [normalize_comment(r) for r in (t.get("replies") or {}).get("comments", [])]
            threads.append(item)
        return {"items": threads, "next_page": (data or {}).get("nextPageToken")}

    # --- записывающие операции (только по явному действию пользователя) ------------------
    async def rate(self, video_id: str, rating: str) -> dict[str, Any]:
        if rating not in {"like", "none"}:
            raise ApiError(400, "bad_request", "Доступно: like (поставить) или none (снять)")
        if not (self.oauth.connected and self.oauth.has_scope(*RATE_SCOPES)):
            raise ApiError(403, "insufficient_scope", "Для лайка нужен вход через Google")
        current = await self.my_rating(video_id)
        if current == rating or (rating == "none" and current in {None, "none"}):
            return {"rating": current or "none", "changed": False}
        await self._check_write("videos.rate", video_id)
        await self._api(
            "videos.rate", "POST", "videos/rate", params={"id": video_id, "rating": rating},
            video_id=video_id, kind="write", need_scopes=RATE_SCOPES,
            detail="лайк поставлен" if rating == "like" else "оценка снята",
        )
        return {"rating": rating, "changed": True}

    def _require(self, scopes: tuple[str, ...], message: str) -> None:
        """Проверка прав ДО обращения к API: лишний запрос не тратит квоту."""
        if not self.oauth.connected:
            raise ApiError(401, "not_authenticated", "Эта операция требует входа через Google")
        if not self.oauth.has_scope(*scopes):
            raise ApiError(403, "insufficient_scope", message)

    @staticmethod
    def _clean_text(text: Any) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ApiError(400, "bad_request", "Текст не может быть пустым")
        text = text.strip()
        if len(text) > MAX_COMMENT_LEN:
            raise ApiError(400, "bad_request", f"Текст длиннее {MAX_COMMENT_LEN} символов")
        return text

    async def post_comment(self, video_id: str, text: Any) -> dict[str, Any]:
        text = self._clean_text(text)
        self._require(COMMENT_SCOPES, COMMENT_SCOPE_HINT)
        video = await self.video(video_id)  # нужен channelId владельца видео (по документации API)
        await self._check_write("commentThreads.insert", video_id)
        data = await self._api(
            "commentThreads.insert", "POST", "commentThreads", params={"part": "snippet"},
            body={"snippet": {"channelId": video["channel_id"], "videoId": video_id,
                              "topLevelComment": {"snippet": {"textOriginal": text}}}},
            video_id=video_id, kind="write", need_scopes=COMMENT_SCOPES, detail=f"комментарий: {text[:80]}",
        )
        top = (data or {}).get("snippet", {}).get("topLevelComment", {})
        return normalize_comment(top)

    async def reply_comment(self, video_id: str, parent_id: str, text: Any) -> dict[str, Any]:
        text = self._clean_text(text)
        self._require(COMMENT_SCOPES, COMMENT_SCOPE_HINT)
        await self._check_write("comments.insert", video_id)
        data = await self._api(
            "comments.insert", "POST", "comments", params={"part": "snippet"},
            body={"snippet": {"parentId": parent_id, "textOriginal": text}},
            video_id=video_id, kind="write", need_scopes=COMMENT_SCOPES, detail=f"ответ: {text[:80]}",
        )
        return normalize_comment(data or {})

    async def delete_comment(self, video_id: str, comment_id: str) -> None:
        """Удаляет комментарий. API разрешает только свои комментарии и комментарии под своими видео."""
        self._require(COMMENT_SCOPES, COMMENT_SCOPE_HINT)
        await self._check_write("comments.delete", video_id)
        await self._api(
            "comments.delete", "DELETE", "comments", params={"id": comment_id},
            video_id=video_id, kind="write", need_scopes=COMMENT_SCOPES, detail=f"удалён {comment_id}",
        )

    async def update_video(self, video_id: str, title: Any, description: Any) -> dict[str, Any]:
        """Правка названия/описания СВОЕГО видео. Остальные поля сохраняются (API стирает пропущенные)."""
        if not isinstance(title, str) or not (1 <= len(title.strip()) <= 100) or "<" in title or ">" in title:
            raise ApiError(400, "bad_request", "Название: 1–100 символов, без символов < и >")
        if not isinstance(description, str) or len(description.encode("utf-8")) > 5000 or "<" in description or ">" in description:
            raise ApiError(400, "bad_request", "Описание: до 5000 байт, без символов < и >")
        if not (self.oauth.connected and self.oauth.has_scope(*UPDATE_SCOPES)):
            raise ApiError(403, "insufficient_scope", "Для правки видео нужен вход через Google")
        profile = self.oauth.profile()
        current = await self.video(video_id)
        if not profile or profile["id"] != current["channel_id"]:
            raise ApiError(403, "not_owner", "Редактировать можно только видео своего канала")
        await self._check_write("videos.update", video_id)
        snippet: dict[str, Any] = {
            "title": title.strip(), "description": description,
            "categoryId": current["category_id"],
        }
        if current.get("tags"):
            snippet["tags"] = current["tags"]
        if current.get("default_language"):
            snippet["defaultLanguage"] = current["default_language"]
        data = await self._api(
            "videos.update", "PUT", "videos", params={"part": "snippet"},
            body={"id": video_id, "snippet": snippet}, video_id=video_id, kind="write",
            need_scopes=UPDATE_SCOPES, detail="обновлены название и описание",
        )
        return normalize_video(data or {})

    # --- отслеживание статистики (только чтение) -----------------------------------
    async def poll_once(self) -> dict[str, Any]:
        """Снимает реальные счётчики всех отслеживаемых видео. Один запрос на 50 видео (1 ед. квоты)."""
        async with self._poll_lock:
            watch = await asyncio.to_thread(self.db.watch_list)
            if not watch:
                return {"polled": 0, "missing": []}
            ids = [w["video_id"] for w in watch]
            found = await self.videos(ids, parts="snippet,statistics")
            seen = set()
            for v in found:
                seen.add(v["id"])
                snap = await asyncio.to_thread(self.db.add_snapshot, v["id"], v["views"], v["likes"], v["comments"])
                await asyncio.to_thread(self.db.watch_add, v["id"], v["title"], v["channel_title"])
                await self.bus.publish({"type": "yt_snapshot", **snap})
            missing = [i for i in ids if i not in seen]
            if missing:
                await self._record("read", "poll", None, False, 200, 0, f"Недоступны (удалены/скрыты): {', '.join(missing)}")
            return {"polled": len(seen), "missing": missing}

    async def add_watch(self, video_id: str) -> dict[str, Any]:
        v = await self.video(video_id)
        await asyncio.to_thread(self.db.watch_add, video_id, v["title"], v["channel_title"])
        snap = await asyncio.to_thread(self.db.add_snapshot, video_id, v["views"], v["likes"], v["comments"])
        await self.bus.publish({"type": "yt_snapshot", **snap})
        return v

    def start_poller(self) -> None:
        if self._poller is None:
            self._poller = asyncio.create_task(self._poll_loop(), name="yt-poller")

    async def stop_poller(self) -> None:
        task, self._poller = self._poller, None
        await self.oauth.cancel_device()
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.yt_poll_interval_min * 60)
            if not self.can_read:
                continue
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except ApiError as exc:
                log.warning("Плановый опрос статистики не удался: %s", exc)
            except Exception:  # noqa: BLE001
                log.exception("Неожиданная ошибка планового опроса")
