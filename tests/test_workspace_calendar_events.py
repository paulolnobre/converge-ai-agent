"""Reading a whole calendar: every page, or nothing (a partial list would make
the panel's mirror drop events that are still there), and the facts the
agenda needs about each event — all-day as a civil date in the office's zone,
tentative, version, the meeting's identity and whether this calendar organizes
it."""
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from core.config import settings
from core.integrations.workspace.errors import WorkspaceError
from core.integrations.workspace.google import GoogleWorkspace
from core.integrations.workspace.microsoft import _GRAPH, MicrosoftWorkspace

START = datetime(2026, 10, 20, tzinfo=ZoneInfo("America/Belem"))
END = datetime(2026, 11, 20, tzinfo=ZoneInfo("America/Belem"))


@pytest.fixture(autouse=True)
def office_in_belem_process_in_utc(monkeypatch):
    monkeypatch.setattr(settings, "office_timezone", "America/Belem")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def _google_item(n: int, **extra) -> dict:
    return {"id": f"e{n}", "summary": f"Evento {n}", "status": "confirmed",
            "start": {"dateTime": "2026-10-23T10:00:00-03:00"},
            "end": {"dateTime": "2026-10-23T10:30:00-03:00"}, **extra}


def _pages(monkeypatch, adapter, pages: list):
    """Each call to _request answers the next page; an Exception is raised."""
    calls: list[dict] = []

    async def fake_request(method, url, *, params=None, **kwargs):
        calls.append({"url": url, "params": dict(params or {})})
        page = pages[len(calls) - 1]
        if isinstance(page, Exception):
            raise page
        return httpx.Response(200, json=page)

    monkeypatch.setattr(adapter, "_request", fake_request)
    return calls


async def test_google_reads_every_page(monkeypatch):
    adapter = GoogleWorkspace()
    calls = _pages(monkeypatch, adapter, [
        {"items": [_google_item(n) for n in range(250)], "nextPageToken": "p2"},
        {"items": [_google_item(n) for n in range(250, 253)]},
    ])
    events = await adapter.list_events("cal", START, END)
    assert len(events) == 253
    assert "pageToken" not in calls[0]["params"] and calls[1]["params"]["pageToken"] == "p2"


async def test_google_error_on_second_page_raises_and_returns_nothing(monkeypatch):
    adapter = GoogleWorkspace()
    _pages(monkeypatch, adapter, [
        {"items": [_google_item(1)], "nextPageToken": "p2"},
        WorkspaceError("Google GET falhou (500)", status=500),
    ])
    with pytest.raises(WorkspaceError):
        await adapter.list_events("cal", START, END)


async def test_google_all_day_is_a_civil_date_in_the_office_zone(monkeypatch):
    adapter = GoogleWorkspace()
    _pages(monkeypatch, adapter, [{"items": [{
        "id": "d", "summary": "Feriado", "status": "confirmed",
        "start": {"date": "2026-10-23"}, "end": {"date": "2026-10-24"}}]}])
    event, = await adapter.list_events("cal", START, END)
    belem = ZoneInfo("America/Belem")
    assert event.all_day is True
    assert event.start == datetime(2026, 10, 23, tzinfo=belem)
    assert event.start.utcoffset() == belem.utcoffset(datetime(2026, 10, 23))
    assert event.end == datetime(2026, 10, 24, tzinfo=belem)


async def test_google_fills_status_etag_ical_uid_original_start_and_organizer(monkeypatch):
    adapter = GoogleWorkspace()
    _pages(monkeypatch, adapter, [{"items": [_google_item(
        7, status="tentative", etag='"3"', iCalUID="abc@google.com",
        originalStartTime={"dateTime": "2026-10-23T09:00:00-03:00"},
        organizer={"email": "paulo@x.com", "self": True})]}])
    event, = await adapter.list_events("cal", START, END)
    assert (event.status, event.etag, event.ical_uid) == ("tentative", '"3"', "abc@google.com")
    assert event.original_start == datetime.fromisoformat("2026-10-23T09:00:00-03:00")
    assert event.organizer_is_self is True and event.all_day is False


async def test_google_defaults_when_fields_are_absent(monkeypatch):
    adapter = GoogleWorkspace()
    _pages(monkeypatch, adapter, [{"items": [_google_item(1)]}])
    event, = await adapter.list_events("cal", START, END)
    assert (event.status, event.etag, event.ical_uid, event.original_start,
            event.organizer_is_self) == ("confirmed", None, None, None, False)


async def test_google_skips_cancelled_items(monkeypatch):
    adapter = GoogleWorkspace()
    _pages(monkeypatch, adapter, [{"items": [_google_item(1), _google_item(2, status="cancelled")]}])
    assert [e.id for e in await adapter.list_events("cal", START, END)] == ["e1"]


def _graph_item(n: int, **extra) -> dict:
    return {"id": f"m{n}", "subject": f"Evento {n}",
            "start": {"dateTime": "2026-10-23T10:00:00.0000000", "timeZone": "America/Belem"},
            "end": {"dateTime": "2026-10-23T10:30:00.0000000", "timeZone": "America/Belem"},
            **extra}


async def test_microsoft_follows_next_link(monkeypatch):
    adapter = MicrosoftWorkspace()
    calls = _pages(monkeypatch, adapter, [
        {"value": [_graph_item(1)],
         "@odata.nextLink": f"{_GRAPH}/users/u/calendarView?$skiptoken=abc"},
        {"value": [_graph_item(2)]},
    ])
    events = await adapter.list_events("u", START, END)
    assert [e.id for e in events] == ["m1", "m2"]
    assert calls[1]["url"] == "/users/u/calendarView?$skiptoken=abc" and calls[1]["params"] == {}


async def test_microsoft_fills_all_day_etag_ical_uid_status_and_is_organizer(monkeypatch):
    adapter = MicrosoftWorkspace()
    _pages(monkeypatch, adapter, [{"value": [_graph_item(
        1, isAllDay=True, showAs="tentative", isOrganizer=True, iCalUId="uid-1",
        **{"@odata.etag": 'W/"x"',
           "start": {"dateTime": "2026-10-23T00:00:00.0000000", "timeZone": "UTC"},
           "end": {"dateTime": "2026-10-24T00:00:00.0000000", "timeZone": "UTC"}})]}])
    event, = await adapter.list_events("u", START, END)
    belem = ZoneInfo("America/Belem")
    assert event.all_day is True and event.start == datetime(2026, 10, 23, tzinfo=belem)
    assert (event.status, event.etag, event.ical_uid, event.organizer_is_self) == (
        "tentative", 'W/"x"', "uid-1", True)


async def test_microsoft_original_start_is_a_plain_string(monkeypatch):
    """Graph sends originalStart as a DateTimeOffset string, not {dateTime, timeZone}."""
    adapter = MicrosoftWorkspace()
    _pages(monkeypatch, adapter, [{"value": [_graph_item(1, originalStart="2026-10-23T12:00:00Z")]}])
    event, = await adapter.list_events("u", START, END)
    assert event.original_start == datetime(2026, 10, 23, 12, tzinfo=ZoneInfo("UTC"))


# ── One event, and telling a passing error from a refusal ─────────────────────

from core.integrations.workspace import google as google_module  # noqa: E402
from core.integrations.workspace.errors import (  # noqa: E402
    WorkspaceAuthError,
    WorkspaceTransient,
)
from core.integrations.workspace.models import Gone, NotFound  # noqa: E402


def _answers(monkeypatch, adapter, answers: list):
    """Each _request call takes the next answer: a dict body, or an int status
    (raised the way _request raises it)."""
    async def fake_request(method, url, **kwargs):
        answer = answers.pop(0)
        if isinstance(answer, int):
            raise WorkspaceError(f"falhou ({answer})", status=answer)
        return httpx.Response(200, json=answer)

    monkeypatch.setattr(adapter, "_request", fake_request)


async def test_google_get_event_returns_the_event(monkeypatch):
    adapter = GoogleWorkspace()
    _answers(monkeypatch, adapter, [_google_item(5, etag='"9"')])
    event = await adapter.get_event("cal", "e5")
    assert not isinstance(event, (Gone, NotFound)) and event.id == "e5" and event.etag == '"9"'


async def test_google_get_event_cancelled_body_is_gone(monkeypatch):
    adapter = GoogleWorkspace()
    _answers(monkeypatch, adapter, [{"id": "e5", "status": "cancelled"}])
    assert isinstance(await adapter.get_event("cal", "e5"), Gone)


async def test_google_get_event_410_is_gone_and_404_is_not_found(monkeypatch):
    adapter = GoogleWorkspace()
    _answers(monkeypatch, adapter, [410, 404])
    assert isinstance(await adapter.get_event("cal", "e5"), Gone)
    assert isinstance(await adapter.get_event("cal", "e5"), NotFound)


async def test_google_get_event_500_raises(monkeypatch):
    adapter = GoogleWorkspace()
    _answers(monkeypatch, adapter, [500])
    with pytest.raises(WorkspaceError):
        await adapter.get_event("cal", "e5")


async def test_microsoft_get_event_404_is_not_found(monkeypatch):
    adapter = MicrosoftWorkspace()
    _answers(monkeypatch, adapter, [404])
    assert isinstance(await adapter.get_event("u", "m1"), NotFound)


class _Http:
    def __init__(self, response: httpx.Response):
        self.response = response

    async def request(self, *args, **kwargs):
        return self.response


@pytest.mark.parametrize("status, body, kind", [
    (403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}, WorkspaceTransient),
    (403, {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}, WorkspaceTransient),
    (403, {"error": {"errors": [{"reason": "forbidden"}]}}, WorkspaceAuthError),
    (500, {}, WorkspaceTransient),
    (429, {}, WorkspaceTransient),
])
async def test_google_tells_a_passing_error_from_a_refusal(monkeypatch, status, body, kind):
    async def token(subject=None):
        return "t"

    monkeypatch.setattr(google_module, "google_access_token", token)
    monkeypatch.setattr(google_module, "_http", lambda: _Http(httpx.Response(status, json=body)))
    with pytest.raises(kind) as raised:
        await GoogleWorkspace()._request("GET", "https://www.googleapis.com/calendar/v3/x")
    if kind is WorkspaceAuthError:
        assert not isinstance(raised.value, WorkspaceTransient)
