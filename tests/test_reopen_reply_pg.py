"""The reply the owner leaves with a reopen template, against a real Postgres
(TEST_DATABASE_URL): it lands sealed, and the first message after the tap
takes it once."""
import os
import uuid

import psycopg
import pytest

from core.repositories import handoffs as repo

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


async def _session(conn, status: str = "claimed") -> tuple[int, str]:
    chat_id = "wa:" + uuid.uuid4().hex[:10]
    cur = await conn.execute(
        "INSERT INTO handoff_sessions (chat_id, team_chat_id, reason, status, claimed_by_name) "
        "VALUES (%s, '-1', 'pedido', %s, 'Victor Lobato') RETURNING id", (chat_id, status))
    return (await cur.fetchone())[0], chat_id


async def test_the_reply_is_sealed_and_taken_once(conn):
    session_id, chat_id = await _session(conn)
    await repo.set_reopen_pending(conn, session_id, "Victor:\nSegue o valor.")

    cur = await conn.execute("SELECT reopen_reply, reopen_reply_sealed FROM handoff_sessions "
                             "WHERE id = %s", (session_id,))
    clear, sealed = await cur.fetchone()
    assert clear is None and sealed is not None and b"Segue" not in bytes(sealed)

    first = await repo.take_reopen_pending(conn, chat_id)
    assert first is not None and first.reply == "Victor:\nSegue o valor."
    assert first.owner_name == "Victor Lobato"
    assert await repo.take_reopen_pending(conn, chat_id) is None


async def test_a_reopen_without_reply_is_still_taken_once(conn):
    session_id, chat_id = await _session(conn)
    await repo.set_reopen_pending(conn, session_id, None)

    first = await repo.take_reopen_pending(conn, chat_id)
    assert first is not None and first.reply is None
    assert await repo.take_reopen_pending(conn, chat_id) is None


async def test_nothing_pending_without_a_reopen_or_once_closed(conn):
    _, chat_id = await _session(conn)
    assert await repo.take_reopen_pending(conn, chat_id) is None
    closed_id, closed_chat = await _session(conn, "closed")
    await repo.set_reopen_pending(conn, closed_id, "Oi")
    assert await repo.take_reopen_pending(conn, closed_chat) is None


async def test_the_database_refuses_the_reply_in_clear(conn):
    session_id, _ = await _session(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        await conn.execute("UPDATE handoff_sessions SET reopen_reply = 'claro' WHERE id = %s",
                           (session_id,))


async def test_the_inactivity_sweep_keeps_a_session_waiting_for_the_tap(conn):
    session_id, _ = await _session(conn)
    await repo.set_reopen_pending(conn, session_id, "Victor:\nSegue o valor.")
    await conn.execute("UPDATE handoff_sessions SET last_activity_at = NOW() - interval '3 hours' "
                       "WHERE id = %s", (session_id,))

    stale = [item.session.id for item in await repo.stale_sessions(conn, 60)]
    assert session_id not in stale
    assert await repo.close_if_idle(conn, session_id, 60) is False

    await conn.execute("UPDATE handoff_sessions SET reopen_pending_at = NOW() - interval '8 days' "
                       "WHERE id = %s", (session_id,))
    assert await repo.close_if_idle(conn, session_id, 60) is True
