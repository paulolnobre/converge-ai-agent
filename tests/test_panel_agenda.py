"""The agenda API: the appointments of today, the events of the team's
calendars next to them, and whether that mirror is current — plus the
"Atualizar agenda" request that wakes the sync."""
# ruff: noqa: F811
from datetime import UTC, datetime, timedelta

import pytest

from core.config import settings
from core.models import StaffMember
from core.repositories import calendar_mirror
from core.repositories import contacts as contacts_repo
from core.repositories import funnel as funnel_repo
from core.services import calendar_sync
from tests.test_panel import _value, accounts, client, clock, signed_in  # noqa: F401

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
EVENT = {"key": "uid|", "starts_at": NOW, "ends_at": NOW + timedelta(minutes=30), "all_day": False,
         "status": "confirmed", "title": "Reunião - Ellen Ribeiro", "staff_ids": [1]}


@pytest.fixture
def mirror(monkeypatch):
    state: dict = {"events": [EVENT], "states": [], "woken": 0}
    monkeypatch.setattr(funnel_repo, "appointments_between", lambda conn, start, end: _value([]))
    monkeypatch.setattr(calendar_mirror, "events_between",
                        lambda conn, start, end: _value(state["events"]))
    monkeypatch.setattr(calendar_mirror, "sync_states", lambda conn: _value(state["states"]))
    monkeypatch.setattr(contacts_repo, "active_staff", lambda conn: _value(
        [StaffMember(id=1, name="Paulo", calendar_id="paulo@x.com")]))
    monkeypatch.setattr(calendar_sync, "calendar_ready", lambda: True)
    monkeypatch.setattr(settings, "reception_calendar_id", "geral")
    monkeypatch.setattr(settings, "staff_calendar_ids", "")

    def wake():
        state["woken"] += 1

    monkeypatch.setattr(calendar_sync, "_wake_worker", wake)
    monkeypatch.setattr(calendar_sync, "_requested_at", None)
    return state


def _state(calendar_id, success=None, error=None, error_at=None, finished=None):
    return {"calendar_id": calendar_id, "last_attempt_at": finished, "last_attempt_finished_at": finished,
            "last_success_at": success, "last_error": error, "last_error_at": error_at}


def test_agenda_carries_events_and_sync(signed_in, mirror):
    now = datetime.now(UTC)
    mirror["states"] = [_state("geral", success=now, finished=now),
                        _state("paulo@x.com", success=now, finished=now)]
    body = signed_in.get("/painel/api/agenda").json()
    assert body["appointments"] == [] and body["events"][0]["title"] == "Reunião - Ellen Ribeiro"
    assert body["events"][0]["staff_ids"] == [1]
    assert body["sync"]["stale"] is False
    assert [c["calendar_id"] for c in body["sync"]["calendars"]] == ["geral", "paulo@x.com"]
    assert body["sync"]["calendars"][1]["staff_id"] == 1


def test_stale_when_a_calendar_never_synced(signed_in, mirror):
    now = datetime.now(UTC)
    mirror["states"] = [_state("geral", success=now, finished=now)]
    assert signed_in.get("/painel/api/agenda").json()["sync"]["stale"] is True


def test_stale_when_an_error_came_after_the_last_success(signed_in, mirror):
    now = datetime.now(UTC)
    mirror["states"] = [_state("geral", success=now - timedelta(minutes=2), error="provider_error",
                               error_at=now, finished=now),
                        _state("paulo@x.com", success=now, finished=now)]
    sync = signed_in.get("/painel/api/agenda").json()["sync"]
    assert sync["stale"] is True and sync["calendars"][0]["last_error"] == "provider_error"


def test_stale_when_the_last_success_is_older_than_three_ticks(signed_in, mirror, monkeypatch):
    monkeypatch.setattr(settings, "calendar_sync_minutes", 5)
    old = datetime.now(UTC) - timedelta(minutes=16)
    mirror["states"] = [_state("geral", success=old, finished=old),
                        _state("paulo@x.com", success=old, finished=old)]
    assert signed_in.get("/painel/api/agenda").json()["sync"]["stale"] is True


def test_not_stale_with_no_calendar(signed_in, mirror, monkeypatch):
    monkeypatch.setattr(calendar_sync, "calendar_ready", lambda: False)
    sync = signed_in.get("/painel/api/agenda").json()["sync"]
    assert sync == {"calendars": [], "stale": False, "requested_at": None, "done": True}


def test_sync_request_returns_requested_at_and_wakes_the_worker(signed_in, mirror):
    response = signed_in.post("/painel/api/agenda/sync")
    assert response.status_code == 200 and response.json()["requested_at"]
    assert mirror["woken"] == 1
    assert signed_in.get("/painel/api/agenda").json()["sync"]["done"] is False


def test_done_after_every_calendar_finished_after_the_request(signed_in, mirror):
    signed_in.post("/painel/api/agenda/sync")
    later = datetime.now(UTC) + timedelta(seconds=5)
    mirror["states"] = [_state("geral", success=later, finished=later),
                        _state("paulo@x.com", error="no_access", error_at=later, finished=later)]
    sync = signed_in.get("/painel/api/agenda").json()["sync"]
    assert sync["done"] is True and sync["stale"] is True


def test_sync_request_needs_a_session(client, mirror):
    assert client.post("/painel/api/agenda/sync").status_code == 401
    assert mirror["woken"] == 0


def test_the_agenda_has_the_stale_banner_and_the_sync_button(signed_in):
    page = signed_in.get("/painel").text
    assert 'id="agenda-stale"' in page and 'id="agenda-sync-btn"' in page


def test_each_calendar_says_whether_it_is_stale(signed_in, mirror):
    """A calendar that failed last week and has been fine since is not named."""
    now = datetime.now(UTC)
    mirror["states"] = [
        _state("geral", success=now, error="provider_error", error_at=now - timedelta(days=7),
               finished=now),
        _state("paulo@x.com", success=now - timedelta(hours=1), finished=now - timedelta(hours=1)),
    ]
    calendars = signed_in.get("/painel/api/agenda").json()["sync"]["calendars"]
    assert [c["stale"] for c in calendars] == [False, True]
