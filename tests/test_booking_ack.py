"""A short "ok" right after booking is a thank-you, not a new request: the desk
answers it in code, without the model, and never offers times again (seen in
production: "Ok" after the confirmation got "Horários de quinta" back)."""
# ruff: noqa: F811
from datetime import UTC, datetime, timedelta

import pytest

from core import content
from core.handlers import message as handler
from core.models import Appointment
from core.repositories import appointments as appointments_repo
from core.services import scheduling
from tests.test_desk_flow import CONN, _triaged_as, desk, update  # noqa: F401

START = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)


@pytest.fixture
def booked(monkeypatch, desk):
    """Ana just booked, and nothing was said since."""
    meeting = Appointment(id=1, contact_id=1, calendar_id="desk@example.com", provider="google",
                          provider_event_id="ev", title="Conversa", starts_at=START,
                          ends_at=START + timedelta(minutes=30))
    desk.appointments.append(meeting)
    state = {"meeting": meeting}

    async def just_booked(conn, contact_id, *, minutes=120):
        return state["meeting"]

    monkeypatch.setattr(appointments_repo, "just_booked", just_booked)
    return state


def _ack() -> str:
    meeting = Appointment(id=1, contact_id=1, calendar_id="c", provider="google",
                          provider_event_id="ev", title="t", starts_at=START,
                          ends_at=START + timedelta(minutes=30))
    return content.render("booking.ack", quando=scheduling.when_appointment(meeting))


@pytest.mark.parametrize("said", ["Ok", "ok!", "Obrigada", "ok, obrigado", "Beleza 👍", "👍",
                                  "Combinado, até lá", "Perfeito", "tá bom", "valeu"])
async def test_a_thank_you_right_after_booking_gets_a_short_confirmation(monkeypatch, desk, booked,
                                                                         said):
    async def no_model(*args, **kwargs):
        raise AssertionError("a thank-you needs no model")

    monkeypatch.setattr(handler, "route", no_model)
    await handler.process_message(CONN, update(said))
    assert desk.sent[-1] == _ack()
    assert desk.offer_calls == 0 and not desk.offers


@pytest.mark.parametrize("said", ["Ok, mas posso mudar para sexta?", "ok quero remarcar",
                                  "Obrigada, quanto custa?", "não"])
async def test_anything_more_than_a_thank_you_goes_to_triage(monkeypatch, desk, booked, said):
    _triaged_as(monkeypatch, "faq")
    await handler.process_message(CONN, update(said))
    assert desk.sent[-1] != _ack()


async def test_without_a_fresh_booking_an_ok_goes_to_triage(monkeypatch, desk, booked):
    booked["meeting"] = None
    _triaged_as(monkeypatch, "faq")
    await handler.process_message(CONN, update("Ok"))
    assert desk.sent[-1] == "Claro."
