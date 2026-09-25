from __future__ import annotations

import asyncio
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
