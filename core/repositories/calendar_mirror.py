"""The mirror of the team's calendars, for the panel's agenda.

Display only: nothing here feeds reminders, the funnel or the bot, which read
`appointments`. A calendar's rows are replaced window by window, and only from
a complete listing (the provider raises rather than return part of one).
Title, attendees and meeting link are sealed fields.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from psycopg.rows import dict_row

from core.integrations.workspace.models import CalendarEvent
from core.sealed_fields import field, open_dict, pair_sql, write

_TITLE = field("calendar_events", "title")
_ATTENDEES = field("calendar_events", "attendees")
_MEETING_URL = field("calendar_events", "meeting_url")
_KEEP_DAYS = 14


def event_key(event: CalendarEvent, calendar_id: str) -> str:
    """The same meeting across the calendars it was sent to: iCalUID and the
    occurrence's original start; without iCalUID, this copy alone."""
    if event.ical_uid:
        occurrence = event.original_start.isoformat() if event.original_start else ""
        return f"{event.ical_uid}|{occurrence}"
    return f"{calendar_id}|{event.id}"


async def replace_window(conn, calendar_id: str, provider: str, staff_id: int | None,
                         start: datetime, end: datetime, events: list[CalendarEvent]) -> int:
    """Upsert every event of one calendar's window and drop that calendar's
    rows starting in the window that did not come back. Returns how many rows
    the window holds now."""
    seen: list[str] = []
    for event in events:
        if not event.id:
            continue
        seen.append(event.id)
        sealed = {
            **write(_TITLE, event.title),
            **write(_ATTENDEES, json.dumps([a.model_dump(include={"email", "name"})
                                            for a in event.attendees], ensure_ascii=False)),
            **write(_MEETING_URL, event.meeting_link),
        }
        await conn.execute(
            "INSERT INTO calendar_events (calendar_id, provider, provider_event_id, event_key, "
            "staff_id, starts_at, ends_at, all_day, status, organizer_is_self, etag, "
            "title, title_sealed, attendees, attendees_sealed, meeting_url, meeting_url_sealed, "
            "synced_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, NOW()) "
            "ON CONFLICT (calendar_id, provider_event_id) DO UPDATE SET "
            "event_key = EXCLUDED.event_key, staff_id = EXCLUDED.staff_id, "
            "starts_at = EXCLUDED.starts_at, ends_at = EXCLUDED.ends_at, "
            "all_day = EXCLUDED.all_day, status = EXCLUDED.status, "
            "organizer_is_self = EXCLUDED.organizer_is_self, etag = EXCLUDED.etag, "
            "title_sealed = EXCLUDED.title_sealed, attendees_sealed = EXCLUDED.attendees_sealed, "
            "meeting_url_sealed = EXCLUDED.meeting_url_sealed, synced_at = NOW()",
            (calendar_id, provider, event.id, event_key(event, calendar_id), staff_id,
             event.start, event.end, event.all_day, event.status, event.organizer_is_self,
             event.etag, sealed["title"], sealed["title_sealed"], sealed["attendees"],
             sealed["attendees_sealed"], sealed["meeting_url"], sealed["meeting_url_sealed"]),
        )
    await conn.execute(
        "DELETE FROM calendar_events WHERE calendar_id = %s AND starts_at >= %s "
        "AND starts_at < %s AND NOT (provider_event_id = ANY(%s))",
        (calendar_id, start, end, seen),
    )
    cur = await conn.execute(
        "SELECT count(*) FROM calendar_events WHERE calendar_id = %s AND starts_at >= %s "
        "AND starts_at < %s", (calendar_id, start, end))
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def record_attempt(conn, calendar_id: str) -> None:
    await conn.execute(
        "INSERT INTO calendar_sync_state (calendar_id, last_attempt_at) VALUES (%s, NOW()) "
        "ON CONFLICT (calendar_id) DO UPDATE SET last_attempt_at = NOW()", (calendar_id,))


async def record_success(conn, calendar_id: str) -> None:
    await conn.execute(
        "INSERT INTO calendar_sync_state (calendar_id, last_attempt_finished_at, last_success_at) "
        "VALUES (%s, clock_timestamp(), clock_timestamp()) ON CONFLICT (calendar_id) DO UPDATE SET "
        "last_attempt_finished_at = clock_timestamp(), last_success_at = clock_timestamp()",
        (calendar_id,))


async def record_error(conn, calendar_id: str, code: str) -> None:
    """A short code only — never a title, an address or the provider's text."""
    await conn.execute(
        "INSERT INTO calendar_sync_state (calendar_id, last_attempt_finished_at, last_error, "
        "last_error_at) VALUES (%s, clock_timestamp(), %s, clock_timestamp()) "
        "ON CONFLICT (calendar_id) DO UPDATE SET last_attempt_finished_at = clock_timestamp(), "
        "last_error = EXCLUDED.last_error, last_error_at = clock_timestamp()",
        (calendar_id, code[:40]))


async def forget_calendars_except(conn, calendar_ids: list[str]) -> int:
    """Calendars no longer in the team's set leave the mirror and the state."""
    cur = await conn.execute(
        "DELETE FROM calendar_events WHERE NOT (calendar_id = ANY(%s))", (calendar_ids,))
    await conn.execute(
        "DELETE FROM calendar_sync_state WHERE NOT (calendar_id = ANY(%s))", (calendar_ids,))
    return cur.rowcount


async def purge_old(conn, now: datetime) -> int:
    cur = await conn.execute(
        "DELETE FROM calendar_events WHERE ends_at < %s", (now - timedelta(days=_KEEP_DAYS),))
    return cur.rowcount


async def events_between(conn, start: datetime, end: datetime) -> list[dict]:
    """The events that are not already an appointment (confirmed, done or
    no-show: the panel lists those itself), one item per meeting (the same
    invite in two people's calendars is one item, both people)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT e.id, e.event_key, e.staff_id, e.starts_at, e.ends_at, e.all_day, e.status, "
            f"{pair_sql('e', _TITLE)} FROM calendar_events e "
            f"WHERE e.starts_at < %s AND e.ends_at > %s AND NOT EXISTS ("
            f"SELECT 1 FROM appointments a WHERE a.provider_event_id = e.provider_event_id "
            f"AND a.status <> 'cancelled') ORDER BY e.starts_at, e.id",
            (end, start),
        )
        rows = [open_dict(row, [(_TITLE, "title")]) for row in await cur.fetchall()]
    items: dict[str, dict] = {}
    for row in rows:
        item = items.get(row["event_key"])
        if item is None:
            items[row["event_key"]] = {
                "key": row["event_key"], "starts_at": row["starts_at"], "ends_at": row["ends_at"],
                "all_day": row["all_day"], "status": row["status"], "title": row["title"],
                "staff_ids": [row["staff_id"]],
            }
        elif row["staff_id"] not in item["staff_ids"]:
            item["staff_ids"].append(row["staff_id"])
    for item in items.values():
        item["staff_ids"].sort(key=lambda value: (value is None, value or 0))
    return list(items.values())


async def sync_states(conn) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT calendar_id, last_attempt_at, last_attempt_finished_at, last_success_at, "
            "last_error, last_error_at FROM calendar_sync_state ORDER BY calendar_id")
        return list(await cur.fetchall())
