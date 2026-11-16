"""Разбор YouTube-ссылок.

ВАЖНО: ссылка используется ТОЛЬКО для извлечения 11-символьного ID видео.
Браузер никогда не открывает youtube.com: он загружает локальную песочницу,
в которую подставляется этот ID как метка тестового видео.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_YT_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_PATH_PREFIXES = ("shorts", "embed", "live", "v")


class InvalidYouTubeUrl(ValueError):
    """Ссылка не похожа на ссылку YouTube-видео."""


def is_valid_video_id(value: str) -> bool:
    return bool(_VIDEO_ID_RE.match(value or ""))


def extract_video_id(url: str) -> str:
    """Возвращает ID видео из ссылки YouTube или бросает InvalidYouTubeUrl."""
    if not isinstance(url, str) or not url.strip():
        raise InvalidYouTubeUrl("Введите ссылку на видео YouTube")
    raw = url.strip()
    if len(raw) > 300:
        raise InvalidYouTubeUrl("Ссылка слишком длинная")
    if "://" not in raw:
        raw = "https://" + raw

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise InvalidYouTubeUrl("Допустимы только http/https ссылки")
    host = (parsed.hostname or "").lower()

    candidate = ""
    if host in _SHORT_HOSTS:
        candidate = parsed.path.strip("/").split("/")[0]
    elif host in _YT_HOSTS:
        parts = [p for p in parsed.path.split("/") if p]
        if parts and parts[0] == "watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        elif len(parts) >= 2 and parts[0] in _PATH_PREFIXES:
            candidate = parts[1]
    else:
        raise InvalidYouTubeUrl("Это не ссылка YouTube (youtube.com / youtu.be)")

    if not is_valid_video_id(candidate):
        raise InvalidYouTubeUrl("Не удалось найти корректный ID видео (11 символов)")
    return candidate
