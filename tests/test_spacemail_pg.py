"""The audit table accepts 'spacemail' as a provider once migration 0021 is in.
Skipped unless TEST_DATABASE_URL points at a throwaway database with
infra/init.sql and every migration applied; each test rolls back."""
import os
from pathlib import Path

import psycopg
import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
MIGRATION = Path(__file__).resolve().parent.parent / "infra/migrations/0021_audit_spacemail.sql"


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _audit(conn, provider):
    async with conn.cursor() as cur:
        await cur.execute("INSERT INTO workspace_actions (provider, capability, action, "
                          "succeeded) VALUES (%s, 'mail', 'send_file', TRUE) RETURNING id",
                          (provider,))


async def test_the_audit_takes_a_spacemail_row_and_still_refuses_others(conn):
    await _audit(conn, "spacemail")
    with pytest.raises(psycopg.errors.CheckViolation):
        await _audit(conn, "nobody")


async def test_the_migration_applies_twice(conn):
    async with conn.cursor() as cur:
        await cur.execute(MIGRATION.read_text())
        await cur.execute(MIGRATION.read_text())
