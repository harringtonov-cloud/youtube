"""Состояние песочницы: просмотры, лайки, комментарии.

Правила намеренно похожи на правила настоящих платформ, чтобы автотесты
проверяли осмысленное поведение:
  * просмотр засчитывается один раз на сессию и только после порога
    воспроизведения, измеряемого НА СЕРВЕРЕ;
  * лайк — идемпотентная установка состояния, один лайк на сессию;
  * комментарий: валидация, лимит длины и ограничение частоты.
Все методы синхронные и без await — в одном event loop они атомарны.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

MAX_VIDEOS = 200
MAX_COMMENTS_PER_VIDEO = 500
MAX_COMMENT_LEN = 500
COMMENT_LIMIT = 5  # комментариев
COMMENT_WINDOW_S = 60.0  # за это окно на одну сессию


@dataclass
class VideoState:
    views: int = 0
    viewed: set[str] = field(default_factory=set)
    play_started: dict[str, float] = field(default_factory=dict)
    likes: set[str] = field(default_factory=set)
    comments: list[dict[str, Any]] = field(default_factory=list)
    comment_times: dict[str, list[float]] = field(default_factory=dict)


class SandboxStore:
    def __init__(self, view_threshold_ms: int) -> None:
        self.view_threshold_s = view_threshold_ms / 1000.0
        self._videos: OrderedDict[str, VideoState] = OrderedDict()
        # имитация нестабильного бэкенда (для проверки автовосстановления)
        self.flaky_fail_next = 0
        self.flaky_hits = 0

    # --- служебное ----------------------------------------------------------
    def _video(self, vid: str) -> VideoState:
        v = self._videos.get(vid)
        if v is None:
            v = VideoState()
            self._videos[vid] = v
            while len(self._videos) > MAX_VIDEOS:
                self._videos.popitem(last=False)
        else:
            self._videos.move_to_end(vid)
        return v

    def reset(self, vid: str) -> None:
        self._videos[vid] = VideoState()
        self._videos.move_to_end(vid)

    def arm_flaky(self, count: int) -> None:
        self.flaky_fail_next = max(0, count)
        self.flaky_hits = 0

    def consume_flaky(self) -> bool:
        """True — текущий запрос должен упасть с 503."""
        if self.flaky_fail_next > 0:
            self.flaky_fail_next -= 1
            self.flaky_hits += 1
            return True
        return False

    # --- чтение -------------------------------------------------------------
    def stats(self, vid: str, sid: str | None = None) -> dict[str, Any]:
        v = self._video(vid)
        data: dict[str, Any] = {
            "vid": vid,
            "views": v.views,
            "likes": len(v.likes),
            "comments_count": len(v.comments),
            "comments": v.comments[-50:],
            "view_threshold_ms": int(self.view_threshold_s * 1000),
        }
        if sid is not None:
            data["liked"] = sid in v.likes
            data["viewed"] = sid in v.viewed
        return data

    def counters(self, vid: str) -> dict[str, Any]:
        v = self._video(vid)
        return {"vid": vid, "views": v.views, "likes": len(v.likes), "comments": len(v.comments)}

    # --- просмотры ----------------------------------------------------------
    def play(self, vid: str, sid: str) -> dict[str, Any]:
        v = self._video(vid)
        v.play_started[sid] = time.monotonic()
        return {"ok": True}

    def view(self, vid: str, sid: str) -> dict[str, Any]:
        v = self._video(vid)
        if sid in v.viewed:
            return {"counted": False, "reason": "already_counted", "views": v.views}
        started = v.play_started.get(sid)
        if started is None:
            return {"counted": False, "reason": "not_played", "views": v.views}
        if time.monotonic() - started < self.view_threshold_s:
            return {"counted": False, "reason": "too_early", "views": v.views}
        v.views += 1
        v.viewed.add(sid)
        return {"counted": True, "reason": "ok", "views": v.views}

    # --- лайки --------------------------------------------------------------
    def set_like(self, vid: str, sid: str, liked: bool) -> dict[str, Any]:
        v = self._video(vid)
        if liked:
            v.likes.add(sid)
        else:
            v.likes.discard(sid)
        return {"liked": sid in v.likes, "likes": len(v.likes)}

    # --- комментарии --------------------------------------------------------
    def add_comment(self, vid: str, sid: str, text: Any) -> tuple[int, dict[str, Any]]:
        """Возвращает (http_status, payload)."""
        v = self._video(vid)
        if not isinstance(text, str):
            return 400, {"error": "invalid", "message": "Некорректный комментарий"}
        text = text.strip()
        if not text:
            return 400, {"error": "empty", "message": "Комментарий пустой"}
        if len(text) > MAX_COMMENT_LEN:
            return 400, {
                "error": "too_long",
                "message": f"Комментарий длиннее {MAX_COMMENT_LEN} символов",
            }
        now = time.monotonic()
        recent = [t for t in v.comment_times.get(sid, []) if now - t < COMMENT_WINDOW_S]
        if len(recent) >= COMMENT_LIMIT:
            v.comment_times[sid] = recent
            return 429, {"error": "rate_limited", "message": "Слишком часто, подождите"}
        recent.append(now)
        v.comment_times[sid] = recent
        comment = {"id": len(v.comments) + 1, "author": f"guest-{sid[:4]}", "text": text}
        v.comments.append(comment)
        if len(v.comments) > MAX_COMMENTS_PER_VIDEO:
            del v.comments[: len(v.comments) - MAX_COMMENTS_PER_VIDEO]
        return 201, {"comment": comment, "comments_count": len(v.comments)}
