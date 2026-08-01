from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TaskRecord:
    id: str
    api: str
    model: str
    prompt: str
    seconds: int
    size: str
    ratio: str
    resolution: str
    status: str
    progress: int
    created_at: int
    updated_at: int
    account_id: str | None = None
    completed_at: int | None = None
    video_id: str | None = None
    video_url: str | None = None
    poster_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] | None = None


class TaskStore:
    def __init__(self, path: str) -> None:
        self.path = str(Path(path))
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    api TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    seconds INTEGER NOT NULL,
                    size TEXT NOT NULL,
                    ratio TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    account_id TEXT,
                    completed_at INTEGER,
                    video_id TEXT,
                    video_url TEXT,
                    poster_url TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    raw TEXT
                )
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "account_id" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN account_id TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_error TEXT
                )
                """
            )

    @staticmethod
    def _decode(row: sqlite3.Row) -> TaskRecord:
        values = dict(row)
        values["raw"] = json.loads(values["raw"]) if values.get("raw") else None
        return TaskRecord(**values)

    def create(
        self,
        *,
        task_id: str,
        api: str,
        model: str,
        prompt: str,
        seconds: int,
        size: str = "720x1280",
        ratio: str = "adaptive",
        resolution: str = "720p",
        account_id: str | None = None,
    ) -> TaskRecord:
        now = int(time.time())
        record = TaskRecord(
            id=task_id,
            api=api,
            model=model,
            prompt=prompt,
            seconds=seconds,
            size=size,
            ratio=ratio,
            resolution=resolution,
            status="queued",
            progress=0,
            created_at=now,
            updated_at=now,
            account_id=account_id,
        )
        values = asdict(record)
        values["raw"] = None
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, api, model, prompt, seconds, size, ratio, resolution,
                    status, progress, created_at, updated_at, account_id, completed_at,
                    video_id, video_url, poster_url, error_code, error_message, raw
                ) VALUES (
                    :id, :api, :model, :prompt, :seconds, :size, :ratio, :resolution,
                    :status, :progress, :created_at, :updated_at, :account_id, :completed_at,
                    :video_id, :video_url, :poster_url, :error_code, :error_message, :raw
                )
                """,
                values,
            )
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._decode(row) if row else None

    def update(self, task_id: str, **changes: Any) -> TaskRecord:
        allowed = {
            "status",
            "progress",
            "updated_at",
            "completed_at",
            "video_id",
            "video_url",
            "poster_url",
            "error_code",
            "error_message",
            "raw",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"Unsupported task fields: {sorted(invalid)}")
        changes["updated_at"] = int(time.time())
        if changes.get("status") in {"succeeded", "failed"}:
            changes.setdefault("completed_at", changes["updated_at"])
        encoded = dict(changes)
        if "raw" in encoded:
            encoded["raw"] = json.dumps(encoded["raw"], ensure_ascii=False)
        assignments = ", ".join(f"{name} = :{name}" for name in encoded)
        encoded["id"] = task_id
        with self._lock, self._connect() as connection:
            connection.execute(f"UPDATE tasks SET {assignments} WHERE id = :id", encoded)
        record = self.get(task_id)
        if record is None:
            raise KeyError(task_id)
        return record

    def list(
        self,
        *,
        limit: int = 20,
        after: str | None = None,
        order: str = "desc",
        account_id: str | None = None,
    ) -> list[TaskRecord]:
        direction = "ASC" if order == "asc" else "DESC"
        params: list[Any] = []
        conditions: list[str] = []
        if after:
            cursor = self.get(after)
            if cursor:
                operator = ">" if direction == "ASC" else "<"
                conditions.append(f"created_at {operator} ?")
                params.append(cursor.created_at)
        if account_id:
            conditions.append("account_id = ?")
            params.append(account_id)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at {direction}, id {direction} LIMIT ?",
                params,
            ).fetchall()
        return [self._decode(row) for row in rows]

    def delete(self, task_id: str) -> bool:
        with self._lock, self._connect() as connection:
            result = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return result.rowcount > 0

    def create_account(self, *, account_id: str, name: str) -> dict[str, Any]:
        now = int(time.time())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts (id, name, enabled, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (account_id, name, now, now),
            )
        account = self.get_account(account_id)
        if account is None:
            raise RuntimeError("Failed to create account")
        return account

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM accounts ORDER BY created_at ASC, id ASC"
            ).fetchall()
        result = []
        for row in rows:
            account = dict(row)
            account["enabled"] = bool(account["enabled"])
            result.append(account)
        return result

    def update_account(self, account_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"name", "enabled", "last_error"}
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"Unsupported account fields: {sorted(invalid)}")
        if "enabled" in changes:
            changes["enabled"] = int(bool(changes["enabled"]))
        changes["updated_at"] = int(time.time())
        encoded = dict(changes)
        encoded["id"] = account_id
        assignments = ", ".join(f"{name} = :{name}" for name in changes)
        with self._lock, self._connect() as connection:
            result = connection.execute(
                f"UPDATE accounts SET {assignments} WHERE id = :id", encoded
            )
        if result.rowcount == 0:
            raise KeyError(account_id)
        account = self.get_account(account_id)
        if account is None:
            raise KeyError(account_id)
        return account

    def delete_account(self, account_id: str) -> bool:
        with self._lock, self._connect() as connection:
            result = connection.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        return result.rowcount > 0
