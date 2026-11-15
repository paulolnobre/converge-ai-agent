"""The reminder, the "como foi?" and the bot's cancellation act only on the
meeting as it is now, against a real Postgres (TEST_DATABASE_URL): a meeting
moved by the calendar sync between the selection and the send gets no
reminder at the old time, and the new one goes at the new time."""
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from core.config import settings
from core.integrations import telegram
from core.repositories import appointments as appointments_repo
from core.repositories import contacts as contacts_repo
from core.sealed_fields import field, write
from core.services import appointment_taps as taps
from core.services import attendance, reminders, scheduling

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


@pytest.fixture
def sent(monkeypatch):
    out: list = []

    async def send_message(chat_id, text, **kwargs):
        out.append(("tg", chat_id, text))
        return True

    async def post_message(chat_id, text, **kwargs):
        out.append(("team", chat_id, text))
        return 1

    monkeypatch.setattr(telegram, "send_message", send_message)
    monkeypatch.setattr(telegram, "post_message", post_message)
    monkeypatch.setattr(settings, "team_chat_id", "-100")
    return out


async def _meeting(conn, *, hours: float, contact_id: int | None = None):
    if contact_id is None:
        contact = await contacts_repo.get_or_create(conn, str(7 * 10**9 + uuid.uuid4().int % 10**9),
                                                    name="Ana")
        contact_id = contact.id
        await conn.execute("UPDATE contacts SET stage = 'agendado' WHERE id = %s", (contact_id,))
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=hours)
    title = write(field("appointments", "title"), "Conversa")
    cur = await conn.execute(
        "INSERT INTO appointments (contact_id, calendar_id, provider, provider_event_id, title, "
        "title_sealed, starts_at, ends_at, status) VALUES (%s, %s, 'google', %s, %s, %s, %s, %s, "
        "'confirmed') RETURNING id",
        (contact_id, f"rm-{uuid.uuid4().hex[:6]}", f"ev-{uuid.uuid4().hex[:8]}", title["title"],
         title["title_sealed"], start, start + timedelta(minutes=30)))
    found = await appointments_repo.get_by_id(conn, (await cur.fetchone())[0])
    assert found is not None and found.id is not None
    return found


async def _move(conn, appointment, to: datetime):
    await conn.execute("UPDATE appointments SET starts_at = %s, ends_at = %s, moved_at = NOW() "
                       "WHERE id = %s", (to, to + timedelta(minutes=30), appointment.id))


@pytest.fixture
async def worker_meeting():
    """A committed meeting and a fresh non-autocommit connection, as in the worker.

    The rollback-only conn fixture hides implicit transactions and cannot prove
    that a claim survives a failure later in the sweep.
    """
    async with await psycopg.AsyncConnection.connect(URL or "") as connection:
        meeting = await _meeting(connection, hours=-3)
        await connection.commit()
        try:
            yield connection, meeting
        finally:
            await connection.rollback()
            await connection.execute("DELETE FROM appointments WHERE id = %s", (meeting.id,))
            await connection.execute("DELETE FROM contacts WHERE id = %s", (meeting.contact_id,))


async def test_empty_reminder_sweep_leaves_worker_connection_idle(worker_meeting, sent):
    worker, _ = worker_meeting
    assert await reminders.send_due(worker) == 0
    assert worker.info.transaction_status == TransactionStatus.IDLE


async def test_a_test_meeting_is_never_asked_about_in_the_group(worker_meeting, sent):
    worker, meeting = worker_meeting
    await worker.execute("UPDATE appointments SET test_mode = TRUE WHERE id = %s", (meeting.id,))
    await worker.commit()

    assert await attendance.ask_outcomes(worker) == 0
    assert sent == []
    cur = await worker.execute("SELECT outcome_asked_at FROM appointments WHERE id = %s",
                               (meeting.id,))
    row = await cur.fetchone()
    assert row is not None and row[0] is None


async def test_outcome_claim_and_locks_end_before_the_rest_of_the_sweep(worker_meeting, sent):
    worker, meeting = worker_meeting
    assert await reminders.send_due(worker) == 0
    assert await attendance.ask_outcomes(worker) == 1

    async with await psycopg.AsyncConnection.connect(URL or "", autocommit=True) as observer:
        async with observer.transaction():
            cur = await observer.execute("SELECT pg_try_advisory_xact_lock(7411301, %s)",
                                         (meeting.contact_id,))
            row = await cur.fetchone()
            assert row is not None and row[0], "the contact lock outlived its question"
            cur = await observer.execute("SELECT pg_try_advisory_xact_lock(7411303, hashtext(%s))",
                                         (f"{meeting.calendar_id}:{meeting.provider_event_id}",))
            row = await cur.fetchone()
            assert row is not None and row[0], "the event lock outlived its question"
        # A later operation fails: the already-sent question must stay claimed.
        await worker.rollback()
        cur = await observer.execute("SELECT outcome_asked_at FROM appointments WHERE id = %s",
                                     (meeting.id,))
        row = await cur.fetchone()
        assert row is not None and row[0] is not None
    assert worker.info.transaction_status == TransactionStatus.IDLE
    assert len(sent) == 1


async def test_a_reminder_for_a_meeting_moved_meanwhile_is_not_sent(conn, sent):
    selected = await _meeting(conn, hours=1)
    await _move(conn, selected, selected.starts_at + timedelta(days=1))
    assert await reminders._remind(conn, selected) == 0
    assert sent == []
    cur = await conn.execute("SELECT reminder_sent_at FROM appointments WHERE id = %s", (selected.id,))
    assert (await cur.fetchone())[0] is None


async def test_the_reminder_goes_with_the_new_time_after_a_move(conn, sent):
    selected = await _meeting(conn, hours=1)
    tomorrow = selected.starts_at + timedelta(days=1)
    await _move(conn, selected, tomorrow)
    fresh = await appointments_repo.get_by_id(conn, selected.id or 0)
    assert fresh is not None and await reminders._remind(conn, fresh) == 1
    (_, _, text), = sent
    assert scheduling.when_parts(scheduling.TimeSlot(start=tomorrow, end=tomorrow))["horario"] in text


async def test_an_outcome_question_for_a_meeting_moved_to_tomorrow_is_not_asked(conn, sent,
                                                                                monkeypatch):
    ended = await _meeting(conn, hours=-3)
    await _move(conn, ended, datetime.now(UTC) + timedelta(days=1))

    async def due_outcomes(conn, limit=50):
        return [ended]  # read before the sync moved it

    monkeypatch.setattr(attendance.appointments_repo, "due_outcomes", due_outcomes)
    assert await attendance.ask_outcomes(conn) == 0
    assert sent == []
    cur = await conn.execute("SELECT outcome_asked_at FROM appointments WHERE id = %s", (ended.id,))
    assert (await cur.fetchone())[0] is None


async def test_bot_cancel_keeps_the_stage_when_another_meeting_remains(conn, sent, monkeypatch):
    first = await _meeting(conn, hours=24)
    await _meeting(conn, hours=48, contact_id=first.contact_id)
    contact = await contacts_repo.get_by_id(conn, first.contact_id or 0)
    assert contact is not None

    async def cancel(conn, appointment):
        return await appointments_repo.cancel(conn, appointment.id)

    monkeypatch.setattr(scheduling, "cancel", cancel)
    await attendance.handle_tap(conn, contact, taps.tap("cancel_yes", first))
    cur = await conn.execute("SELECT a.status, c.stage FROM appointments a JOIN contacts c "
                             "ON c.id = a.contact_id WHERE a.id = %s", (first.id,))
    assert await cur.fetchone() == ("cancelled", "agendado")


async def test_bot_cancel_of_the_only_meeting_steps_back(conn, sent, monkeypatch):
    only = await _meeting(conn, hours=24)
    contact = await contacts_repo.get_by_id(conn, only.contact_id or 0)
    assert contact is not None

    async def cancel(conn, appointment):
        return await appointments_repo.cancel(conn, appointment.id)

    monkeypatch.setattr(scheduling, "cancel", cancel)
    await attendance.handle_tap(conn, contact, taps.tap("cancel_yes", only))
    cur = await conn.execute("SELECT stage FROM contacts WHERE id = %s", (only.contact_id,))
    assert (await cur.fetchone())[0] == "respondido"


async def test_just_booked_only_while_nothing_was_said_since(conn):
    fresh = await _meeting(conn, hours=24)
    found = await appointments_repo.just_booked(conn, fresh.contact_id or 0)
    assert found is not None and found.id == fresh.id
    # The confirmation is written with the booking: still "just booked".
    await conn.execute("INSERT INTO sessions (contact_id, chat_id, role, message) "
                       "SELECT %s, chat_id, 'assistant', '' FROM contacts WHERE id = %s",
                       (fresh.contact_id, fresh.contact_id))
    assert await appointments_repo.just_booked(conn, fresh.contact_id or 0) is not None
    # Anything said a little later: the thank-you window is over.
    await conn.execute("INSERT INTO sessions (contact_id, chat_id, role, message, created_at) "
                       "SELECT %s, chat_id, 'assistant', '', NOW() + interval '1 minute' "
                       "FROM contacts WHERE id = %s", (fresh.contact_id, fresh.contact_id))
    assert await appointments_repo.just_booked(conn, fresh.contact_id or 0) is None


async def test_just_booked_is_recent_confirmed_and_ahead(conn):
    old = await _meeting(conn, hours=24)
    await conn.execute("UPDATE appointments SET created_at = NOW() - interval '3 hours' WHERE id = %s",
                       (old.id,))
    assert await appointments_repo.just_booked(conn, old.contact_id or 0) is None
    cancelled = await _meeting(conn, hours=24)
    await conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = %s", (cancelled.id,))
    assert await appointments_repo.just_booked(conn, cancelled.contact_id or 0) is None
    past = await _meeting(conn, hours=-1)
    assert await appointments_repo.just_booked(conn, past.contact_id or 0) is None
