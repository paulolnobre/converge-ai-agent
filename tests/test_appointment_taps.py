"""Buttons about an appointment carry its time: a tap acts only on the meeting
as it was when the button was sent. A button from before a move (by the
calendar sync) does nothing. Old buttons, without a time, still act while the
meeting was never moved."""
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.models import Appointment, Contact, Offer
from core.services import appointment_taps as taps
from core.services import attendance, scheduling

START = datetime(2026, 10, 27, 13, 30, tzinfo=UTC)


def _meeting(**extra) -> Appointment:
    values: dict[str, Any] = dict(id=42, contact_id=7, calendar_id="cal", provider="google", provider_event_id="ev",
                  title="Conversa", starts_at=START, ends_at=START + timedelta(minutes=30))
    values.update(extra)
    return Appointment(**values)


def test_tap_and_parse_round_trip_and_old_format():
    meeting = _meeting()
    data = taps.tap("cancel", meeting)
    assert data == f"appt2:cancel:42:{int(START.timestamp() // 60)}" and len(data) <= 64
    assert taps.parse(data) == ("cancel", 42, int(START.timestamp() // 60))
    assert taps.parse("appt:cancel:42") == ("cancel", 42, None)
    outcome = taps.outcome_tap("no_show", meeting)
    assert outcome.startswith("outcome2:no_show:42:")
    assert taps.parse("outcome:done:42") == ("done", 42, None)
    with pytest.raises(ValueError):
        taps.parse("appt2:cancel:x:1")


def test_still_valid_by_stamp_and_old_buttons_only_if_never_moved():
    meeting = _meeting()
    stamp = taps.parse(taps.tap("ok", meeting))[2]
    assert taps.still_valid(meeting, stamp)
    assert not taps.still_valid(_meeting(starts_at=START + timedelta(days=1),
                                         ends_at=START + timedelta(days=1, minutes=30)), stamp)
    assert taps.still_valid(meeting, None)
    assert not taps.still_valid(_meeting(moved_at=START - timedelta(hours=1)), None)


@pytest.fixture
def contact():
    return Contact(id=7, chat_id="42", name="Ana")


async def test_a_reminder_cancel_tap_from_before_a_move_cancels_nothing(monkeypatch, contact):
    sent_before = _meeting()
    moved = _meeting(starts_at=START + timedelta(days=1), ends_at=START + timedelta(days=1, minutes=30),
                     moved_at=START)
    cancelled = []

    async def own_upcoming(conn, who, appointment_id):
        return moved

    async def cancel(conn, appointment):
        cancelled.append(appointment.id)
        return True

    monkeypatch.setattr(attendance, "own_upcoming", own_upcoming)
    monkeypatch.setattr(scheduling, "cancel", cancel)
    for data in (taps.tap("cancel_yes", sent_before), "appt:cancel_yes:42"):
        _, reply, keyboard = await attendance.handle_tap(object(), contact, data)
        assert keyboard is None and cancelled == []
        assert reply == attendance.content.text("scheduling.reminder_stale")


async def test_the_cancel_question_uses_the_new_format(monkeypatch, contact):
    meeting = _meeting()

    async def own_upcoming(conn, who, appointment_id):
        return meeting

    monkeypatch.setattr(attendance, "own_upcoming", own_upcoming)
    _, _, keyboard = await attendance.handle_tap(object(), contact, taps.tap("cancel", meeting))
    assert keyboard is not None
    datas = [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]
    assert datas == [taps.tap("cancel_yes", meeting), taps.tap("keep", meeting)]


def test_the_reminder_keyboard_uses_the_new_format():
    meeting = _meeting()
    datas = [b["callback_data"] for row in attendance.reminder_keyboard(meeting)["inline_keyboard"]
             for b in row]
    assert datas == [taps.tap("ok", meeting), taps.tap("move", meeting), taps.tap("cancel", meeting)]


async def test_an_old_outcome_tap_after_a_move_records_nothing(monkeypatch):
    moved = _meeting(moved_at=START)
    recorded = []

    async def get_by_id(conn, appointment_id):
        return moved

    async def set_outcome(conn, appointment_id, status, by):
        recorded.append(status)
        return True

    monkeypatch.setattr(attendance, "_team", lambda: "-100")
    monkeypatch.setattr(attendance.appointments_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(attendance.appointments_repo, "set_outcome", set_outcome)
    callback = {"message": {"chat": {"id": -100}}, "from": {"first_name": "Paulo"},
                "data": "outcome:done:42"}
    assert await attendance.handle_team_tap(object(), callback) is False
    stale = taps.outcome_tap("done", _meeting(starts_at=START - timedelta(days=1),
                                              ends_at=START - timedelta(days=1) + timedelta(minutes=30)))
    callback["data"] = stale
    assert await attendance.handle_team_tap(object(), callback) is False
    assert recorded == []


async def test_an_offer_for_a_moved_meeting_is_stale(monkeypatch, contact):
    moved = _meeting(starts_at=START + timedelta(days=1), ends_at=START + timedelta(days=1, minutes=30))
    offer = Offer(id=3, contact_id=7, chat_id="42", slots=[], calendar_id="cal", replaces_id=42,
                  replaces_starts_at=START)

    async def nothing(*args, **kwargs):
        return None

    async def get_by_id(conn, appointment_id):
        return moved

    monkeypatch.setattr(scheduling.appointments_repo, "lock_contact", nothing)
    monkeypatch.setattr(scheduling.appointments_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(scheduling.offers_repo, "expire", nothing)
    result = await scheduling.take_offer(object(), offer=offer, contact=contact,
                                         slot=scheduling.TimeSlot(start=START + timedelta(days=3),
                                                                  end=START + timedelta(days=3, minutes=30)),
                                         title="Conversa")
    assert result.outcome == "stale_reschedule"
