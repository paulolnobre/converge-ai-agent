"""Leads the team adds, against a real Postgres (TEST_DATABASE_URL): listed in
the Inbox before any message, and adopted by the other form of the same
Brazilian mobile when the person first writes."""
import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from core.repositories import contacts, inbox

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


def _mobile() -> tuple[str, str]:
    """The same mobile with and without the ninth digit."""
    tail = f"{uuid.uuid4().int % 10_000_000:07d}"
    return f"wa:55919{'8' + tail}", f"wa:5591{'8' + tail}"


async def _lead(conn, chat_id: str) -> tuple[int, int]:
    cur = await conn.execute(
        "INSERT INTO panel_users (email, name, password_hash) VALUES (%s, 'A', 'h') RETURNING id",
        (f"{uuid.uuid4().hex[:8]}@x.test",))
    user_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO contacts (chat_id, source, origin, origin_set_by) "
        "VALUES (%s, 'whatsapp', 'email', %s) RETURNING id", (chat_id, user_id))
    return (await cur.fetchone())[0], user_id


async def test_a_lead_without_messages_is_listed(conn):
    typed, _ = _mobile()
    lead, user_id = await _lead(conn, typed)

    rows = await inbox.list_conversations(conn, user_id=user_id, limit=1000)

    assert lead in [row["contact_id"] for row in rows]


async def test_the_first_reply_from_the_other_form_is_the_same_contact(conn):
    typed, meta = _mobile()
    lead, _ = await _lead(conn, typed)
    await conn.execute("INSERT INTO sessions (contact_id, chat_id, role, message) "
                       "VALUES (%s, %s, 'assistant', 'oi')", (lead, typed))

    contact = await contacts.get_or_create(conn, meta, "Ana", inbound_at=datetime.now(UTC))

    assert contact.id == lead and contact.chat_id == meta
    cur = await conn.execute("SELECT DISTINCT chat_id FROM sessions WHERE contact_id = %s", (lead,))
    assert await cur.fetchall() == [(meta,)]


async def test_a_contact_that_already_wrote_is_not_adopted(conn):
    typed, meta = _mobile()
    lead, _ = await _lead(conn, typed)
    await conn.execute("UPDATE contacts SET last_inbound_at = NOW() WHERE id = %s", (lead,))

    contact = await contacts.get_or_create(conn, meta, "Ana", inbound_at=datetime.now(UTC))

    assert contact.id != lead


async def test_a_claim_made_before_they_wrote_keeps_its_owner(conn):
    typed, meta = _mobile()
    lead, user_id = await _lead(conn, typed)
    await conn.execute(
        "INSERT INTO handoff_sessions (contact_id, chat_id, team_chat_id, reason, status, "
        "claimed_by_user_id) VALUES (%s, %s, 't', 'painel', 'claimed', %s)",
        (lead, typed, user_id))

    await contacts.get_or_create(conn, meta, "Ana", inbound_at=datetime.now(UTC))

    cur = await conn.execute("SELECT chat_id, claimed_by_user_id FROM handoff_sessions "
                             "WHERE contact_id = %s", (lead,))
    assert await cur.fetchall() == [(meta, user_id)]


async def test_adoption_waits_for_a_panel_action_holding_the_chat(conn):
    """The panel takes the chat lock before any row lock; adoption takes the
    same lock first, so the two never deadlock and never interleave."""
    import asyncio

    from core.repositories import handoffs

    typed, meta = _mobile()
    lead, _ = await _lead(conn, typed)
    await conn.commit()
    panel = await psycopg.AsyncConnection.connect(URL or "")
    try:
        await handoffs.lock_chat(panel, typed)
        adoption = asyncio.create_task(
            contacts.get_or_create(conn, meta, "Ana", inbound_at=datetime.now(UTC)))
        await asyncio.sleep(0.3)
        assert not adoption.done()
        await panel.commit()
        contact = await asyncio.wait_for(adoption, 5)
        assert contact.id == lead and contact.chat_id == meta
    finally:
        await panel.close()
        await conn.rollback()
        await conn.execute("DELETE FROM contacts WHERE id = %s", (lead,))
        await conn.commit()


async def test_creating_the_same_chat_at_once_gives_one_contact(conn):
    """The panel adding a lead and the person's first message, together."""
    import asyncio

    chat_id = f"wa:49{uuid.uuid4().int % 10**10:010d}"
    other = await psycopg.AsyncConnection.connect(URL or "")
    try:
        first = await contacts.get_or_create(conn, chat_id, "Ana")
        second = asyncio.create_task(contacts.get_or_create(other, chat_id, "Ana"))
        await asyncio.sleep(0.3)
        assert not second.done()
        await conn.commit()
        assert (await asyncio.wait_for(second, 5)).id == first.id
    finally:
        await other.rollback()
        await other.close()
        await conn.execute("DELETE FROM contacts WHERE chat_id = %s", (chat_id,))
        await conn.commit()
