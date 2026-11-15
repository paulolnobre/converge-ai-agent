"""Errors in the log become admin alerts: one per kind per window, with the
count, the code's message template (never the values) and no customer data."""
import logging
from datetime import UTC, datetime, timedelta

import pytest

from core.config import settings
from core.services import error_alerts

T0 = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)   # 12:00 in Belém


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    error_alerts.reset()
    monkeypatch.setattr(settings, "error_alert_minutes", 15)
    monkeypatch.setattr(settings, "office_timezone", "America/Belem")
    yield
    error_alerts.reset()


def _record(msg="Could not send to %s", args=("wa:5591999990000",), name="core.channels",
            level=logging.ERROR, exc: type[BaseException] | None = None) -> logging.LogRecord:
    exc_info = None
    if exc is not None:
        try:
            raise exc("boom")
        except BaseException as caught:
            exc_info = (type(caught), caught, caught.__traceback__)
    return logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)


def test_the_same_error_is_one_kind_whatever_the_values():
    a = error_alerts.kind_of(_record(args=("wa:1",)))
    b = error_alerts.kind_of(_record(args=("wa:2",)))
    assert a == b
    assert "wa:" not in a[2], "the template, never the values"


def test_the_exception_type_and_the_place_make_another_kind():
    plain = error_alerts.kind_of(_record())
    assert error_alerts.kind_of(_record(exc=TimeoutError))[0] != plain[0]
    assert error_alerts.kind_of(_record(name="core.agent"))[0] != plain[0]
    assert error_alerts.kind_of(_record(exc=TimeoutError))[2].endswith("(TimeoutError)")


def test_one_alert_per_kind_per_window_with_the_count():
    for _ in range(3):
        error_alerts.note(_record(), T0)
    (key, seen), = error_alerts.take_due(T0)
    assert seen.count == 3
    assert "3 vezes desde 12:00" in error_alerts.text([seen])

    error_alerts.note(_record(), T0 + timedelta(minutes=5))
    assert error_alerts.take_due(T0 + timedelta(minutes=5)) == [], "inside the window"
    error_alerts.note(_record(), T0 + timedelta(minutes=10))
    (_, later), = error_alerts.take_due(T0 + timedelta(minutes=15))
    assert later.count == 2, "what happened meanwhile is counted, not lost"


def test_the_handler_counts_only_errors():
    handler = error_alerts.AlertHandler()
    log = logging.getLogger("alerts_test_only")
    log.propagate = False   # not through "core", which carries the bot's own handler
    log.addHandler(handler)
    try:
        log.warning("just a warning")
        log.error("a real error")
        log.exception("no exception active")
    finally:
        log.removeHandler(handler)
    due = error_alerts.take_due()
    assert sorted(seen.what for _, seen in due) == ["a real error", "no exception active"]


def test_the_alert_path_never_alerts_about_itself():
    error_alerts.AlertHandler().emit(_record(name=error_alerts.__name__))
    assert error_alerts.take_due() == []


def test_too_many_kinds_are_counted_together(monkeypatch):
    monkeypatch.setattr(error_alerts, "MAX_KINDS", 2)
    for i in range(5):
        error_alerts.note(_record(msg=f"kind {i}"), T0)
    due = dict(error_alerts.take_due(T0))
    assert len(due) == 3 and due["others"].count == 3


async def test_stored_alerts_go_to_the_admin_room_and_its_outbox(monkeypatch):
    from contextlib import asynccontextmanager

    from core.repositories import admin_room, connection

    rows: list[tuple] = []

    class Conn:
        async def execute(self, sql, params=None):
            rows.append(("outbox", params))

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield Conn()

    async def get_pool():
        return Pool()

    async def insert(conn, text, *, kind, event_key):
        rows.append(("room", text, kind, event_key))
        return 77

    monkeypatch.setattr(connection, "get_pool", get_pool)
    monkeypatch.setattr(admin_room, "insert", insert)
    error_alerts.note(_record(exc=TimeoutError), T0)

    assert await error_alerts.flush(T0) == 1

    room, outbox = rows
    assert room[2] == "alert" and room[3].startswith("errors:")
    assert "TimeoutError" in room[1] and "wa:" not in room[1]
    assert outbox[1] == (77, room[1], True), "no admin chat: app and admin room only"


async def test_a_failed_store_keeps_the_count_for_the_next_pass(monkeypatch):
    from core.repositories import connection

    async def broken():
        raise OSError("database gone")

    monkeypatch.setattr(connection, "get_pool", broken)
    error_alerts.note(_record(), T0)
    error_alerts.note(_record(), T0)

    assert await error_alerts.flush(T0) == 0

    (_, seen), = error_alerts.take_due(T0 + timedelta(seconds=60))
    assert seen.count == 2, "not stored: counted again, and due right away"


def test_the_bot_installs_the_handler():
    import core.web.app  # noqa: F401  (installs on import, as in production)

    for name in ("core", "uvicorn.error"):
        assert any(isinstance(h, error_alerts.AlertHandler)
                   for h in logging.getLogger(name).handlers), name


async def test_the_worker_stops_at_once_without_touching_the_database(monkeypatch):
    import asyncio

    async def refused(now=None):
        pytest.fail("no flush while shutting down")

    monkeypatch.setattr(error_alerts, "flush", refused)
    error_alerts.note(_record(), T0)
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(error_alerts.run_error_alert_worker(stop), 1)


def test_a_burst_is_one_alert_listing_the_worst_first(monkeypatch):
    monkeypatch.setattr(error_alerts, "_LINES", 2)
    for i, times in enumerate((1, 5, 3, 2)):
        for _ in range(times):
            error_alerts.note(_record(msg=f"kind {i}"), T0)
    body = error_alerts.text([seen for _, seen in error_alerts.take_due(T0)])
    assert body.startswith("⚠️ 4 tipos de erro no agente")
    assert body.index("kind 1") < body.index("kind 2")
    assert "kind 0" not in body and "e mais 2 tipos (3 erros)" in body


@pytest.mark.parametrize("char", ["é", "🔥", "x"])
def test_an_alert_stays_small_enough_for_a_push(char):
    for i in range(20):
        error_alerts.note(_record(msg=f"kind {i} " + char * 400, name="core." + char * 200), T0)
    import json

    body = error_alerts.text([seen for _, seen in error_alerts.take_due(T0)])
    payload = json.dumps({"title": body, "url": "/painel/#admin", "tag": "alert:1"}).encode()
    assert len(payload) < 3500, "as core.services.push sends it, inside the 4 KB limit"


async def test_with_an_admin_chat_the_alert_also_goes_to_telegram(monkeypatch):
    from contextlib import asynccontextmanager

    from core.repositories import admin_room, connection

    params: list = []

    class Conn:
        async def execute(self, sql, values=None):
            params.append(values)

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield Conn()

    async def get_pool():
        return Pool()

    async def insert(conn, text, *, kind, event_key):
        return 5

    monkeypatch.setattr(connection, "get_pool", get_pool)
    monkeypatch.setattr(admin_room, "insert", insert)
    monkeypatch.setattr(settings, "admin_alert_chat_id", "-1009")
    error_alerts.note(_record(), T0)

    await error_alerts.flush(T0)

    assert params[0][2] is False, "left for the monitor to send to the admin chat"


def test_each_error_is_counted_for_prometheus():
    from core.metrics import LOG_ERRORS

    before = LOG_ERRORS.labels("core.channels")._value.get()
    error_alerts.note(_record(), T0)
    assert LOG_ERRORS.labels("core.channels")._value.get() == before + 1
