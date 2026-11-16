"""Google OAuth 2.0: вход по коду (device flow) и классический веб-вход (authorization code).

Почему два способа:
  * device flow работает на сервере без домена и HTTPS: код вводится на google.com/device
    с телефона. Но Google разрешает в нём только права `youtube` и `youtube.readonly`.
  * Комментарии (commentThreads.insert, comments.insert/delete) требуют
    `youtube.force-ssl`, а он доступен только в веб-входе (нужен HTTPS-адрес возврата).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
import urllib.parse
from typing import Any

from ..config import Settings
from ..db import Database
from ..events import EventBus
from .http import ApiError, call

log = logging.getLogger("yt-tester.oauth")

SCOPE_YOUTUBE = "https://www.googleapis.com/auth/youtube"
SCOPE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_FORCE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"

DEVICE_SCOPES = [SCOPE_YOUTUBE]  # force-ssl в device flow Google не разрешает
WEB_SCOPES = [SCOPE_FORCE_SSL]

TOKENS_KEY = "oauth_tokens"
PROFILE_KEY = "oauth_profile"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class OAuthManager:
    def __init__(self, settings: Settings, db: Database, bus: EventBus) -> None:
        self.s = settings
        self.db = db
        self.bus = bus
        self._lock = threading.Lock()
        self._device: dict[str, Any] | None = None
        self._device_task: asyncio.Task[None] | None = None
        self._web_states: dict[str, float] = {}

    # --- конфигурация ---------------------------------------------------------
    @property
    def client_configured(self) -> bool:
        return bool(self.s.google_client_id and self.s.google_client_secret)

    @property
    def web_configured(self) -> bool:
        return self.client_configured and bool(self.s.google_redirect_uri)

    # --- токены ---------------------------------------------------------------
    def tokens(self) -> dict[str, Any] | None:
        raw = self.db.kv_get(TOKENS_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def profile(self) -> dict[str, Any] | None:
        raw = self.db.kv_get(PROFILE_KEY)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    @property
    def connected(self) -> bool:
        t = self.tokens()
        return bool(t and (t.get("refresh_token") or t.get("access_token")))

    def granted_scopes(self) -> set[str]:
        t = self.tokens() or {}
        return set(str(t.get("scope", "")).split())

    def has_scope(self, *any_of: str) -> bool:
        return bool(self.granted_scopes() & set(any_of))

    def _store(self, data: dict[str, Any], flow: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        prev = previous or {}
        tokens = {
            "access_token": data["access_token"],
            # при refresh Google не всегда присылает refresh_token повторно — сохраняем прежний
            "refresh_token": data.get("refresh_token") or prev.get("refresh_token"),
            "expires_at": time.time() + int(data.get("expires_in", 3600)),
            "scope": data.get("scope") or prev.get("scope", ""),
            "token_type": data.get("token_type", "Bearer"),
            "flow": flow if flow else prev.get("flow", ""),
            "obtained_at": time.time(),
        }
        self.db.kv_set(TOKENS_KEY, json.dumps(tokens))
        return tokens

    def clear(self) -> None:
        self.db.kv_delete(TOKENS_KEY)
        self.db.kv_delete(PROFILE_KEY)

    def get_access_token(self) -> str:
        """Актуальный access token (при необходимости обновляется). Блокирующий вызов."""
        with self._lock:
            tokens = self.tokens()
            if not tokens:
                raise ApiError(401, "not_authenticated", "Вход через Google не выполнен")
            if tokens.get("expires_at", 0) - time.time() > 60:
                return str(tokens["access_token"])
            refresh = tokens.get("refresh_token")
            if not refresh:
                self.clear()
                raise ApiError(401, "invalid_grant", "Срок действия доступа истёк. Войдите через Google снова")
            try:
                _, data = call(
                    "POST",
                    f"{self.s.google_oauth_base}/token",
                    form={
                        "client_id": self.s.google_client_id,
                        "client_secret": self.s.google_client_secret,
                        "refresh_token": str(refresh),
                        "grant_type": "refresh_token",
                    },
                    retries=1,
                )
            except ApiError as exc:
                if exc.reason == "invalid_grant":
                    # доступ отозван (или токен «Testing»-приложения истёк за 7 дней)
                    self.clear()
                    raise ApiError(401, "invalid_grant", "Доступ отозван или истёк. Войдите через Google снова") from None
                raise
            return str(self._store(data, "", tokens)["access_token"])

    # --- профиль канала ---------------------------------------------------------
    def fetch_profile(self) -> dict[str, Any] | None:
        """channels.list?mine=true — кто вошёл. Нет канала → None (вход всё равно действителен)."""
        token = self.get_access_token()
        try:
            _, data = call(
                "GET",
                f"{self.s.youtube_api_base}/channels",
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {token}"},
            )
        except ApiError as exc:
            log.warning("Не удалось получить профиль канала: %s", exc)
            return None
        items = (data or {}).get("items") or []
        if not items:
            self.db.kv_delete(PROFILE_KEY)
            return None
        profile = {"id": items[0]["id"], "title": items[0].get("snippet", {}).get("title", "")}
        self.db.kv_set(PROFILE_KEY, json.dumps(profile))
        return profile

    async def _finish_login(self, data: dict[str, Any], flow: str) -> None:
        await asyncio.to_thread(self._store, data, flow, None)
        profile = await asyncio.to_thread(self.fetch_profile)
        action = await asyncio.to_thread(
            self.db.add_yt_action, "auth", f"oauth.{flow}", None, True, 200, 1 if profile else 0,
            f"Вход выполнен: {profile['title'] if profile else 'канал не найден'}; права: {data.get('scope', '')}",
        )
        await self.bus.publish({"type": "yt_action", "action": action})
        await self.bus.publish({"type": "yt_auth", "status": "connected"})

    # --- device flow --------------------------------------------------------------
    def device_state(self) -> dict[str, Any]:
        d = self._device
        if not d:
            return {"status": "idle"}
        return {k: d[k] for k in ("status", "user_code", "verification_url", "expires_at", "message") if k in d}

    async def start_device(self) -> dict[str, Any]:
        if not self.client_configured:
            raise ApiError(400, "not_configured", "Не заданы GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET")
        await self.cancel_device()
        _, data = await asyncio.to_thread(
            call, "POST", f"{self.s.google_oauth_base}/device/code",
            form={"client_id": self.s.google_client_id, "scope": " ".join(DEVICE_SCOPES)}, retries=1,
        )
        interval = max(int(data.get("interval", 5)), 1)
        expires_at = time.time() + int(data.get("expires_in", 1800))
        self._device = {
            "status": "pending",
            "user_code": data["user_code"],
            "verification_url": data.get("verification_url") or data.get("verification_uri", "https://www.google.com/device"),
            "expires_at": expires_at,
            "message": "",
        }
        self._device_task = asyncio.create_task(self._poll_device(str(data["device_code"]), interval, expires_at))
        return self.device_state()

    async def cancel_device(self) -> None:
        task, self._device_task = self._device_task, None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._device and self._device["status"] == "pending":
            self._device = None

    async def _poll_device(self, device_code: str, interval: int, expires_at: float) -> None:
        assert self._device is not None
        state = self._device
        net_errors = 0
        try:
            while time.time() < expires_at:
                await asyncio.sleep(interval)
                try:
                    _, data = await asyncio.to_thread(
                        call, "POST", f"{self.s.google_oauth_base}/token",
                        form={
                            "client_id": self.s.google_client_id,
                            "client_secret": self.s.google_client_secret,
                            "device_code": device_code,
                            "grant_type": _DEVICE_GRANT,
                        },
                        retries=0,
                    )
                except ApiError as exc:
                    if exc.reason == "authorization_pending":
                        net_errors = 0
                        continue
                    if exc.reason == "slow_down":
                        interval += 5
                        continue
                    if exc.reason == "network" and net_errors < 5:
                        net_errors += 1
                        continue
                    if exc.reason == "access_denied":
                        state.update(status="denied", message="Доступ отклонён в окне Google")
                    elif exc.reason in {"expired_token", "invalid_grant"}:
                        state.update(status="expired", message="Код истёк. Запросите новый")
                    else:
                        state.update(status="error", message=str(exc))
                    await self.bus.publish({"type": "yt_auth", "status": state["status"]})
                    return
                await self._finish_login(data, "device")
                state.update(status="approved", message="")
                return
            state.update(status="expired", message="Код истёк. Запросите новый")
            await self.bus.publish({"type": "yt_auth", "status": "expired"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — фоновая задача не должна падать молча
            log.exception("Сбой опроса device flow")
            state.update(status="error", message=str(exc))
            await self.bus.publish({"type": "yt_auth", "status": "error"})

    # --- веб-вход (authorization code) ---------------------------------------------
    def web_start(self) -> str:
        if not self.web_configured:
            raise ApiError(
                400, "not_configured",
                "Для веб-входа задайте GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET и GOOGLE_REDIRECT_URI (HTTPS)",
            )
        now = time.time()
        self._web_states = {k: v for k, v in self._web_states.items() if v > now}
        state = secrets.token_urlsafe(24)
        self._web_states[state] = now + 600
        query = urllib.parse.urlencode({
            "client_id": self.s.google_client_id,
            "redirect_uri": self.s.google_redirect_uri,
            "response_type": "code",
            "scope": " ".join(WEB_SCOPES),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "state": state,
        })
        return f"{self.s.google_auth_url}?{query}"

    async def web_finish(self, code: str, state: str) -> None:
        expires = self._web_states.pop(state, 0)
        if not expires or expires < time.time():
            raise ApiError(400, "bad_state", "Неверный или просроченный параметр state. Начните вход заново")
        _, data = await asyncio.to_thread(
            call, "POST", f"{self.s.google_oauth_base}/token",
            form={
                "code": code,
                "client_id": self.s.google_client_id,
                "client_secret": self.s.google_client_secret,
                "redirect_uri": self.s.google_redirect_uri,
                "grant_type": "authorization_code",
            },
            retries=0,
        )
        await self._finish_login(data, "web")

    # --- выход ---------------------------------------------------------------------
    async def logout(self) -> None:
        await self.cancel_device()
        self._device = None
        tokens = await asyncio.to_thread(self.tokens)
        if tokens:
            token = tokens.get("refresh_token") or tokens.get("access_token")
            try:  # отзываем доступ у Google; сбой не мешает локальному выходу
                await asyncio.to_thread(
                    call, "POST", f"{self.s.google_oauth_base}/revoke", form={"token": str(token)}, retries=0,
                )
            except ApiError as exc:
                log.warning("Не удалось отозвать токен у Google: %s", exc)
        await asyncio.to_thread(self.clear)
        action = await asyncio.to_thread(
            self.db.add_yt_action, "auth", "oauth.logout", None, True, 200, 0, "Выход, доступ отозван"
        )
        await self.bus.publish({"type": "yt_action", "action": action})
        await self.bus.publish({"type": "yt_auth", "status": "disconnected"})
