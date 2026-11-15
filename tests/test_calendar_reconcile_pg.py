"""The bot's meetings follow the calendar, against a real Postgres
(TEST_DATABASE_URL) and a fake provider: moved → new time and the reminder due
again; cancelled only with proof (Gone, or NotFound twice with good listings);
never a change to a row edited since it was read; done and no-show untouched."""
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from core.config import settings
from core.integrations import workspace
from core.integrations.workspace.errors import WorkspaceError
from core.integrations.workspace.models import CalendarEvent, Gone, NotFound
from core.repositories import appointments as appointments_repo
from core.repositories import contacts as contacts_repo
from core.sealed_fields import field, write
from core.services import calendar_reconcile, calendar_sync

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "", autocommit=True)
    try:
        yield connection
    finally:
        await connection.execute("DELETE FROM appointments WHERE calendar_id LIKE 'rc-%'")
        await connection.execute("DELETE FROM workspace_actions WHERE target LIKE 'rcev-%'")
        await connection.execute("DELETE FROM contacts WHERE chat_id LIKE 'rc%'")
        await connection.execute("DELETE FROM calendar_events WHERE calendar_id LIKE 'rc-%'")
        await connection.execute("DELETE FROM calendar_sync_state WHERE calendar_id LIKE 'rc-%'")
        await connection.close()


class FakeCalendar:
    def __init__(self):
        self.listing: dict[str, list[CalendarEvent] | Exception] = {}
        self.single: dict[str, object] = {}
        self.asked_single: list[str] = []

    async def list_events(self, calendar_id, start, end):
        answer = self.listing.get(calendar_id, [])
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def get_event(self, calendar_id, event_id):
        self.asked_single.append(event_id)
        answer = self.single.get(event_id, NotFound())
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def fake(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(workspace, "calendar", lambda: fake)
    return fake


def _cal() -> str:
    return f"rc-{uuid.uuid4().hex[:8]}"


async def _meeting(conn, calendar: str, *, hours: float = 24, status: str = "confirmed",
                   stage: str = "agendado", contact_id: int | None = None) -> appointments_repo.Appointment:
    if contact_id is None:
        contact = await contacts_repo.get_or_create(conn, f"rc{uuid.uuid4().hex[:10]}", name="Ana")
        contact_id = contact.id
        await conn.execute("UPDATE contacts SET stage = %s WHERE id = %s", (stage, contact_id))
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=hours)
    title = write(field("appointments", "title"), "Conversa")
    cur = await conn.execute(
        "INSERT INTO appointments (contact_id, calendar_id, provider, provider_event_id, title, "
        "title_sealed, starts_at, ends_at, status, provider_etag, reminder_sent_at) VALUES "
        "(%s, %s, 'google', %s, %s, %s, %s, %s, %s, '\"1\"', NOW()) RETURNING id",
        (contact_id, calendar, f"rcev-{uuid.uuid4().hex[:8]}", title["title"], title["title_sealed"],
         start, start + timedelta(minutes=30), status))
    appointment_id = (await cur.fetchone())[0]
    found = await appointments_repo.get_by_id(conn, appointment_id)
    assert found is not None
    return found


def _event(meeting, *, start=None, etag='"1"', all_day=False, organizer=True) -> CalendarEvent:
    start = start or meeting.starts_at
    return CalendarEvent(id=meeting.provider_event_id, title="Conversa", start=start,
                         end=start + (meeting.ends_at - meeting.starts_at), etag=etag,
                         all_day=all_day, organizer_is_self=organizer)


async def _reconcile(conn, calendar, meetings, listed, listing_ok=True) -> int:
    return await calendar_reconcile.reconcile_calendar(
        conn, calendar, "google", None if listed is None else {e.id: e for e in listed},
        meetings, listing_ok)


async def _reload(conn, meeting) -> appointments_repo.Appointment:
    assert meeting.id is not None
    found = await appointments_repo.get_by_id(conn, meeting.id)
    assert found is not None
    return found


async def _row(conn, appointment_id: int | None) -> dict:
    cur = await conn.execute(
        "SELECT a.starts_at, a.status, a.reminder_sent_at, a.provider_missing_at, a.sync_problem, "
        "a.provider_etag, a.moved_at, c.stage FROM appointments a JOIN contacts c ON c.id = a.contact_id "
        "WHERE a.id = %s", (appointment_id,))
    keys = ("starts_at", "status", "reminded", "missing", "problem", "etag", "moved_at", "stage")
    return dict(zip(keys, await cur.fetchone(), strict=True))


async def test_moved_in_google_updates_and_resets_the_reminder(conn, fake):
    cal = _cal()
    m = await _meeting(conn, cal)
    later = m.starts_at + timedelta(days=1)
    assert await _reconcile(conn, cal, [m], [_event(m, start=later, etag='"2"')]) == 0
    row = await _row(conn, m.id)
    assert (row["starts_at"], row["etag"], row["reminded"]) == (later, '"2"', None)
    assert row["moved_at"] is not None and row["status"] == "confirmed"
    cur = await conn.execute("SELECT action FROM workspace_actions WHERE target = %s",
                             (m.provider_event_id,))
    assert [r[0] for r in await cur.fetchall()] == ["sync_update"]


@pytest.mark.parametrize("already_moved", [False, True])
async def test_end_only_change_keeps_reminder_confirmation_and_buttons(conn, fake, already_moved):
    cal = _cal()
    m = await _meeting(conn, cal)
    await conn.execute(
        "UPDATE appointments SET attendance_confirmed_at = NOW(), outcome_asked_at = NOW(), "
        "moved_at = CASE WHEN %s THEN NOW() ELSE NULL END WHERE id = %s",
        (already_moved, m.id))
    m = await _reload(conn, m)
    markers_sql = ("SELECT reminder_sent_at, attendance_confirmed_at, outcome_asked_at, moved_at "
                   "FROM appointments WHERE id = %s")
    before = await (await conn.execute(markers_sql, (m.id,))).fetchone()
    changed = _event(m, etag='"duration"').model_copy(
        update={"end": m.ends_at + timedelta(minutes=30)})

    assert await _reconcile(conn, cal, [m], [changed]) == 0

    fresh = await _reload(conn, m)
    assert fresh.ends_at == changed.end and fresh.provider_etag == changed.etag
    assert fresh.starts_at == m.starts_at
    assert await (await conn.execute(markers_sql, (m.id,))).fetchone() == before
    assert m.id not in [a.id for a in await appointments_repo.due_reminders(conn, 24, 60)]


@pytest.mark.parametrize("left_calendar", [False, True])
async def test_old_meetings_are_not_fetched_or_cancelled(conn, fake, monkeypatch, left_calendar):
    cal = _cal()
    old = await _meeting(conn, cal, hours=-90 * 24)
    recent = await _meeting(conn, cal, hours=-13 * 24)
    future = await _meeting(conn, cal, hours=45 * 24)
    fake.single[old.provider_event_id] = Gone()
    fake.single[recent.provider_event_id] = _event(recent)
    fake.single[future.provider_event_id] = _event(future)
    monkeypatch.setattr(calendar_sync, "calendar_ready", lambda: True)
    monkeypatch.setattr(settings, "reception_calendar_id", _cal() if left_calendar else cal)
    monkeypatch.setattr(settings, "staff_calendar_ids", "")

    await calendar_sync.sync_once(conn)

    assert old.provider_event_id not in fake.asked_single
    assert recent.provider_event_id in fake.asked_single
    assert future.provider_event_id in fake.asked_single
    assert (await _row(conn, old.id))["status"] == "confirmed"
    assert (await _row(conn, old.id))["stage"] == "agendado"


async def test_calendar_with_only_old_meetings_is_not_a_target(conn):
    cal = _cal()
    await _meeting(conn, cal, hours=-90 * 24)
    assert await appointments_repo.sync_targets(conn, cal) == []
    assert cal not in await appointments_repo.calendars_with_targets(conn)


async def test_moved_beyond_the_window_is_found_by_get_event(conn, fake):
    cal = _cal()
    m = await _meeting(conn, cal)
    far = m.starts_at + timedelta(days=45)
    fake.single[m.provider_event_id] = _event(m, start=far, etag='"3"')
    assert await _reconcile(conn, cal, [m], []) == 0
    assert (await _row(conn, m.id))["starts_at"] == far
    assert fake.asked_single == [m.provider_event_id]


async def test_gone_cancels_and_steps_the_funnel_back(conn, fake):
    cal = _cal()
    m = await _meeting(conn, cal)
    fake.single[m.provider_event_id] = Gone()
    await _reconcile(conn, cal, [m], [])
    row = await _row(conn, m.id)
    assert (row["status"], row["stage"]) == ("cancelled", "respondido")


async def test_gone_with_another_upcoming_keeps_the_stage(conn, fake):
    cal = _cal()
    m = await _meeting(conn, cal)
    other = await _meeting(conn, cal, hours=48, contact_id=m.contact_id)
    fake.single[m.provider_event_id] = Gone()
    await _reconcile(conn, cal, [m, other], [_event(other)])
    assert (await _row(conn, m.id))["status"] == "cancelled"
    assert (await _row(conn, other.id))["stage"] == "agendado"


async def test_not_found_twice_with_good_listings_cancels(conn, fake):
    cal = _cal()
    m = await _meeting(conn, cal)
    await _reconcile(conn, cal, [m], [])
    first = await _row(conn, m.id)
    assert first["status"] == "confirmed" and first["missing"] is not None
    m = await _reload(conn, m)
    await _reconcile(conn, cal, [m], [])
    assert (await _row(conn, m.id))["status"] == "cancelled"


async def test_not_found_then_back_cancels_nothing(conn, fake):
    cal = _cal()
    m = await _meeting(conn, cal)
    await _reconcile(conn, cal, [m], [])
    m = await _reload(conn, m)
    await _reconcile(conn, cal, [m], [_event(m)])
    row = await _row(conn, m.id)
    assert row["status"] == "confirmed" and row["missing"] is None


async def test_not_found_when_the_listing_failed_never_counts(conn, fake):
    cal = _cal()
    m = await _meeting(conn, cal)
    for _ in range(2):
        m = await _reload(conn, m)
        await _reconcile(conn, cal, [m], None, listing_ok=False)
    row = await _row(conn, m.id)
    assert row["status"] == "confirmed" and row["missing"] is None


async def test_a_get_event_error_changes_nothing_and_counts(conn, fake):
    cal = _cal()
    m = await _meeting(conn, cal)
    fake.single[m.provider_event_id] = WorkspaceError("falhou (500)", status=500)
    assert await _reconcile(conn, cal, [m], []) == 1
    row = await _row(conn, m.id)
    assert row["status"] == "confirmed" and row["missing"] is None


async def test_changed_locally_after_the_snapshot_is_left_for_next_pass(conn, fake):
    cal = _cal()
    m = await _meeting(conn, cal)
    moved_by_panel = m.starts_at + timedelta(hours=2)
    await conn.execute("UPDATE appointments SET starts_at = %s, ends_at = %s WHERE id = %s",
                       (moved_by_panel, moved_by_panel + timedelta(minutes=30), m.id))
    await _reconcile(conn, cal, [m], [_event(m, start=m.starts_at + timedelta(days=1))])
    assert (await _row(conn, m.id))["starts_at"] == moved_by_panel


async def test_all_day_and_not_organizer_become_sync_problem_and_clear(conn, fake):
    cal = _cal()
    m = await _meeting(conn, cal)
    await _reconcile(conn, cal, [m], [_event(m, all_day=True)])
    assert (await _row(conn, m.id))["problem"] == "all_day"
    m = await _reload(conn, m)
    await _reconcile(conn, cal, [m], [_event(m, organizer=False)])
    assert (await _row(conn, m.id))["problem"] == "not_organizer"
    m = await _reload(conn, m)
    await _reconcile(conn, cal, [m], [_event(m)])
    row = await _row(conn, m.id)
    assert row["problem"] is None and row["starts_at"] == m.starts_at


async def test_start_collision_and_its_clearing(conn, fake):
    cal = _cal()
    m = await _meeting(conn, cal)
    blocker = await _meeting(conn, cal, hours=30)
    assert await _reconcile(conn, cal, [m], [_event(m, start=blocker.starts_at)]) == 1
    row = await _row(conn, m.id)
    assert row["problem"] == "start_collision" and row["starts_at"] == m.starts_at
    m = await _reload(conn, m)
    assert await _reconcile(conn, cal, [m], [_event(m, etag='"7"')]) == 0  # back to its time
    assert (await _row(conn, m.id))["problem"] is None


async def test_done_and_no_show_are_never_touched(conn, fake):
    cal = _cal()
    for status in ("done", "no_show"):
        m = await _meeting(conn, cal, hours=-5 - (status == "no_show"), status=status)
        fake.single[m.provider_event_id] = Gone()
        await _reconcile(conn, cal, [m], [])
        assert (await _row(conn, m.id))["status"] == status


async def test_moved_after_outcome_asked_is_reconciled(conn, fake):
    cal = _cal()
    m = await _meeting(conn, cal, hours=-3)
    await conn.execute("UPDATE appointments SET outcome_asked_at = NOW() WHERE id = %s", (m.id,))
    tomorrow = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    targets = await appointments_repo.sync_targets(conn, cal)
    assert [t.id for t in targets] == [m.id]
    await _reconcile(conn, cal, targets, [_event(m, start=tomorrow)])
    cur = await conn.execute("SELECT starts_at, outcome_asked_at FROM appointments WHERE id = %s",
                             (m.id,))
    starts_at, asked = await cur.fetchone()
    assert starts_at == tomorrow and asked is None


async def test_a_pass_reconciles_and_success_needs_no_errors(conn, fake, monkeypatch):
    cal = _cal()
    m = await _meeting(conn, cal)
    monkeypatch.setattr(calendar_sync, "calendar_ready", lambda: True)
    monkeypatch.setattr(settings, "reception_calendar_id", cal)
    monkeypatch.setattr(settings, "staff_calendar_ids", "")
    later = m.starts_at + timedelta(days=2)
    fake.listing[cal] = [_event(m, start=later)]
    assert (await calendar_sync.sync_once(conn))["ok"] == 1
    assert (await _row(conn, m.id))["starts_at"] == later
    m = await _reload(conn, m)
    fake.listing[cal] = []
    fake.single[m.provider_event_id] = WorkspaceError("falhou (500)", status=500)
    assert (await calendar_sync.sync_once(conn))["error"] == 1
    cur = await conn.execute("SELECT last_error FROM calendar_sync_state WHERE calendar_id = %s", (cal,))
    assert (await cur.fetchone())[0] == "reconcile_error"


async def test_a_calendar_out_of_the_set_is_checked_by_get_event(conn, fake, monkeypatch):
    cal, gone_cal = _cal(), _cal()
    m = await _meeting(conn, gone_cal)
    monkeypatch.setattr(calendar_sync, "calendar_ready", lambda: True)
    monkeypatch.setattr(settings, "reception_calendar_id", cal)
    monkeypatch.setattr(settings, "staff_calendar_ids", "")
    moved = m.starts_at + timedelta(days=1)
    fake.single[m.provider_event_id] = _event(m, start=moved)
    await calendar_sync.sync_once(conn)
    assert (await _row(conn, m.id))["starts_at"] == moved
    fake.single[m.provider_event_id] = NotFound()
    for _ in range(2):
        await calendar_sync.sync_once(conn)
    assert (await _row(conn, m.id))["status"] == "confirmed"  # NotFound never counts there
