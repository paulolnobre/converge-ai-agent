"""Copying the team's calendars into the panel's mirror (calendar_events).

Display only: what is mirrored here never becomes an appointment, a reminder
or a funnel move. Each calendar is read whole (the provider raises rather than
return part of a listing) and replaced in one transaction; a calendar that
fails keeps what it had, and its state says so. Never two passes over the
same calendar at once, across processes. Logs carry the calendar id and a
short code — never a title or an attendee.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from core.config import settings
from core.integrations import workspace
from core.integrations.workspace.errors import WorkspaceAuthError, WorkspaceError
from core.metrics import CALENDAR_SYNC_LAST_SUCCESS, CALENDAR_SYNC_RUNS
from core.models import StaffMember
from core.repositories import appointments as appointments_repo
from core.repositories import calendar_mirror as mirror
from core.repositories import contacts as contacts_repo
from core.repositories.connection import get_pool
from core.services.calendar_reconcile import reconcile_calendar

logger = logging.getLogger(__name__)

_LOCK = 7411304
_BACK = timedelta(days=14)
_AHEAD = timedelta(days=30)

_wake: asyncio.Event | None = None  # made by the running worker, in its own loop
_requested_at: datetime | None = None


def calendar_ready() -> bool:
    """A calendar provider with credentials: without one, only retention runs."""
    report = workspace.configuration_report()
    provider = report["capabilities"]["calendar"]
    return report["providers"].get(provider) == "configured"


def team_calendars(staff: list[StaffMember]) -> list[tuple[str, int | None]]:
    """(calendar id, owner): the reception calendar, each active person's own,
    then STAFF_CALENDAR_IDS — once each, the first owner found winning."""
    found: dict[str, int | None] = {}
    if settings.reception_calendar_id:
        found[settings.reception_calendar_id] = None
    for person in staff:
        if person.active and person.calendar_id and person.calendar_id not in found:
            found[person.calendar_id] = person.id
    for value in settings.staff_calendar_ids.split(","):
        calendar_id = value.strip()
        if calendar_id and calendar_id not in found:
            found[calendar_id] = None
    return list(found.items())


async def _sync_calendar(conn, calendar_id: str, staff_id: int | None, provider: str,
                         start: datetime, end: datetime) -> str:
    async with conn.transaction():
        cur = await conn.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s))",
                                 (_LOCK, calendar_id))
        row = await cur.fetchone()
    if not (row and row[0]):
        logger.info("Calendar %s is being synced elsewhere; skipped", calendar_id)
        return "skipped"
    try:
        async with conn.transaction():
            await mirror.record_attempt(conn, calendar_id)
            # Read before asking the provider: a row changed after this (by the
            # panel or the bot) is left for the next pass, never overwritten.
            snapshot = await appointments_repo.sync_targets(conn, calendar_id)
        try:
            events = await workspace.calendar().list_events(calendar_id, start, end)
            async with conn.transaction():
                count = await mirror.replace_window(conn, calendar_id, provider, staff_id,
                                                    start, end, events)
            failed = await reconcile_calendar(conn, calendar_id, provider,
                                              {e.id: e for e in events if e.id}, snapshot, True)
            if failed:
                async with conn.transaction():
                    await mirror.record_error(conn, calendar_id, "reconcile_error")
                logger.warning("Calendar %s mirrored; %d appointment(s) not reconciled",
                               calendar_id, failed)
                return "error"
            async with conn.transaction():
                await mirror.record_success(conn, calendar_id)
        except Exception as exc:
            # One calendar failing never stops the others nor retention. Only
            # the type is logged: a provider's text could carry a title.
            if isinstance(exc, WorkspaceAuthError):
                code = "no_access"
            elif isinstance(exc, WorkspaceError):
                code = "provider_error"
            else:
                code = "unexpected"
            async with conn.transaction():
                await mirror.record_error(conn, calendar_id, code)
            logger.warning("Calendar %s could not be mirrored (%s, %s)", calendar_id, code,
                           type(exc).__name__)
            return "error"
        logger.info("Calendar %s mirrored: %d events", calendar_id, count)
        return "ok"
    finally:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", (_LOCK, calendar_id))


async def _reconcile_left_calendars(conn, provider: str, team: set[str]) -> None:
    """Appointments in a calendar no longer in the team's set (a person
    deactivated) are still followed, event by event. Without a listing,
    NotFound never counts."""
    async with conn.transaction():
        left = [cid for cid in await appointments_repo.calendars_with_targets(conn)
                if cid not in team]
    for calendar_id in left:
        async with conn.transaction():
            snapshot = await appointments_repo.sync_targets(conn, calendar_id)
        failed = await reconcile_calendar(conn, calendar_id, provider, None, snapshot, False)
        if failed:
            logger.warning("Calendar %s (out of the team): %d appointment(s) not reconciled",
                           calendar_id, failed)


async def sync_once(conn) -> dict[str, int]:
    """One pass over every team calendar, then retention — always."""
    counts = {"ok": 0, "error": 0, "skipped": 0}
    now = datetime.now(UTC)
    calendars: list[tuple[str, int | None]] | None = None
    try:
        if calendar_ready():
            async with conn.transaction():
                staff = await contacts_repo.active_staff(conn)
            calendars = team_calendars(staff)
            provider = workspace.configuration_report()["capabilities"]["calendar"]
            for calendar_id, staff_id in calendars:
                outcome = await _sync_calendar(conn, calendar_id, staff_id, provider,
                                               now - _BACK, now + _AHEAD)
                counts[outcome] += 1
            await _reconcile_left_calendars(conn, provider, {cid for cid, _ in calendars})
        else:
            calendars = []
    finally:
        # Retention is a privacy promise: it runs whatever happened above. The
        # set of calendars is only trusted when it was read.
        async with conn.transaction():
            if calendars is not None:
                await mirror.forget_calendars_except(conn, [cid for cid, _ in calendars])
            await mirror.purge_old(conn, now)
    return counts


def _result(counts: dict[str, int]) -> str:
    if not counts["ok"] and not counts["error"]:
        return "skipped"
    if not counts["error"]:
        return "ok"
    return "partial" if counts["ok"] else "error"


async def _one_pass() -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        counts = await sync_once(conn)
    result = _result(counts)
    CALENDAR_SYNC_RUNS.labels(result).inc()
    if result == "ok":
        CALENDAR_SYNC_LAST_SUCCESS.set(time.time())


def _wake_worker() -> None:
    if _wake is not None:
        _wake.set()


def request_sync() -> datetime:
    """The panel's "Atualizar agenda": a pass now instead of at the next tick."""
    global _requested_at
    _requested_at = datetime.now(UTC)
    _wake_worker()
    return _requested_at


def requested_at() -> datetime | None:
    return _requested_at


def sync_status(states: list[dict], calendars: list[tuple[str, int | None]],
                now: datetime, requested: datetime | None) -> dict:
    """What the panel shows about the mirror. Stale: a calendar never read
    well, read well too long ago (three ticks), or failing since its last
    success. Done: every calendar finished a pass after the panel's request."""
    by_id = {state["calendar_id"]: state for state in states}
    limit = timedelta(minutes=3 * settings.calendar_sync_minutes)
    rows, stale, done = [], False, True
    for calendar_id, staff_id in calendars:
        state = by_id.get(calendar_id, {})
        success, error_at = state.get("last_success_at"), state.get("last_error_at")
        finished = state.get("last_attempt_finished_at")
        late = success is None or now - success > limit or bool(error_at and error_at > success)
        stale = stale or late
        if requested is not None and (finished is None or finished <= requested):
            done = False
        rows.append({"calendar_id": calendar_id, "staff_id": staff_id, "last_success_at": success,
                     "last_error": state.get("last_error"), "last_error_at": error_at,
                     "stale": late})
    return {"calendars": rows, "stale": stale, "requested_at": requested, "done": done}


async def status(conn) -> dict:
    calendars: list[tuple[str, int | None]] = []
    if calendar_ready():
        calendars = team_calendars(await contacts_repo.active_staff(conn))
    return sync_status(await mirror.sync_states(conn), calendars, datetime.now(UTC), _requested_at)


async def _wait(wake: asyncio.Event, stop_event: asyncio.Event, seconds: float) -> None:
    """Until the next tick, a request from the panel, or shutdown."""
    waiters = [asyncio.ensure_future(wake.wait()), asyncio.ensure_future(stop_event.wait())]
    try:
        await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
    wake.clear()


async def run_calendar_sync_worker(stop_event: asyncio.Event) -> None:
    global _wake
    wake = _wake = asyncio.Event()
    while not stop_event.is_set():
        try:
            await _one_pass()
        except Exception:
            CALENDAR_SYNC_RUNS.labels("error").inc()
            logger.exception("Calendar sync pass failed")
        if stop_event.is_set():
            return
        try:
            await _wait(wake, stop_event, settings.calendar_sync_minutes * 60)
        except Exception:
            logger.exception("Calendar sync wait failed")
            await asyncio.sleep(settings.calendar_sync_minutes * 60)
