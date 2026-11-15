"""The calendar sync against a real Postgres (TEST_DATABASE_URL) and a fake
provider: every team calendar mirrored with its owner, a failing calendar left
as it was, one pass per calendar at a time, retention always, no title in a
log."""
import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from core.config import settings
from core.integrations import workspace
from core.integrations.workspace.errors import WorkspaceAuthError, WorkspaceError
from core.integrations.workspace.models import CalendarEvent
from core.models import StaffMember
from core.repositories import calendar_mirror as mirror
from core.services import calendar_sync

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def conn():
    """Autocommit: a pass runs its own transactions and session locks."""
    connection = await psycopg.AsyncConnection.connect(URL or "", autocommit=True)
    await connection.execute("DELETE FROM calendar_events; DELETE FROM calendar_sync_state")
    try:
        yield connection
    finally:
        await connection.execute("DELETE FROM calendar_events; DELETE FROM calendar_sync_state")
        await connection.execute("DELETE FROM staff WHERE email LIKE 'sync-test-%'")
        await connection.close()


def _event(event_id: str, title: str = "Reunião - Ellen Ribeiro", hours: int = 24) -> CalendarEvent:
    start = datetime.now(UTC) + timedelta(hours=hours)
    return CalendarEvent(id=event_id, title=title, start=start, end=start + timedelta(minutes=30))


class FakeCalendar:
    def __init__(self):
        self.by_calendar: dict[str, list[CalendarEvent] | Exception] = {}
        self.asked: list[str] = []

    async def list_events(self, calendar_id, start, end):
        self.asked.append(calendar_id)
        answer = self.by_calendar.get(calendar_id, [])
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def fake_calendar(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(workspace, "calendar", lambda: fake)
    monkeypatch.setattr(calendar_sync, "calendar_ready", lambda: True)
    monkeypatch.setattr(settings, "reception_calendar_id", "geral")
    monkeypatch.setattr(settings, "staff_calendar_ids", "")
    return fake


async def _staff(conn, name: str, calendar_id: str | None, active: bool = True) -> int:
    cur = await conn.execute(
        "INSERT INTO staff (name, email, calendar_id, active) VALUES (%s, %s, %s, %s) RETURNING id",
        (name, f"sync-test-{uuid.uuid4().hex[:8]}@x.com", calendar_id, active))
    return (await cur.fetchone())[0]


async def _rows(conn) -> dict[str, tuple]:
    cur = await conn.execute("SELECT provider_event_id, calendar_id, staff_id FROM calendar_events")
    return {row[0]: (row[1], row[2]) for row in await cur.fetchall()}


async def _state(conn, calendar_id: str) -> dict:
    return next(s for s in await mirror.sync_states(conn) if s["calendar_id"] == calendar_id)


async def test_each_calendar_is_mirrored_with_its_owner(conn, fake_calendar):
    paulo = await _staff(conn, "Paulo", "paulo@x.com")
    fake_calendar.by_calendar = {"geral": [_event("g1")], "paulo@x.com": [_event("p1")]}
    result = await calendar_sync.sync_once(conn)
    assert result == {"ok": 2, "error": 0, "skipped": 0}
    assert await _rows(conn) == {"g1": ("geral", None), "p1": ("paulo@x.com", paulo)}
    assert (await _state(conn, "geral"))["last_success_at"] is not None


async def test_a_failing_calendar_keeps_its_rows_and_records_the_error(conn, fake_calendar):
    await _staff(conn, "Paulo", "paulo@x.com")
    fake_calendar.by_calendar = {"geral": [_event("g1")], "paulo@x.com": [_event("p1")]}
    await calendar_sync.sync_once(conn)
    fake_calendar.by_calendar = {"geral": WorkspaceError("Google GET falhou (500)", status=500),
                                 "paulo@x.com": [_event("p2")]}
    result = await calendar_sync.sync_once(conn)
    assert result == {"ok": 1, "error": 1, "skipped": 0}
    assert set(await _rows(conn)) == {"g1", "p2"}
    state = await _state(conn, "geral")
    assert state["last_error"] == "provider_error" and state["last_error_at"] > state["last_success_at"]


async def test_no_access_has_its_own_code(conn, fake_calendar):
    fake_calendar.by_calendar = {"geral": WorkspaceAuthError("Google recusou", status=403)}
    await calendar_sync.sync_once(conn)
    assert (await _state(conn, "geral"))["last_error"] == "no_access"


async def test_a_calendar_already_being_synced_is_skipped(conn, fake_calendar):
    fake_calendar.by_calendar = {"geral": [_event("g1")]}
    holder = await psycopg.AsyncConnection.connect(URL or "", autocommit=True)
    try:
        await holder.execute("SELECT pg_advisory_lock(7411304, hashtext('geral'))")
        result = await calendar_sync.sync_once(conn)
        assert result == {"ok": 0, "error": 0, "skipped": 1}
        assert fake_calendar.asked == [] and await _rows(conn) == {}
    finally:
        await holder.close()


async def test_retention_runs_with_no_calendar_configured(conn, monkeypatch):
    monkeypatch.setattr(calendar_sync, "calendar_ready", lambda: False)
    old = datetime.now(UTC) - timedelta(days=20)
    await conn.execute(
        "INSERT INTO calendar_events (calendar_id, provider, provider_event_id, event_key, "
        "starts_at, ends_at, status) VALUES ('x', 'google', 'old', 'x|old', %s, %s, 'confirmed')",
        (old, old + timedelta(minutes=30)))
    await calendar_sync.sync_once(conn)
    assert await _rows(conn) == {}


async def test_a_deactivated_person_loses_her_calendar_rows(conn, fake_calendar):
    person = await _staff(conn, "Rodrigo", "rodrigo@x.com")
    fake_calendar.by_calendar = {"rodrigo@x.com": [_event("r1")]}
    await calendar_sync.sync_once(conn)
    await conn.execute("UPDATE staff SET active = FALSE WHERE id = %s", (person,))
    await calendar_sync.sync_once(conn)
    assert "r1" not in await _rows(conn)


def test_team_calendars_dedupes_reception_staff_and_setting(monkeypatch):
    monkeypatch.setattr(settings, "reception_calendar_id", "geral")
    monkeypatch.setattr(settings, "staff_calendar_ids", "paulo@x.com, extra@x.com, geral")
    staff = [StaffMember(id=1, name="Paulo", calendar_id="paulo@x.com"),
             StaffMember(id=2, name="Sem agenda"),
             StaffMember(id=3, name="Inativo", calendar_id="old@x.com", active=False)]
    assert calendar_sync.team_calendars(staff) == [
        ("geral", None), ("paulo@x.com", 1), ("extra@x.com", None)]


async def test_request_sync_wakes_the_worker_early(monkeypatch):
    runs: list[int] = []
    stop = asyncio.Event()

    async def fake_pass():
        runs.append(1)
        if len(runs) == 2:
            stop.set()

    monkeypatch.setattr(calendar_sync, "_one_pass", fake_pass)
    monkeypatch.setattr(settings, "calendar_sync_minutes", 15)
    task = asyncio.create_task(calendar_sync.run_calendar_sync_worker(stop))
    await asyncio.sleep(0.05)
    calendar_sync.request_sync()
    await asyncio.wait_for(task, timeout=2)
    assert len(runs) == 2


async def test_logs_carry_no_title(conn, fake_calendar, caplog):
    fake_calendar.by_calendar = {"geral": WorkspaceError("Google GET falhou (500)", status=500)}
    with caplog.at_level(logging.INFO):
        await calendar_sync.sync_once(conn)
    fake_calendar.by_calendar = {"geral": [_event("g1", title="Consulta sigilosa")]}
    with caplog.at_level(logging.INFO):
        await calendar_sync.sync_once(conn)
    assert "sigilosa" not in caplog.text and "geral" in caplog.text


async def test_an_unexpected_error_marks_that_calendar_and_the_pass_goes_on(conn, fake_calendar):
    await _staff(conn, "Paulo", "paulo@x.com")
    old = datetime.now(UTC) - timedelta(days=20)
    await conn.execute(
        "INSERT INTO calendar_events (calendar_id, provider, provider_event_id, event_key, "
        "starts_at, ends_at, status) VALUES ('geral', 'google', 'old', 'x|old', %s, %s, 'confirmed')",
        (old, old + timedelta(minutes=30)))
    fake_calendar.by_calendar = {"geral": ValueError("bad payload"), "paulo@x.com": [_event("p1")]}
    result = await calendar_sync.sync_once(conn)
    assert result == {"ok": 1, "error": 1, "skipped": 0}
    assert set(await _rows(conn)) == {"p1"}  # the other calendar synced, and retention ran
    state = await _state(conn, "geral")
    assert state["last_error"] == "unexpected" and state["last_attempt_finished_at"]


def test_the_worker_survives_a_second_event_loop(monkeypatch):
    """The wake-up event belongs to the loop the worker runs in, not to the
    first one that ever used the module: in a second loop the worker still
    waits between passes instead of spinning."""
    passes: list[int] = []

    async def fake_pass():
        passes.append(1)

    monkeypatch.setattr(calendar_sync, "_one_pass", fake_pass)
    monkeypatch.setattr(settings, "calendar_sync_minutes", 15)

    async def run_once():
        passes.clear()
        stop = asyncio.Event()
        task = asyncio.create_task(calendar_sync.run_calendar_sync_worker(stop))
        await asyncio.sleep(0.05)
        calendar_sync.request_sync()
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        assert task.exception() is None
        assert len(passes) <= 3  # one at start, one on the request; never a spin

    asyncio.run(run_once())
    asyncio.run(run_once())
