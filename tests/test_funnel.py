"""The bot moves a contact only through what it can see, and never past a
person's own call."""
import pytest

from core.repositories import funnel as funnel_repo
from core.services import funnel


def test_a_reply_moves_only_a_brand_new_contact():
    assert funnel.transition("replied") == ("respondido", ("novo",))


def test_a_booking_never_overrides_a_human_stage():
    to, allowed_from = funnel.transition("booked")
    assert to == "agendado"
    assert not {"proposta", "fechado", "perdido"} & set(allowed_from)


def test_a_cancellation_steps_back_only_from_agendado():
    """Cancelling a meeting says nothing about a proposal already out."""
    assert funnel.transition("cancelled") == ("respondido", ("agendado",))


async def test_a_failing_funnel_write_never_breaks_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
):
    async def broken(*args, **kwargs):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(funnel_repo, "move_stage", broken)
    assert await funnel.advance(object(), 1, "replied") is False


async def test_no_contact_is_no_move():
    assert await funnel.advance(object(), None, "booked") is False
