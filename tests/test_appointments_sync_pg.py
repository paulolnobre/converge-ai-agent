"""Appointments that follow the calendar, against a real Postgres
(TEST_DATABASE_URL): every change only when the row is still what was read,
the reminder and the "como foi?" claimed only for the time that was read, and
a cancellation that steps the funnel back only when nothing else is booked."""
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from core.repositories import appointments as repo
from core.repositories import contacts as contacts_repo
from core.sealed_fields import field, write
from core.services import funnel

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
MIGRATION = Path(__file__).resolve().parent.parent / "infra/migrations/0013_appointments_sync.sql"


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
    contact = await contacts_repo.get_or_create(conn, f"9{uuid.uuid4().int % 10**9}", name="Ana")
    assert contact.id is not None
    return contact.id


async def _appt(conn, contact_id: int, hours: float = 24, status: str = "confirmed",
                etag: str | None = '"1"') -> repo.Appointment:
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=hours)
    title = write(field("appointments", "title"), "Conversa")
    cur = await conn.execute(
        "INSERT INTO appointments (contact_id, calendar_id, provider, provider_event_id, title, "
        "title_sealed, starts_at, ends_at, status, provider_etag) VALUES (%s, 'cal', 'google', %s, "
        "%s, %s, %s, %s, %s, %s) RETURNING id",
        (contact_id, f"ev-{uuid.uuid4().hex[:8]}", title["title"], title["title_sealed"], start,
         start + timedelta(minutes=30), status, etag))
    appointment_id = (await cur.fetchone())[0]
    cur = await conn.execute("SELECT starts_at, ends_at, status, provider_etag, calendar_id, "
                             "provider_event_id FROM appointments WHERE id = %s", (appointment_id,))
    s, e, st, et, cal, ev = await cur.fetchone()
    return repo.Appointment(id=appointment_id, contact_id=contact_id, calendar_id=cal,
                            provider="google", provider_event_id=ev, title="", starts_at=s,
                            ends_at=e, status=st, provider_etag=et)


def _id(a: repo.Appointment) -> int:
    assert a.id is not None
    return a.id


def _snapshot(a: repo.Appointment) -> tuple:
    return (a.starts_at, a.status, a.provider_etag)


async def _row(conn, appointment_id: int) -> dict:
    cur = await conn.execute(
        "SELECT starts_at, provider_etag, moved_at, reminder_sent_at, attendance_confirmed_at, "
        "outcome_asked_at, sync_problem, provider_missing_at, status FROM appointments WHERE id = %s",
        (appointment_id,))
    keys = ("starts_at", "etag", "moved_at", "reminded", "confirmed", "asked", "problem",
            "missing", "status")
    return dict(zip(keys, await cur.fetchone(), strict=True))


async def test_apply_move_needs_the_snapshot_and_resets_the_markers(conn):
    a = await _appt(conn, await _contact(conn))
    await conn.execute("UPDATE appointments SET reminder_sent_at = NOW(), attendance_confirmed_at = "
                       "NOW(), provider_missing_at = NOW() WHERE id = %s", (_id(a),))
    new = a.starts_at + timedelta(days=1)
    assert not await repo.apply_move(conn, _id(a), (a.starts_at, a.status, '"other"'), new,
                                     new + timedelta(minutes=30), '"2"')
    assert await repo.apply_move(conn, _id(a), _snapshot(a), new, new + timedelta(minutes=30), '"2"')
    row = await _row(conn, _id(a))
    assert row["starts_at"] == new and row["etag"] == '"2"' and row["moved_at"] is not None
    assert row["reminded"] is None and row["confirmed"] is None and row["missing"] is None


async def test_apply_move_to_the_past_keeps_outcome_asked(conn):
    a = await _appt(conn, await _contact(conn), hours=-3)
    await conn.execute("UPDATE appointments SET outcome_asked_at = NOW() WHERE id = %s", (_id(a),))
    earlier = a.starts_at - timedelta(hours=1)
    assert await repo.apply_move(conn, _id(a), _snapshot(a), earlier, earlier + timedelta(minutes=30),
                                 '"2"')
    assert (await _row(conn, _id(a)))["asked"] is not None


async def test_apply_move_to_the_future_resets_outcome_asked(conn):
    a = await _appt(conn, await _contact(conn), hours=-3)
    await conn.execute("UPDATE appointments SET outcome_asked_at = NOW() WHERE id = %s", (_id(a),))
    later = datetime.now(UTC) + timedelta(days=1)
    assert await repo.apply_move(conn, _id(a), _snapshot(a), later, later + timedelta(minutes=30), '"2"')
    assert (await _row(conn, _id(a)))["asked"] is None


async def test_apply_etag_clears_a_start_collision_and_missing(conn):
    a = await _appt(conn, await _contact(conn))
    await conn.execute("UPDATE appointments SET sync_problem = 'start_collision', "
                       "provider_missing_at = NOW() WHERE id = %s", (_id(a),))
    assert await repo.apply_etag(conn, _id(a), _snapshot(a), '"5"')
    row = await _row(conn, _id(a))
    assert row["etag"] == '"5"' and row["problem"] is None and row["missing"] is None


async def test_apply_etag_keeps_another_problem(conn):
    a = await _appt(conn, await _contact(conn))
    await conn.execute("UPDATE appointments SET sync_problem = 'all_day' WHERE id = %s", (_id(a),))
    assert await repo.apply_etag(conn, _id(a), _snapshot(a), '"5"')
    assert (await _row(conn, _id(a)))["problem"] == "all_day"


async def test_set_sync_problem_mark_missing_and_cancel_from_provider(conn):
    a = await _appt(conn, await _contact(conn))
    assert await repo.set_sync_problem(conn, _id(a), _snapshot(a), "all_day")
    assert (await _row(conn, _id(a)))["problem"] == "all_day"
    assert await repo.set_sync_problem(conn, _id(a), _snapshot(a), None)
    assert await repo.mark_missing(conn, _id(a), _snapshot(a))
    first = (await _row(conn, _id(a)))["missing"]
    assert await repo.mark_missing(conn, _id(a), _snapshot(a))
    assert (await _row(conn, _id(a)))["missing"] == first  # the first pass is kept
    assert await repo.cancel_from_provider(conn, _id(a), _snapshot(a))
    assert (await _row(conn, _id(a)))["status"] == "cancelled"
    assert not await repo.cancel_from_provider(conn, _id(a), _snapshot(a))


async def test_done_and_no_show_are_never_touched(conn):
    for status in ("done", "no_show"):
        a = await _appt(conn, await _contact(conn), hours=-5, status=status)
        assert not await repo.apply_move(conn, _id(a), _snapshot(a), a.starts_at + timedelta(days=1),
                                         a.ends_at + timedelta(days=1), '"2"')
        assert not await repo.cancel_from_provider(conn, _id(a), _snapshot(a))


async def test_sync_targets_are_the_confirmed_with_an_event(conn):
    contact = await _contact(conn)
    kept = await _appt(conn, contact)
    await _appt(conn, contact, status="cancelled")
    await _appt(conn, contact, hours=-5, status="done")
    ids = [_id(a) for a in await repo.sync_targets(conn, "cal")]
    assert _id(kept) in ids and len([i for i in ids if i in (_id(kept),)]) == 1
    assert "cal" in await repo.calendars_with_targets(conn)


async def test_claim_reminder_only_with_the_same_start(conn):
    a = await _appt(conn, await _contact(conn))
    assert await repo.claim_reminder(conn, _id(a), a.starts_at + timedelta(minutes=1)) is None
    claimed = await repo.claim_reminder(conn, _id(a), a.starts_at)
    assert claimed is not None and claimed.starts_at == a.starts_at
    assert await repo.claim_reminder(conn, _id(a), a.starts_at) is None  # once
    b = await _appt(conn, await _contact(conn), hours=26)
    await conn.execute("UPDATE appointments SET sync_problem = 'all_day' WHERE id = %s", (_id(b),))
    assert await repo.claim_reminder(conn, _id(b), b.starts_at) is None


async def test_claim_outcome_only_ended_unasked_same_start(conn):
    future = await _appt(conn, await _contact(conn))
    assert await repo.claim_outcome(conn, _id(future), future.starts_at) is None
    past = await _appt(conn, await _contact(conn), hours=-3)
    assert await repo.claim_outcome(conn, _id(past), past.starts_at - timedelta(hours=1)) is None
    assert await repo.claim_outcome(conn, _id(past), past.starts_at) is not None
    assert await repo.claim_outcome(conn, _id(past), past.starts_at) is None


async def test_due_reminders_skips_sync_problem(conn):
    a = await _appt(conn, await _contact(conn), hours=2)
    await conn.execute("UPDATE appointments SET sync_problem = 'not_organizer' WHERE id = %s", (_id(a),))
    assert _id(a) not in [x.id for x in await repo.due_reminders(conn, 24, 60, limit=1000)]


async def test_step_back_only_without_another_upcoming(conn):
    contact = await _contact(conn)
    await conn.execute("UPDATE contacts SET stage = 'agendado' WHERE id = %s", (contact,))
    await _appt(conn, contact)
    assert not await funnel.step_back_after_cancel(conn, contact)
    await conn.execute("UPDATE appointments SET status = 'cancelled' WHERE contact_id = %s", (contact,))
    assert await funnel.step_back_after_cancel(conn, contact)
    cur = await conn.execute("SELECT stage FROM contacts WHERE id = %s", (contact,))
    assert (await cur.fetchone())[0] == "respondido"


async def test_lock_event_takes_an_advisory_lock(conn):
    await repo.lock_event(conn, "cal", "ev-1")
    cur = await conn.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND "
                             "classid = 7411303 AND pid = pg_backend_pid()")
    assert (await cur.fetchone())[0] == 1


async def test_the_migration_runs_twice(conn):
    sql = MIGRATION.read_text()
    await conn.execute(sql)
    await conn.execute(sql)
