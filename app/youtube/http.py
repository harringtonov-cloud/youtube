"""Минимальный HTTP-клиент для Google API на стандартной библиотеке (без внешних зависимостей).

Синхронный: из async-кода вызывается через asyncio.to_thread.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("yt-tester.http")

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_NO_RETRY_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}


class ApiError(Exception):
    """Ошибка Google API / OAuth / сети с машинным кодом причины."""

    def __init__(self, status: int, reason: str, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message
        self.payload = payload

    def __str__(self) -> str:
        return f"[{self.status} {self.reason}] {self.message}"


def _parse_error(status: int, raw: bytes) -> ApiError:
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        return ApiError(status, "http_error", text[:300] or f"HTTP {status}")
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):  # формат YouTube Data API
        reasons = [e.get("reason") for e in err.get("errors", []) if isinstance(e, dict)]
        reason = reasons[0] if reasons else str(err.get("status") or "http_error")
        return ApiError(int(err.get("code") or status), reason, str(err.get("message") or text[:300]), data)
    if isinstance(err, str):  # формат OAuth: {"error": "...", "error_description": "..."}
        return ApiError(status, err, str(data.get("error_description") or err), data)
    return ApiError(status, "http_error", text[:300] or f"HTTP {status}", data)


def call(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    form: dict[str, str] | None = None,
    timeout: float = 20.0,
    retries: int | None = None,
) -> tuple[int, Any]:
    """Выполняет запрос и возвращает (status, json | None). Ошибки → ApiError.

    Повторы (с нарастающей паузой) делаются только для идемпотентных GET, чтобы
    случайно не продублировать запись (например, комментарий).
    """
    method = method.upper()
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        url = f"{url}?{urllib.parse.urlencode(clean)}"
    data: bytes | None = None
    hdrs = {"Accept": "application/json", "User-Agent": "yt-engagement-tester/2.0"}
    hdrs.update(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"

    attempts = (2 if method == "GET" else 0) if retries is None else retries
    last: ApiError | None = None
    for attempt in range(attempts + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.status
            return status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as exc:
            last = _parse_error(exc.code, exc.read())
            retryable = exc.code in _RETRY_STATUSES and last.reason not in _NO_RETRY_REASONS
            if not retryable or attempt >= attempts:
                raise last from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            last = ApiError(0, "network", f"Сетевая ошибка: {getattr(exc, 'reason', exc)}")
            if attempt >= attempts:
                raise last from None
        except ValueError as exc:
            raise ApiError(502, "bad_response", f"Некорректный ответ сервера: {exc}") from None
        delay = 0.5 * (2**attempt)
        log.warning("Повтор запроса %s %s через %.1f с (%s)", method, url.split("?")[0], delay, last)
        time.sleep(delay)
    raise last or ApiError(0, "network", "Неизвестная сетевая ошибка")
