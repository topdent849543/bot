from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

from bot.db import Database


def test_task_progress_and_two_preassigned_spins(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "test.db"))
        await database.connect()
        try:
            user = SimpleNamespace(id=101, username="demo", first_name="Demo", last_name="User")
            assert await database.ensure_user(user)
            first_task = await database.add_task("اشترك بالقناة الأولى", "https://t.me/example_one")
            second_task = await database.add_task("اشترك بالقناة الثانية", "https://t.me/example_two")

            assert await database.task_progress(user.id) == (0, 2)
            await database.complete_task(user.id, first_task)
            assert await database.task_progress(user.id) == (1, 2)
            await database.complete_task(user.id, second_task)
            assert await database.task_progress(user.id) == (2, 2)

            await database.assign_outcome(user.id, 1, "حظ أوفر", 999)
            await database.assign_outcome(user.id, 2, "مبروك", 999)
            assert (await database.spin(user.id)).message == "حظ أوفر"
            assert (await database.spin(user.id)).message == "مبروك"
            assert await database.spin(user.id) is None
            assert await database.get_spin_count(user.id) == 2
        finally:
            await database.close()

    asyncio.run(scenario())


def test_blocking_applies_only_to_new_users(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "test.db"))
        await database.connect()
        try:
            existing = SimpleNamespace(id=1, username="old", first_name="Old", last_name="")
            newcomer = SimpleNamespace(id=2, username="new", first_name="New", last_name="")
            assert await database.ensure_user(existing)
            await database.set_setting("new_users_blocked", "1")
            assert await database.ensure_user(existing)
            assert not await database.ensure_user(newcomer)
            assert await database.ensure_user(newcomer, admin_exempt=True)
        finally:
            await database.close()

    asyncio.run(scenario())


def test_manual_outcome_can_be_assigned_before_user_starts_bot(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "test.db"))
        await database.connect()
        try:
            future_user_id = 987654321
            await database.assign_outcome(future_user_id, 1, "جائزة خاصة", 999)
            assert await database.get_user(future_user_id) is None
            assert (await database.get_assigned_outcome(future_user_id, 1))["message"] == "جائزة خاصة"

            future_user = SimpleNamespace(
                id=future_user_id, username=None, first_name="New", last_name="User"
            )
            assert await database.ensure_user(future_user)
            assert (await database.spin(future_user_id)).message == "جائزة خاصة"
            assert (await database.spin(future_user_id)).message == "🍀 حظ أوفر! نتمنى لك حظًا أفضل في المرة القادمة."
            assert await database.spin(future_user_id) is None
        finally:
            await database.close()

    asyncio.run(scenario())


def test_unassigned_user_gets_two_default_losing_results(tmp_path):
    async def scenario():
        database = Database(str(tmp_path / "test.db"))
        await database.connect()
        try:
            user = SimpleNamespace(id=321, username=None, first_name="Test", last_name="")
            assert await database.ensure_user(user)
            assert (await database.spin(user.id)).message == "🍀 حظ أوفر! نتمنى لك حظًا أفضل في المرة القادمة."
            assert (await database.spin(user.id)).message == "🍀 حظ أوفر! نتمنى لك حظًا أفضل في المرة القادمة."
            assert await database.spin(user.id) is None
        finally:
            await database.close()

    asyncio.run(scenario())


def test_existing_database_migrates_outcome_assignments_for_unknown_ids(tmp_path):
    async def scenario():
        path = tmp_path / "legacy.db"
        legacy = sqlite3.connect(path)
        legacy.execute("PRAGMA foreign_keys = ON")
        legacy.executescript(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE user_spin_outcomes (
                user_id INTEGER NOT NULL,
                spin_number INTEGER NOT NULL CHECK (spin_number IN (1, 2)),
                message TEXT NOT NULL,
                assigned_by INTEGER NOT NULL,
                assigned_at TEXT NOT NULL,
                PRIMARY KEY (user_id, spin_number),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            INSERT INTO users(user_id, created_at) VALUES(1, 'old');
            INSERT INTO user_spin_outcomes VALUES(1, 1, 'Legacy reward', 9, 'old');
            """
        )
        legacy.close()

        database = Database(str(path))
        await database.connect()
        try:
            assert (await database.get_assigned_outcome(1, 1))["message"] == "Legacy reward"
            await database.assign_outcome(987654321, 2, "Pending reward", 9)
            assert (await database.get_assigned_outcome(987654321, 2))["message"] == "Pending reward"
        finally:
            await database.close()

    asyncio.run(scenario())
