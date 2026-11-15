"""Blocking and ending, against a real Postgres: the lists, the team's
"new conversation" notice, and what goes out on its own. Skipped unless
TEST_DATABASE_URL points at a throwaway database with infra/init.sql and every
migration applied; each test rolls back."""
import os
from pathlib import Path

import psycopg
import pytest

from core.repositories import automations as automations_repo
from core.repositories import contacts as contacts_repo
from core.repositories import inbox as inbox_repo

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
MIGRATION = Path(__file__).resolve().parent.parent / "infra/migrations/0028_block_and_end.sql"


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    await connection.execute("SELECT 1")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _user(conn) -> int:
    async with conn.cursor() as cur:
        await cur.execute("INSERT INTO panel_users (email, name, password_hash) "
                          "VALUES ('block@x.com', 'P', 'h') RETURNING id")
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _ids(conn, user_id, state) -> set[int]:
    rows = await inbox_repo.list_conversations(conn, user_id=user_id, state=state)
    return {r["contact_id"] for r in rows}


async def test_the_migration_applies_twice(conn):
    async with conn.cursor() as cur:
        await cur.execute(MIGRATION.read_text())
        await cur.execute(MIGRATION.read_text())


async def test_a_blocked_contact_is_only_under_bloqueadas(conn):
    user = await _user(conn)
    chat = "wa:5591922220001"
    contact = await contacts_repo.get_or_create(conn, chat, name="Troll")
    assert contact.id is not None
    await contacts_repo.record_message(conn, contact.id, chat, "user", "oi")
    assert contact.id in await _ids(conn, user, "all")

    await contacts_repo.set_blocked(conn, contact.id, user)

    assert await contacts_repo.is_blocked(conn, chat)
    assert (await contacts_repo.get_or_create(conn, chat)).blocked_at is not None
    for state in ("all", "bot", "archived", "waiting"):
        assert contact.id not in await _ids(conn, user, state), state
    assert contact.id in await _ids(conn, user, "blocked")
    found = await inbox_repo.conversation(conn, contact.id, user)
    assert found is not None and found["blocked"] is True

    await contacts_repo.set_blocked(conn, contact.id, None)
    assert not await contacts_repo.is_blocked(conn, chat)
    assert contact.id in await _ids(conn, user, "all")


async def test_an_ended_conversation_is_archived_until_the_person_writes(conn):
    user = await _user(conn)
    chat = "wa:5591922220002"
    contact = await contacts_repo.get_or_create(conn, chat, name="Ana")
    assert contact.id is not None
    await conn.execute("UPDATE contacts SET conversation_noticed_at = NOW() - INTERVAL "
                       "'2 minutes' WHERE id = %s", (contact.id,))
    await contacts_repo.record_message(conn, contact.id, chat, "user", "oi")
    await conn.execute("UPDATE sessions SET created_at = NOW() - INTERVAL '1 minute' "
                       "WHERE contact_id = %s", (contact.id,))

    await contacts_repo.set_ended(conn, contact.id)

    assert contact.id not in await _ids(conn, user, "all")
    assert contact.id in await _ids(conn, user, "archived")
    found = await inbox_repo.conversation(conn, contact.id, user)
    assert found is not None and found["ended"] is True
    # A reminder the bot sends does not bring it back.
    await contacts_repo.record_message(conn, contact.id, chat, "assistant", "Lembrete")
    assert contact.id in await _ids(conn, user, "archived")

    # Writing again: a new conversation, back in the list, the team told again
    # (it was told 2 minutes ago, inside the 24h).
    assert await inbox_repo.claim_conversation_notice(conn, contact.id)
    await contacts_repo.record_message(conn, contact.id, chat, "user", "voltei")
    await conn.execute("UPDATE contacts SET ended_at = NOW() - INTERVAL '30 seconds' "
                       "WHERE id = %s", (contact.id,))   # ended before "voltei"
    assert contact.id in await _ids(conn, user, "all")


async def test_nothing_queued_goes_to_a_blocked_contact(conn):
    user = await _user(conn)
    chat = "tg:922220003"
    contact = await contacts_repo.get_or_create(conn, chat, name="Bia")
    assert contact.id is not None
    automations = await automations_repo.list_automations(conn)
    queue_id = await automations_repo.enqueue(conn, automations[0]["id"], contact.id, None,
                                              "approved", "teste")
    assert queue_id is not None
    await contacts_repo.set_blocked(conn, contact.id, user)

    picked = [r for r in await automations_repo.approved(conn, 200) if r["id"] == queue_id]
    assert picked and picked[0]["automation_opt_out"], "discarded when it would go out"

    await automations_repo.discard_for_contact(conn, contact.id)
    assert not [r for r in await automations_repo.approved(conn, 200) if r["id"] == queue_id]
