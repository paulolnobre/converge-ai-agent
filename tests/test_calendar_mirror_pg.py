"""The calendar mirror against a real Postgres (TEST_DATABASE_URL): a window
replaced per calendar, sealed on disk, grouped by meeting for the panel, the
bot's own appointments left out, and old rows gone."""
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from core.integrations.workspace.models import Attendee, CalendarEvent
from core.repositories import calendar_mirror as mirror

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
WINDOW = (NOW - timedelta(days=14), NOW + timedelta(days=30))


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    await connection.execute("SELECT 1")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


def _event(event_id: str, *, hours: int = 24, title: str = "Reunião - Ellen Ribeiro",
           ical_uid: str | None = None, **extra) -> CalendarEvent:
    start = NOW + timedelta(hours=hours)
    return CalendarEvent(id=event_id, title=title, start=start, end=start + timedelta(minutes=30),
                         ical_uid=ical_uid, attendees=[Attendee(email="ellen@x.com", name="Ellen")],
                         meeting_link="https://meet.google.com/abc", **extra)


async def _staff(conn, name: str) -> int:
    cur = await conn.execute("INSERT INTO staff (name, email) VALUES (%s, %s) RETURNING id",
                             (name, f"{uuid.uuid4().hex[:8]}@x.com"))
    return (await cur.fetchone())[0]


async def _replace(conn, calendar: str, events, staff_id=None):
    return await mirror.replace_window(conn, calendar, "google", staff_id, *WINDOW, events)


async def _ids(conn, calendar: str) -> set[str]:
    cur = await conn.execute(
        "SELECT provider_event_id FROM calendar_events WHERE calendar_id = %s", (calendar,))
    return {row[0] for row in await cur.fetchall()}


async def test_replace_window_inserts_updates_and_drops_what_did_not_come(conn):
    assert await _replace(conn, "A", [_event("e1"), _event("e2")]) == 2
    assert await _replace(conn, "A", [_event("e1", hours=48)]) == 1
    assert await _ids(conn, "A") == {"e1"}
    found = await mirror.events_between(conn, *WINDOW)
    assert [item["starts_at"] for item in found] == [NOW + timedelta(hours=48)]


async def test_rows_of_another_calendar_are_untouched(conn):
    await _replace(conn, "A", [_event("e1")])
    await _replace(conn, "B", [_event("e9")])
    await _replace(conn, "A", [])
    assert await _ids(conn, "A") == set() and await _ids(conn, "B") == {"e9"}


async def test_rows_outside_the_window_are_not_dropped(conn):
    await _replace(conn, "A", [_event("far", hours=24 * 40)])  # beyond +30 days: never listed
    await _replace(conn, "A", [])
    assert await _ids(conn, "A") == {"far"}


async def test_title_attendees_and_link_are_sealed_on_disk(conn):
    await _replace(conn, "A", [_event("e1")])
    cur = await conn.execute("SELECT title, title_sealed, attendees, attendees_sealed, "
                             "meeting_url, meeting_url_sealed FROM calendar_events")
    title, title_sealed, attendees, attendees_sealed, url, url_sealed = await cur.fetchone()
    assert (title, attendees, url) == (None, None, None)
    assert b"Ellen" not in bytes(title_sealed) and attendees_sealed and url_sealed
    item, = await mirror.events_between(conn, *WINDOW)
    assert item["title"] == "Reunião - Ellen Ribeiro"


async def test_an_event_already_booked_by_the_bot_is_left_out(conn):
    await _replace(conn, "A", [_event("bot-1"), _event("hand-1", hours=30)])
    await conn.execute(
        "INSERT INTO appointments (calendar_id, provider, provider_event_id, title, title_sealed, "
        "starts_at, ends_at, status) VALUES ('A', 'google', 'bot-1', '', '\\x00', %s, %s, 'confirmed')",
        (NOW + timedelta(hours=24), NOW + timedelta(hours=24, minutes=30)))
    assert [item["title"] for item in await mirror.events_between(conn, *WINDOW)] == [
        "Reunião - Ellen Ribeiro"]
    assert len(await mirror.events_between(conn, *WINDOW)) == 1


async def test_the_same_invite_in_two_calendars_is_one_item_with_both_people(conn):
    paulo, rodrigo = await _staff(conn, "Paulo"), await _staff(conn, "Rodrigo")
    await _replace(conn, "A", [_event("x1", ical_uid="uid-1")], staff_id=paulo)
    await _replace(conn, "B", [_event("x2", ical_uid="uid-1")], staff_id=rodrigo)
    item, = await mirror.events_between(conn, *WINDOW)
    assert item["staff_ids"] == sorted([paulo, rodrigo])
    assert item["key"] == mirror.event_key(_event("x1", ical_uid="uid-1"), "A")


async def test_the_reception_calendar_has_no_owner(conn):
    await _replace(conn, "geral", [_event("g1")])
    item, = await mirror.events_between(conn, *WINDOW)
    assert item["staff_ids"] == [None]


async def test_all_day_and_status_reach_the_panel(conn):
    await _replace(conn, "A", [_event("d1", all_day=True, status="tentative")])
    item, = await mirror.events_between(conn, *WINDOW)
    assert item["all_day"] is True and item["status"] == "tentative"


async def test_purge_old_drops_events_ended_more_than_14_days_ago(conn):
    await _replace(conn, "A", [_event("old", hours=-24 * 13), _event("new")])
    assert await mirror.purge_old(conn, NOW + timedelta(days=2)) == 1
    assert await _ids(conn, "A") == {"new"}


async def test_forget_calendars_except_drops_mirror_and_state(conn):
    await _replace(conn, "A", [_event("e1")])
    await _replace(conn, "B", [_event("e2")])
    await mirror.record_attempt(conn, "B")
    await mirror.forget_calendars_except(conn, ["A"])
    assert await _ids(conn, "B") == set() and await _ids(conn, "A") == {"e1"}
    assert all(s["calendar_id"] != "B" for s in await mirror.sync_states(conn))


async def test_sync_state_records_attempt_success_and_error(conn):
    await mirror.record_attempt(conn, "A")
    await mirror.record_error(conn, "A", "provider_error")
    state, = [s for s in await mirror.sync_states(conn) if s["calendar_id"] == "A"]
    assert state["last_error"] == "provider_error" and state["last_success_at"] is None
    assert state["last_error_at"] and state["last_attempt_finished_at"]
    await mirror.record_success(conn, "A")
    state, = [s for s in await mirror.sync_states(conn) if s["calendar_id"] == "A"]
    assert state["last_success_at"] is not None
    assert state["last_success_at"] >= state["last_error_at"]


def test_event_key_uses_ical_uid_and_occurrence_or_falls_back_to_the_copy():
    start = datetime(2026, 10, 23, 9, tzinfo=UTC)
    assert mirror.event_key(_event("e1", ical_uid="u", original_start=start), "A") == (
        f"u|{start.isoformat()}")
    assert mirror.event_key(_event("e1", ical_uid="u"), "A") == "u|"
    assert mirror.event_key(_event("e1"), "A") == "A|e1"


@pytest.mark.parametrize("status", ["done", "no_show"])
async def test_a_past_bot_meeting_with_an_outcome_is_still_left_out(conn, status):
    await _replace(conn, "A", [_event("bot-2", hours=-24)])
    await conn.execute(
        "INSERT INTO appointments (calendar_id, provider, provider_event_id, title, title_sealed, "
        "starts_at, ends_at, status) VALUES ('A', 'google', 'bot-2', '', '\\x00', %s, %s, %s)",
        (NOW - timedelta(hours=24), NOW - timedelta(hours=23, minutes=30), status))
    assert await mirror.events_between(conn, *WINDOW) == []
