from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass(slots=True)
class SpinResult:
    spin_number: int
    message: str
    spun_at: str


class Database:
    """Small SQLite repository with serialized writes for the bot state."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.execute("PRAGMA busy_timeout = 5000")
        await self._create_schema()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    def _db(self) -> aiosqlite.Connection:
        if self.conn is None:
            raise RuntimeError("Database connection has not been opened.")
        return self.conn

    async def _create_schema(self) -> None:
        db = self._db()
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                channel_url TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_tasks (
                user_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, task_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS prizes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_spin_outcomes (
                user_id INTEGER NOT NULL,
                spin_number INTEGER NOT NULL CHECK (spin_number IN (1, 2)),
                message TEXT NOT NULL,
                assigned_by INTEGER NOT NULL,
                assigned_at TEXT NOT NULL,
                PRIMARY KEY (user_id, spin_number),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS spin_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                spin_number INTEGER NOT NULL CHECK (spin_number IN (1, 2)),
                message TEXT NOT NULL,
                spun_at TEXT NOT NULL,
                UNIQUE (user_id, spin_number),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_user_tasks_user ON user_tasks(user_id);
            CREATE INDEX IF NOT EXISTS idx_spin_history_user ON spin_history(user_id);
            """
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES('new_users_blocked', '0')"
        )
        await db.commit()

    async def get_setting(self, key: str, default: str = "") -> str:
        row = await (await self._db().execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )).fetchone()
        return str(row["value"]) if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self._lock:
            await self._db().execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await self._db().commit()

    async def new_users_blocked(self) -> bool:
        return await self.get_setting("new_users_blocked", "0") == "1"

    async def ensure_user(self, user: Any, *, admin_exempt: bool = False) -> bool:
        """Registers a user unless new registrations are blocked. Existing users remain allowed."""
        async with self._lock:
            db = self._db()
            existing = await (await db.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (user.id,)
            )).fetchone()
            if existing:
                await db.execute(
                    "UPDATE users SET username = ?, first_name = ?, last_name = ? WHERE user_id = ?",
                    (user.username or "", user.first_name or "", user.last_name or "", user.id),
                )
                await db.commit()
                return True

            blocked_row = await (await db.execute(
                "SELECT value FROM settings WHERE key = 'new_users_blocked'"
            )).fetchone()
            blocked = bool(blocked_row and blocked_row["value"] == "1")
            if blocked and not admin_exempt:
                return False

            await db.execute(
                "INSERT INTO users(user_id, username, first_name, last_name, created_at) VALUES(?, ?, ?, ?, ?)",
                (user.id, user.username or "", user.first_name or "", user.last_name or "", utc_now()),
            )
            await db.commit()
            return True

    async def add_task(self, title: str, channel_url: str) -> int:
        async with self._lock:
            cursor = await self._db().execute(
                "INSERT INTO tasks(title, channel_url, created_at) VALUES(?, ?, ?)",
                (title.strip(), channel_url.strip(), utc_now()),
            )
            await self._db().commit()
            return int(cursor.lastrowid)

    async def get_tasks(self) -> list[aiosqlite.Row]:
        cursor = await self._db().execute(
            "SELECT * FROM tasks WHERE is_active = 1 ORDER BY id ASC"
        )
        return await cursor.fetchall()

    async def get_task(self, task_id: int) -> aiosqlite.Row | None:
        return await (await self._db().execute(
            "SELECT * FROM tasks WHERE id = ? AND is_active = 1", (task_id,)
        )).fetchone()

    async def deactivate_task(self, task_id: int) -> None:
        async with self._lock:
            await self._db().execute("UPDATE tasks SET is_active = 0 WHERE id = ?", (task_id,))
            await self._db().commit()

    async def completed_task_ids(self, user_id: int) -> set[int]:
        rows = await (await self._db().execute(
            "SELECT task_id FROM user_tasks WHERE user_id = ?", (user_id,)
        )).fetchall()
        return {int(row["task_id"]) for row in rows}

    async def complete_task(self, user_id: int, task_id: int) -> bool:
        """Marks a task done without checking channel membership, as requested."""
        async with self._lock:
            cursor = await self._db().execute(
                "INSERT OR IGNORE INTO user_tasks(user_id, task_id, completed_at) VALUES(?, ?, ?)",
                (user_id, task_id, utc_now()),
            )
            await self._db().commit()
            return cursor.rowcount > 0

    async def task_progress(self, user_id: int) -> tuple[int, int]:
        tasks = await self.get_tasks()
        if not tasks:
            return 0, 0
        completed = await self.completed_task_ids(user_id)
        done = sum(int(task["id"]) in completed for task in tasks)
        return done, len(tasks)

    async def get_spin_count(self, user_id: int) -> int:
        row = await (await self._db().execute(
            "SELECT COUNT(*) AS count FROM spin_history WHERE user_id = ?", (user_id,)
        )).fetchone()
        return int(row["count"])

    async def get_assigned_outcome(self, user_id: int, spin_number: int) -> aiosqlite.Row | None:
        return await (await self._db().execute(
            "SELECT * FROM user_spin_outcomes WHERE user_id = ? AND spin_number = ?",
            (user_id, spin_number),
        )).fetchone()

    async def assign_outcome(
        self, user_id: int, spin_number: int, message: str, assigned_by: int
    ) -> None:
        async with self._lock:
            await self._db().execute(
                "INSERT INTO user_spin_outcomes(user_id, spin_number, message, assigned_by, assigned_at) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, spin_number) DO UPDATE SET "
                "message = excluded.message, assigned_by = excluded.assigned_by, assigned_at = excluded.assigned_at",
                (user_id, spin_number, message.strip(), assigned_by, utc_now()),
            )
            await self._db().commit()

    async def spin(self, user_id: int) -> SpinResult | None:
        """Creates one spin atomically. Returns None when it is not pre-assigned or exhausted."""
        async with self._lock:
            db = self._db()
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (await db.execute(
                    "SELECT COUNT(*) AS count FROM spin_history WHERE user_id = ?", (user_id,)
                )).fetchone()
                spin_number = int(row["count"]) + 1
                if spin_number > 2:
                    await db.rollback()
                    return None

                outcome = await (await db.execute(
                    "SELECT message FROM user_spin_outcomes WHERE user_id = ? AND spin_number = ?",
                    (user_id, spin_number),
                )).fetchone()
                if not outcome:
                    await db.rollback()
                    return None

                spun_at = utc_now()
                message = str(outcome["message"])
                await db.execute(
                    "INSERT INTO spin_history(user_id, spin_number, message, spun_at) VALUES(?, ?, ?, ?)",
                    (user_id, spin_number, message, spun_at),
                )
                await db.commit()
                return SpinResult(spin_number=spin_number, message=message, spun_at=spun_at)
            except Exception:
                await db.rollback()
                raise

    async def get_earnings(self, user_id: int) -> list[aiosqlite.Row]:
        cursor = await self._db().execute(
            "SELECT spin_number, message, spun_at FROM spin_history WHERE user_id = ? ORDER BY spin_number ASC",
            (user_id,),
        )
        return await cursor.fetchall()

    async def add_prize(self, name: str, description: str) -> int:
        async with self._lock:
            cursor = await self._db().execute(
                "INSERT INTO prizes(name, description, created_at) VALUES(?, ?, ?)",
                (name.strip(), description.strip(), utc_now()),
            )
            await self._db().commit()
            return int(cursor.lastrowid)

    async def get_prizes(self) -> list[aiosqlite.Row]:
        cursor = await self._db().execute(
            "SELECT * FROM prizes WHERE is_active = 1 ORDER BY id DESC"
        )
        return await cursor.fetchall()

    async def get_prize(self, prize_id: int) -> aiosqlite.Row | None:
        return await (await self._db().execute(
            "SELECT * FROM prizes WHERE id = ? AND is_active = 1", (prize_id,)
        )).fetchone()

    async def deactivate_prize(self, prize_id: int) -> None:
        async with self._lock:
            await self._db().execute("UPDATE prizes SET is_active = 0 WHERE id = ?", (prize_id,))
            await self._db().commit()

    async def user_count(self) -> int:
        row = await (await self._db().execute("SELECT COUNT(*) AS count FROM users")).fetchone()
        return int(row["count"])

    async def list_users(self, page: int, per_page: int = 8) -> tuple[list[aiosqlite.Row], int]:
        total = await self.user_count()
        offset = max(page, 0) * per_page
        cursor = await self._db().execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset)
        )
        return await cursor.fetchall(), total

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        return await (await self._db().execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )).fetchone()

    async def stats(self) -> dict[str, int]:
        db = self._db()
        users = await (await db.execute("SELECT COUNT(*) AS count FROM users")).fetchone()
        tasks = await (await db.execute("SELECT COUNT(*) AS count FROM tasks WHERE is_active = 1")).fetchone()
        prizes = await (await db.execute("SELECT COUNT(*) AS count FROM prizes WHERE is_active = 1")).fetchone()
        spins = await (await db.execute("SELECT COUNT(*) AS count FROM spin_history")).fetchone()
        return {
            "users": int(users["count"]),
            "tasks": int(tasks["count"]),
            "prizes": int(prizes["count"]),
            "spins": int(spins["count"]),
        }
