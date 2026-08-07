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
    advertiser_id: str | None = None
    completed_at: int | None = None
    video_id: str | None = None
    video_url: str | None = None
    poster_url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class EventRecord:
    id: int
    created_at: int
    level: str
    category: str
    message: str
    account_id: str | None = None
    task_id: str | None = None
    details: dict[str, Any] | None = None


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
                    advertiser_id TEXT,
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
            if "advertiser_id" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN advertiser_id TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_error TEXT,
                    username TEXT,
                    password_ciphertext TEXT,
                    email_address TEXT,
                    auto_login INTEGER NOT NULL DEFAULT 1,
                    login_state TEXT NOT NULL DEFAULT 'not_configured',
                    last_login_at INTEGER,
                    last_login_attempt INTEGER,
                    session_ciphertext TEXT,
                    session_updated_at INTEGER
                )
                """
            )
            account_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(accounts)").fetchall()
            }
            migrations = {
                "username": "TEXT",
                "password_ciphertext": "TEXT",
                "email_address": "TEXT",
                "auto_login": "INTEGER NOT NULL DEFAULT 1",
                "login_state": "TEXT NOT NULL DEFAULT 'not_configured'",
                "last_login_at": "INTEGER",
                "last_login_attempt": "INTEGER",
                "session_ciphertext": "TEXT",
                "session_updated_at": "INTEGER",
            }
            for name, declaration in migrations.items():
                if name not in account_columns:
                    connection.execute(
                        f"ALTER TABLE accounts ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS subaccounts (
                    account_id TEXT NOT NULL,
                    advertiser_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    account_type TEXT NOT NULL DEFAULT 'unknown',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    seedance_access INTEGER,
                    credits INTEGER,
                    active INTEGER NOT NULL DEFAULT 0,
                    last_checked_at INTEGER,
                    last_error TEXT,
                    quota_blocked_until INTEGER,
                    quota_reason TEXT,
                    quota_updated_at INTEGER,
                    PRIMARY KEY (account_id, advertiser_id)
                )
                """
            )
            subaccount_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(subaccounts)"
                ).fetchall()
            }
            subaccount_migrations = {
                "quota_blocked_until": "INTEGER",
                "quota_reason": "TEXT",
                "quota_updated_at": "INTEGER",
            }
            for name, declaration in subaccount_migrations.items():
                if name not in subaccount_columns:
                    connection.execute(
                        f"ALTER TABLE subaccounts ADD COLUMN {name} {declaration}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    account_id TEXT,
                    task_id TEXT,
                    details TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS events_created_at_idx "
                "ON events(created_at DESC, id DESC)"
            )

    @staticmethod
    def _decode(row: sqlite3.Row) -> TaskRecord:
        values = dict(row)
        values["raw"] = json.loads(values["raw"]) if values.get("raw") else None
        return TaskRecord(**values)

    @staticmethod
    def _decode_event(row: sqlite3.Row) -> EventRecord:
        values = dict(row)
        values["details"] = json.loads(values["details"]) if values.get("details") else None
        return EventRecord(**values)

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
        advertiser_id: str | None = None,
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
            advertiser_id=advertiser_id,
        )
        values = asdict(record)
        values["raw"] = None
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, api, model, prompt, seconds, size, ratio, resolution,
                    status, progress, created_at, updated_at, account_id, advertiser_id, completed_at,
                    video_id, video_url, poster_url, error_code, error_message, raw
                ) VALUES (
                    :id, :api, :model, :prompt, :seconds, :size, :ratio, :resolution,
                    :status, :progress, :created_at, :updated_at, :account_id, :advertiser_id, :completed_at,
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
        status: str | None = None,
        search: str | None = None,
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
        if status:
            conditions.append("status = ?")
            params.append(status)
        if search:
            conditions.append(
                "(id LIKE ? OR prompt LIKE ? OR model LIKE ? OR account_id LIKE ? "
                "OR advertiser_id LIKE ?)"
            )
            pattern = f"%{search}%"
            params.extend([pattern] * 5)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks {where} ORDER BY created_at {direction}, id {direction} LIMIT ?",
                params,
            ).fetchall()
        return [self._decode(row) for row in rows]

    def add_event(
        self,
        *,
        level: str,
        category: str,
        message: str,
        account_id: str | None = None,
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> EventRecord:
        created_at = int(time.time())
        encoded_details = (
            json.dumps(details, ensure_ascii=False, separators=(",", ":"))
            if details
            else None
        )
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events (
                    created_at, level, category, message, account_id, task_id, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    level,
                    category,
                    message,
                    account_id,
                    task_id,
                    encoded_details,
                ),
            )
            event_id = int(cursor.lastrowid)
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("Could not read the event after insertion")
        return self._decode_event(row)

    def task_counts(self) -> dict[str, int]:
        counts = {"total": 0, "queued": 0, "running": 0, "succeeded": 0, "failed": 0}
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
        for row in rows:
            status = str(row["status"])
            value = int(row["count"])
            counts[status] = value
            counts["total"] += value
        return counts

    def duration_analytics_rows(
        self,
        *,
        since: int,
        until: int,
    ) -> list[dict[str, Any]]:
        """Return the small task projection needed by the admin duration dashboard."""
        conditions = ["created_at >= ?", "created_at < ?"]
        params: list[Any] = [since, until]
        where = " AND ".join(conditions)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT status, model, account_id, created_at, completed_at
                FROM tasks
                WHERE {where}
                ORDER BY created_at ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def active_task_ids(
        self, account_id: str, advertiser_id: str
    ) -> list[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM tasks
                WHERE account_id = ? AND advertiser_id = ?
                  AND status IN ('queued', 'running')
                ORDER BY created_at, id
                """,
                (account_id, advertiser_id),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def task_count_since(
        self, account_id: str, advertiser_id: str, since: int
    ) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM tasks
                WHERE account_id = ? AND advertiser_id = ? AND created_at >= ?
                """,
                (account_id, advertiser_id, since),
            ).fetchone()
        return int(row["count"] if row else 0)

    def list_events(
        self,
        *,
        limit: int = 200,
        level: str | None = None,
        category: str | None = None,
        search: str | None = None,
    ) -> list[EventRecord]:
        conditions: list[str] = []
        params: list[Any] = []
        if level:
            conditions.append("level = ?")
            params.append(level)
        if category:
            conditions.append("category = ?")
            params.append(category)
        if search:
            conditions.append(
                "(message LIKE ? OR account_id LIKE ? OR task_id LIKE ? OR details LIKE ?)"
            )
            pattern = f"%{search}%"
            params.extend([pattern] * 4)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM events {where} ORDER BY created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._decode_event(row) for row in rows]

    def delete(self, task_id: str) -> bool:
        with self._lock, self._connect() as connection:
            result = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return result.rowcount > 0

    def create_account(
        self,
        *,
        account_id: str,
        name: str,
        username: str | None = None,
        password_ciphertext: str | None = None,
        email_address: str | None = None,
        auto_login: bool = True,
    ) -> dict[str, Any]:
        now = int(time.time())
        login_state = "pending" if username and password_ciphertext else "not_configured"
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts (
                    id, name, enabled, created_at, updated_at, username,
                    password_ciphertext, email_address, auto_login, login_state
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    name,
                    now,
                    now,
                    username,
                    password_ciphertext,
                    email_address,
                    int(auto_login),
                    login_state,
                ),
            )
        account = self.get_account(account_id)
        if account is None:
            raise RuntimeError("Failed to create account")
        return account

    @staticmethod
    def _decode_account(row: sqlite3.Row, *, include_secret: bool = False) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["auto_login"] = bool(result.get("auto_login", 1))
        result["credentials_configured"] = bool(
            result.get("username") and result.get("password_ciphertext")
        )
        result["session_available"] = bool(result.get("session_ciphertext"))
        if not include_secret:
            result.pop("password_ciphertext", None)
            result.pop("session_ciphertext", None)
        return result

    def get_account(
        self, account_id: str, *, include_secret: bool = False
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        if row is None:
            return None
        return self._decode_account(row, include_secret=include_secret)

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM accounts ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [self._decode_account(row) for row in rows]

    def update_account(self, account_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {
            "name",
            "enabled",
            "last_error",
            "username",
            "password_ciphertext",
            "email_address",
            "auto_login",
            "login_state",
            "last_login_at",
            "last_login_attempt",
            "session_ciphertext",
            "session_updated_at",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"Unsupported account fields: {sorted(invalid)}")
        if "enabled" in changes:
            changes["enabled"] = int(bool(changes["enabled"]))
        if "auto_login" in changes:
            changes["auto_login"] = int(bool(changes["auto_login"]))
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

    def account_credentials(self, account_id: str) -> dict[str, str] | None:
        account = self.get_account(account_id, include_secret=True)
        if not account or not account.get("username") or not account.get("password_ciphertext"):
            return None
        return {
            "username": str(account["username"]),
            "password_ciphertext": str(account["password_ciphertext"]),
            "email_address": str(account.get("email_address") or account["username"]),
        }

    def account_session(self, account_id: str) -> str | None:
        account = self.get_account(account_id, include_secret=True)
        if not account or not account.get("session_ciphertext"):
            return None
        return str(account["session_ciphertext"])

    def delete_account(self, account_id: str) -> bool:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM subaccounts WHERE account_id = ?", (account_id,))
            result = connection.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        return result.rowcount > 0

    @staticmethod
    def _decode_subaccount(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["active"] = bool(result["active"])
        if result.get("seedance_access") is not None:
            result["seedance_access"] = bool(result["seedance_access"])
        return result

    def list_subaccounts(self, account_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            if account_id is None:
                rows = connection.execute(
                    "SELECT * FROM subaccounts ORDER BY account_id, account_type, name, advertiser_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM subaccounts
                    WHERE account_id = ?
                    ORDER BY account_type, name, advertiser_id
                    """,
                    (account_id,),
                ).fetchall()
        return [self._decode_subaccount(row) for row in rows]

    def upsert_subaccounts(
        self, account_id: str, subaccounts: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        now = int(time.time())
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE subaccounts SET active = 0 WHERE account_id = ?", (account_id,)
            )
            for item in subaccounts:
                seedance_access = item.get("seedance_access")
                connection.execute(
                    """
                    INSERT INTO subaccounts (
                        account_id, advertiser_id, name, account_type, enabled,
                        seedance_access, credits, active, last_checked_at, last_error
                    ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id, advertiser_id) DO UPDATE SET
                        name = excluded.name,
                        account_type = excluded.account_type,
                        seedance_access = COALESCE(
                            excluded.seedance_access, subaccounts.seedance_access
                        ),
                        credits = COALESCE(excluded.credits, subaccounts.credits),
                        active = excluded.active,
                        last_checked_at = excluded.last_checked_at,
                        last_error = excluded.last_error
                    """,
                    (
                        account_id,
                        str(item["advertiser_id"]),
                        str(item.get("name") or item["advertiser_id"]),
                        str(item.get("account_type") or "unknown"),
                        None if seedance_access is None else int(bool(seedance_access)),
                        item.get("credits"),
                        int(bool(item.get("active"))),
                        int(item.get("last_checked_at") or now),
                        item.get("last_error"),
                    ),
                )
        return self.list_subaccounts(account_id)

    def set_subaccount_enabled(
        self, account_id: str, advertiser_id: str, enabled: bool
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            result = connection.execute(
                """
                UPDATE subaccounts SET enabled = ?
                WHERE account_id = ? AND advertiser_id = ?
                """,
                (int(enabled), account_id, advertiser_id),
            )
        if result.rowcount == 0:
            raise KeyError((account_id, advertiser_id))
        return next(
            item
            for item in self.list_subaccounts(account_id)
            if item["advertiser_id"] == advertiser_id
        )

    def update_subaccount(
        self, account_id: str, advertiser_id: str, **changes: Any
    ) -> dict[str, Any]:
        allowed = {
            "name",
            "account_type",
            "seedance_access",
            "credits",
            "active",
            "last_checked_at",
            "last_error",
            "quota_blocked_until",
            "quota_reason",
            "quota_updated_at",
        }
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"Unsupported subaccount fields: {sorted(invalid)}")
        for key in ("seedance_access", "active"):
            if key in changes and changes[key] is not None:
                changes[key] = int(bool(changes[key]))
        assignments = ", ".join(f"{name} = :{name}" for name in changes)
        encoded = {**changes, "account_id": account_id, "advertiser_id": advertiser_id}
        with self._lock, self._connect() as connection:
            result = connection.execute(
                f"""
                UPDATE subaccounts SET {assignments}
                WHERE account_id = :account_id AND advertiser_id = :advertiser_id
                """,
                encoded,
            )
        if result.rowcount == 0:
            raise KeyError((account_id, advertiser_id))
        return next(
            item
            for item in self.list_subaccounts(account_id)
            if item["advertiser_id"] == advertiser_id
        )
