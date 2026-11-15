"""The watcher's saved point against a real Postgres: the migration and the
upsert. Skipped unless TEST_DATABASE_URL points at a throwaway database with
infra/init.sql and every migration applied; each test rolls back."""
import os
from pathlib import Path

import psycopg
import pytest

from core.repositories import mail_alerts as repo

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
MIGRATION = Path(__file__).resolve().parent.parent / "infra/migrations/0022_mail_alert_state.sql"


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def test_nothing_saved_before_the_first_pass_then_the_point_moves(conn):
    assert await repo.get_state(conn, "t@x.com") is None
    await repo.set_state(conn, "t@x.com", 77, 26)
    assert await repo.get_state(conn, "t@x.com") == (77, 26)
    await repo.set_state(conn, "t@x.com", 77, 30)
    assert await repo.get_state(conn, "t@x.com") == (77, 30)
    await repo.set_state(conn, "t@x.com", 99, 2)
    assert await repo.get_state(conn, "t@x.com") == (99, 2)


async def test_the_migration_applies_twice(conn):
    async with conn.cursor() as cur:
        await cur.execute(MIGRATION.read_text())
        await cur.execute(MIGRATION.read_text())


async def test_the_lock_is_held_per_mailbox_until_the_transaction_ends(conn):
    other = await psycopg.AsyncConnection.connect(URL or "")
    try:
        assert await repo.try_lock(conn, "lock@x.com") is True
        assert await repo.try_lock(other, "lock@x.com") is False
        assert await repo.try_lock(other, "outra@x.com") is True
        await conn.commit()
        await other.rollback()
        assert await repo.try_lock(other, "lock@x.com") is True
    finally:
        await other.rollback()
        await other.close()
