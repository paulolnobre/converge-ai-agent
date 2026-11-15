"""Proposal questionnaires against a real Postgres (TEST_DATABASE_URL): the
answers land sealed, a contact has one active questionnaire, and the team's
two notices are each marked once."""
import os
import uuid
from datetime import timedelta

import psycopg
import pytest

from core.models import ProposalBriefing
from core.repositories import proposal_briefings as repo

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


async def _contact(conn) -> int:
    cur = await conn.execute(
        "INSERT INTO contacts (chat_id, source) VALUES (%s, 'telegram') RETURNING id",
        (uuid.uuid4().hex[:12],))
    return (await cur.fetchone())[0]


async def _user(conn) -> int:
    cur = await conn.execute(
        "INSERT INTO panel_users (email, name) VALUES (%s, 'Equipe') RETURNING id",
        (f"{uuid.uuid4().hex[:8]}@x.com",))
    return (await cur.fetchone())[0]


async def _get(conn, briefing_id: int) -> ProposalBriefing:
    found = await repo.get(conn, briefing_id)
    assert found is not None
    return found


async def _open(conn, contact_id: int) -> ProposalBriefing:
    found = await repo.open_for(conn, contact_id)
    assert found is not None
    return found


async def _age(conn, briefing_id: int, hours: int = 25) -> None:
    await conn.execute(
        "UPDATE proposal_briefings SET updated_at = NOW() - make_interval(hours => %s) "
        "WHERE id = %s", (hours, briefing_id))


async def test_answers_are_sealed_at_rest(conn):
    contact_id = await _contact(conn)
    b = await repo.create(conn, contact_id, {"desafio": "agenda"}, ["quem"], "quem")
    cur = await conn.execute(
        "SELECT answers, answers_sealed FROM proposal_briefings WHERE id = %s", (b.id,))
    clear, sealed = await cur.fetchone()
    assert clear == "" and b"agenda" not in bytes(sealed)
    got = await _get(conn, b.id)
    assert got.answers == {"desafio": "agenda"}
    assert got.prefilled == ["quem"] and got.step == "quem" and got.status == "open"


async def test_one_active_briefing_per_contact(conn):
    contact_id = await _contact(conn)
    first = await repo.create(conn, contact_id, {}, [], "quem")
    with pytest.raises(psycopg.errors.UniqueViolation):
        async with conn.transaction():
            await repo.create(conn, contact_id, {}, [], "quem")
    # incomplete_sent is still active
    await repo.set_status(conn, first.id, "incomplete_sent")
    with pytest.raises(psycopg.errors.UniqueViolation):
        async with conn.transaction():
            await repo.create(conn, contact_id, {}, [], "quem")
    # a finished one frees the slot
    await repo.set_status(conn, first.id, "sent")
    assert (await repo.create(conn, contact_id, {}, [], "quem")).id != first.id


async def test_open_for_returns_open_and_incomplete_sent_only(conn):
    contact_id = await _contact(conn)
    assert await repo.open_for(conn, contact_id) is None
    b = await repo.create(conn, contact_id, {}, [], "quem")
    assert (await _open(conn, contact_id)).id == b.id
    await repo.set_status(conn, b.id, "incomplete_sent")
    assert (await _open(conn, contact_id)).id == b.id
    await repo.set_status(conn, b.id, "cancelled")
    assert await repo.open_for(conn, contact_id) is None


async def test_save_updates_answers_and_step(conn):
    b = await repo.create(conn, await _contact(conn), {}, [], "quem")
    await repo.save(conn, b.id, answers={"quem": "Ana"}, step="desafio")
    got = await _get(conn, b.id)
    assert got.answers == {"quem": "Ana"} and got.step == "desafio"


async def test_save_persists_prefilled_only_when_given(conn):
    b = await repo.create(conn, await _contact(conn), {"quem": "A", "desafio": "B"},
                          ["quem", "desafio"], "custo_hoje")
    await repo.save(conn, b.id, answers={"quem": "A"}, step="desafio")
    assert (await _get(conn, b.id)).prefilled == ["quem", "desafio"]
    await repo.save(conn, b.id, answers={"quem": "A"}, step="desafio", prefilled=["quem"])
    assert (await _get(conn, b.id)).prefilled == ["quem"]
    await repo.save(conn, b.id, answers={}, step="quem", prefilled=[])
    assert (await _get(conn, b.id)).prefilled == []


async def test_set_sent_records_submitted_at(conn):
    b = await repo.create(conn, await _contact(conn), {}, [], None)
    assert b.submitted_at is None
    await repo.set_status(conn, b.id, "sent")
    got = await _get(conn, b.id)
    assert got.status == "sent" and got.submitted_at is not None


async def test_abandoned_lists_only_old_open_unnotified(conn):
    b = await repo.create(conn, await _contact(conn), {}, [], "quem")
    assert b.id not in [x.id for x in await repo.abandoned(conn, timedelta(hours=24))]
    await _age(conn, b.id)
    assert b.id in [x.id for x in await repo.abandoned(conn, timedelta(hours=24))]
    assert await repo.mark_notified(conn, b.id, "incomplete") is True
    assert b.id not in [x.id for x in await repo.abandoned(conn, timedelta(hours=24))]


async def test_abandoned_skips_incomplete_sent(conn):
    b = await repo.create(conn, await _contact(conn), {}, [], "quem")
    await repo.set_status(conn, b.id, "incomplete_sent")
    await _age(conn, b.id)
    assert b.id not in [x.id for x in await repo.abandoned(conn, timedelta(hours=24))]


async def test_close_abandoned_only_closes_old_open_rows(conn):
    day = timedelta(hours=24)
    fresh = await repo.create(conn, await _contact(conn), {}, [], "quem")
    assert await repo.close_abandoned(conn, fresh.id, day) is False
    assert (await _get(conn, fresh.id)).status == "open"
    sent = await repo.create(conn, await _contact(conn), {}, [], "quem")
    await _age(conn, sent.id)
    await repo.set_status(conn, sent.id, "sent")
    await _age(conn, sent.id)
    assert await repo.close_abandoned(conn, sent.id, day) is False
    assert (await _get(conn, sent.id)).status == "sent"
    old = await repo.create(conn, await _contact(conn), {}, [], "quem")
    await _age(conn, old.id)
    assert await repo.close_abandoned(conn, old.id, day) is True
    assert (await _get(conn, old.id)).status == "incomplete_sent"
    assert await repo.close_abandoned(conn, old.id, day) is False


async def test_mark_notified_kinds_are_independent_and_once(conn):
    b = await repo.create(conn, await _contact(conn), {}, [], "quem")
    assert await repo.mark_notified(conn, b.id, "sent") is True
    assert await repo.mark_notified(conn, b.id, "sent") is False
    got = await _get(conn, b.id)
    assert got.notified_at is not None and got.incomplete_notified_at is None
    assert await repo.mark_notified(conn, b.id, "incomplete") is True
    assert await repo.mark_notified(conn, b.id, "incomplete") is False
    got = await _get(conn, b.id)
    assert got.notified_at is not None and got.incomplete_notified_at is not None


async def test_requests_skip_open_and_cancelled(conn):
    ids = {}
    for status in ("open", "sent", "incomplete_sent", "cancelled", "proposal_sent"):
        b = await repo.create(conn, await _contact(conn), {}, [], None)
        await repo.set_status(conn, b.id, status)
        ids[status] = b.id
    listed = {b.id for b in await repo.requests(conn)}
    assert {ids["sent"], ids["incomplete_sent"], ids["proposal_sent"]} <= listed
    assert not ({ids["open"], ids["cancelled"]} & listed)


async def test_mark_proposal_sent_records_who_and_when(conn):
    user_id = await _user(conn)
    sent = await repo.create(conn, await _contact(conn), {}, [], None)
    await repo.set_status(conn, sent.id, "sent")
    assert await repo.mark_proposal_sent(conn, sent.id, user_id) is True
    got = await _get(conn, sent.id)
    assert got.status == "proposal_sent" and got.proposal_sent_by == user_id
    assert got.proposal_sent_at is not None
    assert await repo.mark_proposal_sent(conn, sent.id, user_id) is False

    incomplete = await repo.create(conn, await _contact(conn), {}, [], None)
    await repo.set_status(conn, incomplete.id, "incomplete_sent")
    assert await repo.mark_proposal_sent(conn, incomplete.id, user_id) is True

    open_one = await repo.create(conn, await _contact(conn), {}, [], None)
    assert await repo.mark_proposal_sent(conn, open_one.id, user_id) is False


async def test_unmark_puts_the_request_back_as_it_was(conn):
    """A proposal the channel refused: the request waits again, with its
    status, and can be sent once more."""
    user_id = await _user(conn)
    b = await repo.create(conn, await _contact(conn), {}, [], None)
    await repo.set_status(conn, b.id, "incomplete_sent")
    assert await repo.mark_proposal_sent(conn, b.id, user_id) is True

    await repo.unmark_proposal_sent(conn, b.id, "incomplete_sent")

    got = await _get(conn, b.id)
    assert got.status == "incomplete_sent"
    assert got.proposal_sent_at is None and got.proposal_sent_by is None
    assert await repo.mark_proposal_sent(conn, b.id, user_id) is True


async def test_deleting_the_contact_removes_the_briefing(conn):
    contact_id = await _contact(conn)
    b = await repo.create(conn, contact_id, {}, [], None)
    await conn.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))
    assert await repo.get(conn, b.id) is None


async def test_deleting_the_panel_user_keeps_the_request_without_who(conn):
    """Like every other reference to panel_users: the request stays, the
    "who marked it" goes."""
    user_id = await _user(conn)
    b = await repo.create(conn, await _contact(conn), {}, [], None)
    await repo.set_status(conn, b.id, "sent")
    assert await repo.mark_proposal_sent(conn, b.id, user_id) is True

    await conn.execute("DELETE FROM panel_users WHERE id = %s", (user_id,))

    got = await _get(conn, b.id)
    assert got.proposal_sent_by is None
    assert got.status == "proposal_sent" and got.proposal_sent_at is not None


async def test_requests_leave_out_the_team_unless_in_test_mode(conn):
    """The same rule as the funnel and the Inbox: a staff chat is not a
    customer's request — unless that chat is in /teste."""
    staff_chat = uuid.uuid4().hex[:12]
    cur = await conn.execute(
        "INSERT INTO contacts (chat_id, source) VALUES (%s, 'telegram') RETURNING id",
        (staff_chat,))
    staff_contact = (await cur.fetchone())[0]
    await conn.execute("INSERT INTO staff (chat_id, name) VALUES (%s, 'Equipe')", (staff_chat,))
    customer = await _contact(conn)
    ids = {}
    for contact_id in (staff_contact, customer):
        b = await repo.create(conn, contact_id, {}, [], None)
        await repo.set_status(conn, b.id, "sent")
        ids[contact_id] = b.id

    listed = {b.id for b in await repo.requests(conn)}
    assert ids[customer] in listed and ids[staff_contact] not in listed

    await conn.execute("UPDATE contacts SET test_mode = 'novo' WHERE id = %s", (staff_contact,))
    listed = {b.id for b in await repo.requests(conn)}
    assert ids[staff_contact] in listed
