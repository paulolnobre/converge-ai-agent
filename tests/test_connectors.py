"""The parsing each connector does on what a provider actually returns.

These are the pure halves — no HTTP. What is tested here is the shape handling
that silently corrupts data when it is wrong: time zones, HTML bodies, the
nested part tree Gmail uses for a message.
"""
from datetime import datetime

from core.config import settings
from core.integrations.workspace import google, microsoft


def test_graph_parses_a_datetime_with_seven_fractional_digits():
    """Graph emits more precision than fromisoformat accepts."""
    parsed = microsoft._graph_datetime(
        {"dateTime": "2026-09-14T13:30:00.1234567", "timeZone": "UTC"}
    )
    assert parsed.year == 2026 and parsed.hour == 13
    assert parsed.tzinfo is not None


def test_graph_datetime_is_never_naive():
    """A naive datetime here books an appointment hours off after a DST change."""
    parsed = microsoft._graph_datetime({"dateTime": "2026-09-14T13:30:00Z"})
    assert parsed.utcoffset() is not None


def test_graph_falls_back_to_utc_on_an_unknown_time_zone():
    parsed = microsoft._graph_datetime(
        {"dateTime": "2026-09-14T13:30:00", "timeZone": "Mars/Olympus"}
    )
    offset = parsed.utcoffset()
    assert offset is not None and offset.total_seconds() == 0


def test_a_sheet_range_splits_into_worksheet_and_address():
    assert microsoft._split_sheet_range("Plan1!A1:D10") == ("Plan1", "A1:D10")
    assert microsoft._split_sheet_range("'Meu Plano'!A1") == ("Meu Plano", "A1")
    assert microsoft._split_sheet_range("A1:D10") == ("", "A1:D10")


def test_html_is_reduced_to_text_before_a_model_sees_it():
    html = "<div><p>Bom dia</p><br><b>Segue</b> a nota</div>"
    assert "<" not in microsoft._strip_html(html)
    assert "Bom dia" in google._strip_html(html)


def test_gmail_prefers_the_plain_text_part_over_the_html_one():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<b>versao html</b>")}},
            {"mimeType": "text/plain", "body": {"data": _b64("versao texto")}},
        ],
    }
    assert google.GoogleWorkspace()._body_of(payload).strip() == "versao texto"


def test_gmail_falls_back_to_stripped_html_when_there_is_no_plain_part():
    payload = {"mimeType": "text/html", "body": {"data": _b64("<p>somente html</p>")}}
    body = google.GoogleWorkspace()._body_of(payload)
    assert "somente html" in body and "<p>" not in body


def test_a_google_all_day_event_still_produces_a_datetime():
    event = google.GoogleWorkspace()._to_event(
        {"id": "e1", "summary": "Feriado", "start": {"date": "2026-09-14"},
         "end": {"date": "2026-09-15"}},
        "primary",
    )
    assert isinstance(event.start, datetime)
    assert event.provider == "google"


def test_a_calendar_event_carries_the_office_time_zone_into_the_body():
    from core.integrations.workspace.models import CalendarEvent

    event = CalendarEvent(
        title="Reuniao",
        start=datetime(2026, 9, 14, 10, 0),
        end=datetime(2026, 9, 14, 11, 0),
    )
    body = google.GoogleWorkspace()._event_body(event)
    assert body["start"]["timeZone"] == settings.office_timezone
    assert microsoft.MicrosoftWorkspace()._event_body(event)["start"]["timeZone"] == (
        settings.office_timezone
    )


def _b64(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode()).decode()


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _fake_google(monkeypatch, responses: list[dict]):
    """A GoogleWorkspace whose HTTP layer answers from `responses`, in order."""
    workspace = google.GoogleWorkspace()
    calls: list[dict] = []

    async def request(method, url, *, params=None, json=None, **kwargs):
        calls.append({"method": method, "url": url, "params": params or {}, "json": json})
        return _Response(responses.pop(0))

    monkeypatch.setattr(workspace, "_request", request)
    return workspace, calls


_EVENT = {"id": "e1", "summary": "Atendimento", "start": {"dateTime": "2026-10-01T10:00:00-03:00"},
          "end": {"dateTime": "2026-10-01T10:30:00-03:00"}}


def _meeting(online: bool):
    from core.integrations.workspace.models import CalendarEvent

    return CalendarEvent(title="Atendimento", start=datetime(2026, 10, 1, 10, 0),
                         end=datetime(2026, 10, 1, 10, 30), online_meeting=online)


async def test_a_meet_link_is_requested_with_the_event_and_read_back(monkeypatch):
    workspace, calls = _fake_google(
        monkeypatch, [{**_EVENT, "hangoutLink": "https://meet.google.com/abc-defg-hij"}]
    )

    created = await workspace.create_event("cal", _meeting(online=True))

    request = calls[0]["json"]["conferenceData"]["createRequest"]
    assert request["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    assert request["requestId"]
    assert calls[0]["params"]["conferenceDataVersion"] == 1
    assert calls[0]["params"]["sendUpdates"] == "all", "the invite still goes out"
    assert created.meeting_link == "https://meet.google.com/abc-defg-hij"


async def test_a_meet_still_being_created_is_read_once_more(monkeypatch):
    video = {"entryPoints": [{"entryPointType": "phone", "uri": "tel:+1-555"},
                             {"entryPointType": "video", "uri": "https://meet.google.com/xyz"}]}
    workspace, calls = _fake_google(monkeypatch, [_EVENT, {**_EVENT, "conferenceData": video}])

    created = await workspace.create_event("cal", _meeting(online=True))

    assert [call["method"] for call in calls] == ["POST", "GET"]
    assert created.meeting_link == "https://meet.google.com/xyz"


async def test_no_meeting_unless_asked(monkeypatch):
    workspace, calls = _fake_google(monkeypatch, [_EVENT])

    created = await workspace.create_event("cal", _meeting(online=False))

    assert "conferenceData" not in calls[0]["json"]
    assert "conferenceDataVersion" not in calls[0]["params"]
    assert created.meeting_link is None and len(calls) == 1


# ── Errors never carry the request's path (it holds e-mails and calendar ids) ─

import logging  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from core.integrations.workspace.errors import WorkspaceError, audit_detail  # noqa: E402
from core.logger import JsonFormatter  # noqa: E402

_PRIVATE = "maria.silva@x.com"


def _transport(status: int | None):
    def handle(request: httpx.Request) -> httpx.Response:
        if status is None:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)
        return httpx.Response(status, request=request)
    return httpx.MockTransport(handle)


def _logged(error: Exception) -> str:
    """The error as `logger.exception` would write it, traceback included."""
    record = logging.LogRecord("core.test", logging.ERROR, __file__, 1, "Falhou", None,
                               (type(error), error, error.__traceback__))
    return JsonFormatter().format(record)


@pytest.mark.parametrize("status", [None, 403, 500])
async def test_a_google_error_keeps_the_path_out_of_the_log(monkeypatch, status):
    async def token(subject=None):
        return "token"
    monkeypatch.setattr(google, "google_access_token", token)
    monkeypatch.setattr(google, "_client", httpx.AsyncClient(transport=_transport(status)))
    with pytest.raises(WorkspaceError) as caught:
        await google.GoogleWorkspace()._request(
            "GET", f"https://www.googleapis.com/calendar/v3/calendars/{_PRIVATE}/events")
    assert _PRIVATE not in _logged(caught.value)
    assert _PRIVATE not in audit_detail(caught.value)
    if status:
        assert caught.value.status == status and str(status) in audit_detail(caught.value)


@pytest.mark.parametrize("status", [None, 403, 500])
async def test_a_graph_error_keeps_the_path_out_of_the_log(monkeypatch, status):
    async def token():
        return "token"
    monkeypatch.setattr(microsoft, "microsoft_access_token", token)
    monkeypatch.setattr(microsoft, "_client", httpx.AsyncClient(transport=_transport(status)))
    with pytest.raises(WorkspaceError) as caught:
        await microsoft.MicrosoftWorkspace()._request("GET", f"/users/{_PRIVATE}/calendar/events")
    assert _PRIVATE not in _logged(caught.value)
    assert _PRIVATE not in audit_detail(caught.value)
