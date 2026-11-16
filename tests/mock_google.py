"""ТЕСТОВЫЙ ДВОЙНИК серверов Google (OAuth + YouTube Data API v3).

Используется ТОЛЬКО автотестами, чтобы проверить протокол и логику приложения без доступа к
реальному Google-проекту. Приложение само ничего не подделывает: в рабочем режиме оно ходит
по настоящим адресам Google. Двойник повторяет документированное поведение API:
коды ошибок, требуемые права (scopes), стирание пропущенных полей при videos.update и т.д.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SCOPE_YT = "https://www.googleapis.com/auth/youtube"
SCOPE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"
CLIENT_ID, CLIENT_SECRET, API_KEY = "cid", "secret", "KEY123"
ME = "UC_me"


def _video(vid: str, channel: str, title: str, views: str, likes: str | None, comments: str | None) -> dict[str, Any]:
    stats = {"viewCount": views, "favoriteCount": "0"}
    if likes is not None:
        stats["likeCount"] = likes
    if comments is not None:
        stats["commentCount"] = comments
    return {
        "id": vid,
        "snippet": {
            "title": title, "description": "описание", "channelId": channel,
            "channelTitle": "Chan " + channel, "publishedAt": "2024-01-02T03:04:05Z",
            "categoryId": "10", "tags": ["music", "test"], "defaultLanguage": "ru",
            "thumbnails": {"default": {"url": "https://i.ytimg.com/vi/x/default.jpg"},
                           "high": {"url": "https://i.ytimg.com/vi/x/hq.jpg"}},
            "liveBroadcastContent": "none",
        },
        "statistics": stats,
        "contentDetails": {"duration": "PT3M33S"},
        "status": {"privacyStatus": "public"},
    }


class MockGoogle:
    def __init__(self) -> None:
        self.reset()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)

    def reset(self) -> None:
        self.videos = {
            "dQw4w9WgXcQ": _video("dQw4w9WgXcQ", "UC_other", "Чужое видео", "1000", "50", "7"),
            "abcdefghijk": _video("abcdefghijk", ME, "Моё видео", "200", None, "2"),  # лайки скрыты
            "nocomment_1": _video("nocomment_1", "UC_other", "Без комментариев", "5", "1", None),
        }
        self.comments: dict[str, list[dict[str, Any]]] = {"dQw4w9WgXcQ": []}
        self.ratings: set[str] = set()
        self.tokens: dict[str, str] = {}      # access_token -> scope
        self.refresh: dict[str, str] = {}     # refresh_token -> scope
        self.calls: list[tuple[str, str]] = []
        self.revoked: list[str] = []
        self.device_pending_polls = 2
        self.device_mode = "approve"          # approve | deny
        self.device_polls = 0
        self.expires_in = 3600
        self.refresh_revoked = False
        self.fail_next: tuple[int, str, str] | None = None
        self.token_counter = 0
        self.comment_counter = 0
        self.last_update_body: dict[str, Any] | None = None

    # --- жизненный цикл ---------------------------------------------------------
    def start(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p in self.calls if m == method and p == path)

    def _issue(self, scope: str, refresh: str | None = None) -> dict[str, Any]:
        self.token_counter += 1
        access = f"AT_{self.token_counter}"
        self.tokens[access] = scope
        out: dict[str, Any] = {"access_token": access, "expires_in": self.expires_in, "scope": scope, "token_type": "Bearer"}
        if refresh:
            self.refresh[refresh] = scope
            out["refresh_token"] = refresh
        return out

    # --- обработчик HTTP ----------------------------------------------------------
    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # тишина в тестах
                pass

            def _send(self, status: int, body: Any = None) -> None:
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _gerr(self, status: int, reason: str, message: str) -> None:
                self._send(status, {"error": {"code": status, "message": message,
                                              "errors": [{"reason": reason, "message": message}]}})

            def _oerr(self, status: int, error: str, desc: str = "") -> None:
                self._send(status, {"error": error, "error_description": desc or error})

            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _principal(self, q: dict[str, str]) -> tuple[str | None, str]:
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    tok = auth[7:]
                    return (tok, mock.tokens[tok]) if tok in mock.tokens else (None, "")
                if q.get("key") == API_KEY:
                    return ("key", "")
                return (None, "")

            def _handle(self, method: str) -> None:
                u = urllib.parse.urlparse(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                path = u.path
                raw = self._body()
                mock.calls.append((method, path))
                if path.startswith("/oauth/"):
                    return self._oauth(method, path, raw)
                if not path.startswith("/youtube/v3/"):
                    return self._gerr(404, "notFound", "not found")
                if mock.fail_next:
                    status, reason, msg = mock.fail_next
                    mock.fail_next = None
                    return self._gerr(status, reason, msg)
                who, scope = self._principal(q)
                if who is None:
                    return self._gerr(401, "authError", "Login Required")
                self._api(method, path[len("/youtube/v3/"):], q, raw, who, scope)

            # --- OAuth ---
            def _oauth(self, method: str, path: str, raw: bytes) -> None:
                f = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
                if path == "/oauth/device/code":
                    if f.get("client_id") != CLIENT_ID:
                        return self._oerr(401, "invalid_client")
                    return self._send(200, {"device_code": "DEV", "user_code": "ABCD-EFGH",
                                            "verification_url": "https://www.google.com/device",
                                            "expires_in": 1800, "interval": 1})
                if path == "/oauth/revoke":
                    mock.revoked.append(f.get("token", ""))
                    return self._send(200, {})
                if path != "/oauth/token":
                    return self._oerr(404, "not_found")
                if f.get("client_id") != CLIENT_ID or f.get("client_secret") != CLIENT_SECRET:
                    return self._oerr(401, "invalid_client")
                grant = f.get("grant_type", "")
                if grant.endswith("device_code"):
                    mock.device_polls += 1
                    if mock.device_polls <= mock.device_pending_polls:
                        return self._oerr(428, "authorization_pending")
                    if mock.device_mode == "deny":
                        return self._oerr(403, "access_denied")
                    return self._send(200, mock._issue(SCOPE_YT, "REFRESH_D"))
                if grant == "authorization_code":
                    if f.get("code") != "CODE123" or not f.get("redirect_uri"):
                        return self._oerr(400, "invalid_grant", "bad code")
                    return self._send(200, mock._issue(SCOPE_SSL, "REFRESH_W"))
                if grant == "refresh_token":
                    rt = f.get("refresh_token", "")
                    if mock.refresh_revoked or rt not in mock.refresh:
                        return self._oerr(400, "invalid_grant", "Token has been expired or revoked.")
                    return self._send(200, mock._issue(mock.refresh[rt]))  # без нового refresh_token
                return self._oerr(400, "unsupported_grant_type")

            # --- YouTube Data API ---
            def _api(self, method: str, res: str, q: dict[str, str], raw: bytes, who: str, scope: str) -> None:
                scopes = set(scope.split())
                body = json.loads(raw) if raw else None
                if res == "channels" and method == "GET":
                    if who == "key":
                        return self._gerr(401, "authError", "mine requires OAuth")
                    return self._send(200, {"items": [{"id": ME, "snippet": {"title": "Мой канал"}}]})
                if res == "videos" and method == "GET":
                    ids = [i for i in q.get("id", "").split(",") if i]
                    return self._send(200, {"items": [deepcopy(mock.videos[i]) for i in ids if i in mock.videos]})
                if res == "videos/getRating":
                    if who == "key":
                        return self._gerr(401, "authError", "OAuth required")
                    vid = q.get("id", "")
                    return self._send(200, {"items": [{"videoId": vid, "rating": "like" if vid in mock.ratings else "none"}]})
                if res == "videos/rate" and method == "POST":
                    if not scopes & {SCOPE_YT, SCOPE_SSL}:
                        return self._gerr(403, "insufficientPermissions", "Insufficient Permission")
                    if q.get("rating") not in {"like", "dislike", "none"} or q.get("id") not in mock.videos:
                        return self._gerr(404, "videoNotFound", "Video not found")
                    (mock.ratings.add if q["rating"] == "like" else mock.ratings.discard)(q["id"])
                    return self._send(204)
                if res == "videos" and method == "PUT":
                    if not scopes & {SCOPE_YT, SCOPE_SSL}:
                        return self._gerr(403, "insufficientPermissions", "Insufficient Permission")
                    sn = (body or {}).get("snippet", {})
                    vid = (body or {}).get("id")
                    if "title" not in sn or "categoryId" not in sn or vid not in mock.videos:
                        return self._gerr(400, "invalidRequest", "title and categoryId required")
                    if mock.videos[vid]["snippet"]["channelId"] != ME:
                        return self._gerr(403, "forbidden", "not your video")
                    mock.last_update_body = body
                    old = mock.videos[vid]["snippet"]
                    new = {k: old[k] for k in ("channelId", "channelTitle", "publishedAt", "thumbnails", "liveBroadcastContent")}
                    new.update(sn)  # пропущенные поля (tags, defaultLanguage) стираются — как у настоящего API
                    mock.videos[vid]["snippet"] = new
                    return self._send(200, deepcopy(mock.videos[vid]))
                if res == "commentThreads" and method == "GET":
                    vid = q.get("videoId", "")
                    if vid not in mock.videos:
                        return self._gerr(404, "videoNotFound", "not found")
                    if "commentCount" not in mock.videos[vid]["statistics"]:
                        return self._gerr(403, "commentsDisabled", "The video identified by the videoId parameter has disabled comments.")
                    items = [{"snippet": {"topLevelComment": c, "totalReplyCount": 0}} for c in mock.comments.get(vid, [])]
                    return self._send(200, {"items": items})
                if res == "commentThreads" and method == "POST":
                    if SCOPE_SSL not in scopes:
                        return self._gerr(403, "insufficientPermissions", "Insufficient Permission")
                    sn = (body or {}).get("snippet", {})
                    text = sn.get("topLevelComment", {}).get("snippet", {}).get("textOriginal")
                    if not (sn.get("channelId") and sn.get("videoId") and text):
                        return self._gerr(400, "invalidRequest", "channelId, videoId, textOriginal required")
                    mock.comment_counter += 1
                    c = {"id": f"cmt{mock.comment_counter}", "snippet": {"authorDisplayName": "Я", "authorChannelId": {"value": ME}, "textDisplay": text,
                         "likeCount": 0, "publishedAt": "2026-11-16T00:00:00Z", "updatedAt": "2026-11-16T00:00:00Z"}}
                    mock.comments.setdefault(sn["videoId"], []).append(c)
                    mock.last_update_body = body
                    return self._send(200, {"id": "thr" + c["id"], "snippet": {"topLevelComment": c}})
                if res == "comments" and method == "POST":
                    if SCOPE_SSL not in scopes:
                        return self._gerr(403, "insufficientPermissions", "Insufficient Permission")
                    sn = (body or {}).get("snippet", {})
                    if not (sn.get("parentId") and sn.get("textOriginal")):
                        return self._gerr(400, "invalidRequest", "parentId, textOriginal required")
                    return self._send(200, {"id": "reply1", "snippet": {"authorDisplayName": "Я", "textDisplay": sn["textOriginal"]}})
                if res == "comments" and method == "DELETE":
                    if SCOPE_SSL not in scopes:
                        return self._gerr(403, "insufficientPermissions", "Insufficient Permission")
                    for lst in mock.comments.values():
                        for c in list(lst):
                            if c["id"] == q.get("id"):
                                lst.remove(c)
                                return self._send(204)
                    return self._gerr(404, "commentNotFound", "not found")
                return self._gerr(404, "notFound", f"{method} {res}")

            def do_GET(self) -> None: self._handle("GET")
            def do_POST(self) -> None: self._handle("POST")
            def do_PUT(self) -> None: self._handle("PUT")
            def do_DELETE(self) -> None: self._handle("DELETE")

        return H
