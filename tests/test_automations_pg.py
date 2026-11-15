"""The queue's SQL against a real Postgres (TEST_DATABASE_URL): WhatsApp is
picked only when asked for, an approved item carries when it was queued, and
"did they write since" sees only what the customer wrote."""
import os
import uuid
from datetime import timedelta

import psycopg
import pytest

from core.repositories import automations as repo
from core.repositories import contacts as contacts_repo
from core.sealed_fields import field, write

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    await connection.execute("SELECT 1")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _contact(conn, source: str, chat_prefix: str = "") -> tuple[int, str]:
    chat = f"{chat_prefix}{uuid.uuid4().int % 10**11}"
    cur = await conn.execute(
        "INSERT INTO contacts (chat_id, source, stage) VALUES (%s, %s, 'respondido') RETURNING id",
        (chat, source))
    return (await cur.fetchone())[0], chat


async def _said(conn, contact_id, chat, role, text, days_ago=0):
    await contacts_repo.record_message(conn, contact_id, chat, role, text)
    await conn.execute(
        "UPDATE sessions SET created_at = NOW() - make_interval(days => %s) "
        "WHERE id = (SELECT max(id) FROM sessions WHERE contact_id = %s)", (days_ago, contact_id))


async def _stalled_automation(conn) -> int:
    cur = await conn.execute(
        "INSERT INTO automations (kind, enabled, mode, days, message) "
        "VALUES ('stalled', TRUE, 'approve', 1, 'Oi') "
        "ON CONFLICT (kind) DO UPDATE SET enabled = TRUE RETURNING id")
    return (await cur.fetchone())[0]


async def test_whatsapp_is_picked_only_when_asked_for(conn):
    wa_id, wa_chat = await _contact(conn, "whatsapp", "wa:")
    tg_id, tg_chat = await _contact(conn, "telegram")
    for contact_id, chat in ((wa_id, wa_chat), (tg_id, tg_chat)):
        await _said(conn, contact_id, chat, "assistant", "Depende do caso.", days_ago=2)
    automation_id = await _stalled_automation(conn)

    default = {p["contact_id"] for p in await repo.stalled_candidates(conn, automation_id, 1)}
    asked = {p["contact_id"] for p in await repo.stalled_candidates(
        conn, automation_id, 1, whatsapp=True)}

    assert wa_id not in default and tg_id in default
    assert {wa_id, tg_id} <= asked


async def test_an_approved_item_carries_when_it_was_queued(conn):
    contact_id, chat = await _contact(conn, "telegram")
    automation_id = await _stalled_automation(conn)
    queue_id = await repo.enqueue(conn, automation_id, contact_id, None, "approved", "Parou")
    assert queue_id is not None

    item = await repo.approved_for_send(conn, queue_id)

    assert item is not None and item["queued_at"] is not None


async def test_only_what_the_customer_wrote_counts(conn):
    contact_id, chat = await _contact(conn, "telegram")
    automation_id = await _stalled_automation(conn)
    queue_id = await repo.enqueue(conn, automation_id, contact_id, None, "approved", "Parou")
    item = await repo.approved_for_send(conn, queue_id or 0)
    assert item is not None
    queued_at = item["queued_at"]

    await _said(conn, contact_id, chat, "assistant", "Alguma dúvida?")
    assert await repo.wrote_since(conn, contact_id, queued_at - timedelta(seconds=1)) is False

    await _said(conn, contact_id, chat, "user", "não quero mais")
    assert await repo.wrote_since(conn, contact_id, queued_at - timedelta(seconds=1)) is True
    assert await repo.wrote_since(conn, contact_id, queued_at + timedelta(days=1)) is False


async def _done_meeting(conn, *, test: bool) -> int:
    """A contact at "agendado" whose meeting happened three days ago."""
    contact_id, _ = await _contact(conn, "whatsapp", "wa:")
    await conn.execute("UPDATE contacts SET stage = 'agendado', test_mode = %s WHERE id = %s",
                       ("novo" if test else None, contact_id))
    title = write(field("appointments", "title"), "Conversa")
    await conn.execute(
        "INSERT INTO appointments (contact_id, calendar_id, provider, provider_event_id, title, "
        "title_sealed, starts_at, ends_at, status, test_mode) VALUES (%s, 'c', 'google', %s, %s, "
        "%s, NOW() - INTERVAL '3 days', NOW() - INTERVAL '3 days' + INTERVAL '30 minutes', "
        "'done', %s)",
        (contact_id, f"ev-{uuid.uuid4().hex[:8]}", title["title"], title["title_sealed"], test))
    return contact_id


async def test_a_test_meeting_without_a_proposal_is_not_picked(conn):
    real = await _done_meeting(conn, test=False)
    tester = await _done_meeting(conn, test=True)
    cur = await conn.execute(
        "INSERT INTO automations (kind, enabled, mode, days, message) "
        "VALUES ('no_proposal', TRUE, 'approve', 1, 'Oi') "
        "ON CONFLICT (kind) DO UPDATE SET enabled = TRUE RETURNING id")
    automation_id = (await cur.fetchone())[0]

    picked = {p["contact_id"] for p in await repo.no_proposal_candidates(conn, automation_id, 1)}

    assert real in picked and tester not in picked
