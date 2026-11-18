"""История тестирования в SQLite (stdlib). Вызовы из async-кода идут через asyncio.to_thread."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    video_id     TEXT NOT NULL,
    scenarios    TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    summary      TEXT,
    error        TEXT
);
CREATE TABLE IF NOT EXISTS scenario_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 1,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    message      TEXT,
    finished_at  REAL
);
CREATE TABLE IF NOT EXISTS logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts        REAL NOT NULL,
    level     TEXT NOT NULL,
    scenario  TEXT,
    message   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_run ON logs(run_id, id);
CREATE INDEX IF NOT EXISTS idx_results_run ON scenario_results(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);

CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS yt_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    kind         TEXT NOT NULL,          -- read | write | auth
    name         TEXT NOT NULL,          -- напр. videos.list, videos.rate
    video_id     TEXT,
    ok           INTEGER NOT NULL,
    http_status  INTEGER,
    units        INTEGER NOT NULL DEFAULT 0,
    detail       TEXT
);
CREATE INDEX IF NOT EXISTS idx_yt_actions_ts ON yt_actions(ts DESC);
CREATE TABLE IF NOT EXISTS yt_watch (
    video_id       TEXT PRIMARY KEY,
    title          TEXT,
    channel_title  TEXT,
    added_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS yt_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id  TEXT NOT NULL,
    ts        REAL NOT NULL,
    views     INTEGER,
    likes     INTEGER,
    comments  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_yt_snap ON yt_snapshots(video_id, ts);
"""

_RUN_COLUMNS = {"status", "started_at", "finished_at", "summary", "error"}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
            if "options" not in cols:  # миграция для баз, созданных до появления параметров прогона
                self._conn.execute("ALTER TABLE runs ADD COLUMN options TEXT NOT NULL DEFAULT '{}'")
            self._conn.commit()
        try:  # в базе лежат OAuth-токены: доступ только владельцу
            os.chmod(path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- runs -----------------------------------------------------------
    def create_run(self, run_id: str, url: str, video_id: str, scenarios: list[str],
                   options: dict[str, Any] | None = None) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (id, url, video_id, scenarios, status, created_at, options) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, url, video_id, json.dumps(scenarios), "queued", now, json.dumps(options or {})),
            )
            self._conn.commit()
        return self.get_run(run_id) or {}

    def update_run(self, run_id: str, **fields: Any) -> None:
        bad = set(fields) - _RUN_COLUMNS
        if bad:
            raise ValueError(f"unknown run columns: {bad}")
        if not fields:
            return
        if "summary" in fields and not isinstance(fields["summary"], (str, type(None))):
            fields["summary"] = json.dumps(fields["summary"])
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE runs SET {sets} WHERE id=?", (*fields.values(), run_id))
            self._conn.commit()

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["scenarios"] = json.loads(d["scenarios"] or "[]")
        d["options"] = json.loads(d.get("options") or "{}")
        d["summary"] = json.loads(d["summary"]) if d.get("summary") else None
        return d

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return None
            run = self._run_row(row)
            results = self._conn.execute(
                "SELECT name, status, attempts, duration_ms, message, finished_at "
                "FROM scenario_results WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        run["results"] = [dict(r) for r in results]
        return run

    def list_runs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [self._run_row(r) for r in rows]

    def delete_run(self, run_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def clear_finished(self) -> list[str]:
        """Удаляет завершённые прогоны, возвращает их id (для очистки скриншотов)."""
        with self._lock:
            ids = [
                r["id"]
                for r in self._conn.execute(
                    "SELECT id FROM runs WHERE status NOT IN ('queued','running')"
                ).fetchall()
            ]
            self._conn.execute("DELETE FROM runs WHERE status NOT IN ('queued','running')")
            self._conn.commit()
        return ids

    def fail_interrupted(self) -> int:
        """После перезапуска сервера помечаем «зависшие» прогоны как прерванные."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE runs SET status='error', finished_at=?, error=? WHERE status IN ('queued','running')",
                (time.time(), "Сервер был перезапущен во время выполнения"),
            )
            self._conn.commit()
            return cur.rowcount

    # --- scenario results -----------------------------------------------
    def add_scenario_result(
        self,
        run_id: str,
        name: str,
        status: str,
        attempts: int,
        duration_ms: int,
        message: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO scenario_results (run_id, name, status, attempts, duration_ms, message, finished_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, name, status, attempts, duration_ms, message, time.time()),
            )
            self._conn.commit()

    # --- logs -----------------------------------------------------------
    def add_log(self, run_id: str, level: str, scenario: str | None, message: str) -> dict[str, Any]:
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO logs (run_id, ts, level, scenario, message) VALUES (?,?,?,?,?)",
                (run_id, ts, level, scenario, message),
            )
            self._conn.commit()
            log_id = cur.lastrowid
        return {"id": log_id, "run_id": run_id, "ts": ts, "level": level, "scenario": scenario, "message": message}

    def get_logs(self, run_id: str, after: int = 0, limit: int = 2000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, run_id, ts, level, scenario, message FROM logs "
                "WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                (run_id, after, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- key/value (OAuth-токены и профиль) -------------------------------
    def kv_get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    def kv_delete(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM kv WHERE key=?", (key,))
            self._conn.commit()

    # --- журнал операций YouTube API -------------------------------------
    def add_yt_action(
        self,
        kind: str,
        name: str,
        video_id: str | None,
        ok: bool,
        http_status: int | None,
        units: int,
        detail: str | None,
    ) -> dict[str, Any]:
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO yt_actions (ts, kind, name, video_id, ok, http_status, units, detail) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ts, kind, name, video_id, 1 if ok else 0, http_status, units, (detail or "")[:500]),
            )
            self._conn.commit()
            action_id = cur.lastrowid
        return {
            "id": action_id, "ts": ts, "kind": kind, "name": name, "video_id": video_id,
            "ok": bool(ok), "http_status": http_status, "units": units, "detail": detail or "",
        }

    def list_yt_actions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM yt_actions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{**dict(r), "ok": bool(r["ok"])} for r in rows]

    def yt_units_since(self, ts: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(units), 0) AS u FROM yt_actions WHERE ts>=?", (ts,)
            ).fetchone()
        return int(row["u"])

    def yt_writes_since(self, ts: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM yt_actions WHERE kind='write' AND ok=1 AND ts>=?", (ts,)
            ).fetchone()
        return int(row["c"])

    def yt_last_write(self, name: str, video_id: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) AS t FROM yt_actions WHERE kind='write' AND ok=1 AND name=? AND video_id=?",
                (name, video_id),
            ).fetchone()
        return row["t"] if row and row["t"] is not None else None

    # --- список отслеживания и снимки статистики -------------------------
    def watch_add(self, video_id: str, title: str | None, channel_title: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO yt_watch (video_id, title, channel_title, added_at) VALUES (?,?,?,?) "
                "ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, channel_title=excluded.channel_title",
                (video_id, title, channel_title, time.time()),
            )
            self._conn.commit()

    def watch_remove(self, video_id: str) -> bool:
        with self._lock:
            self._conn.execute("DELETE FROM yt_snapshots WHERE video_id=?", (video_id,))
            cur = self._conn.execute("DELETE FROM yt_watch WHERE video_id=?", (video_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def watch_list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM yt_watch ORDER BY added_at DESC").fetchall()
            out = []
            for r in rows:
                snaps = self._conn.execute(
                    "SELECT ts, views, likes, comments FROM yt_snapshots WHERE video_id=? "
                    "ORDER BY ts DESC LIMIT 2",
                    (r["video_id"],),
                ).fetchall()
                item = dict(r)
                item["latest"] = dict(snaps[0]) if snaps else None
                item["previous"] = dict(snaps[1]) if len(snaps) > 1 else None
                out.append(item)
        return out

    def add_snapshot(self, video_id: str, views: int | None, likes: int | None, comments: int | None) -> dict[str, Any]:
        ts = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO yt_snapshots (video_id, ts, views, likes, comments) VALUES (?,?,?,?,?)",
                (video_id, ts, views, likes, comments),
            )
            self._conn.commit()
        return {"video_id": video_id, "ts": ts, "views": views, "likes": likes, "comments": comments}

    def list_snapshots(self, video_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, views, likes, comments FROM yt_snapshots WHERE video_id=? "
                "ORDER BY ts DESC LIMIT ?",
                (video_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
