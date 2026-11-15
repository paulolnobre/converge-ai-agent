"""Triaging the shared inbox, and refusing to send what nobody approved."""
from typing import Any

import pytest

from core.integrations.workspace.models import MailAddress, MailMessage
from core.models import MailTriage
from core.services import mailbox


def _message(**overrides) -> MailMessage:
    base: dict[str, Any] = dict(
        id="m1",
        thread_id="t1",
        sender=MailAddress(email="cliente@example.com"),
        subject="Duvida sobre a proposta",
        body_text="Bom dia, recebi a proposta e queria entender o prazo.",
    )
    base.update(overrides)
    return MailMessage(**base)


def _triage_answer(monkeypatch: pytest.MonkeyPatch, payload):
    async def fake_complete_json(*args, **kwargs):
        return payload

    monkeypatch.setattr(mailbox, "complete_json", fake_complete_json)


async def test_a_clear_email_is_categorized(monkeypatch: pytest.MonkeyPatch):
    _triage_answer(monkeypatch, {
        "category": "proposta", "priority": 2, "needs_human": False,
        "suggested_reply": "O prazo e de 10 dias uteis.", "summary": "prazo",
    })
    decision = await mailbox.triage(_message())
    assert decision.category == "proposta"
    assert decision.priority == 2
    assert decision.suggested_reply


async def test_an_unusable_answer_defaults_to_a_human(monkeypatch: pytest.MonkeyPatch):
    """The cautious default matters: this is somebody's complaint."""
    _triage_answer(monkeypatch, {"priority": 99})
    decision = await mailbox.triage(_message())
    assert decision.needs_human is True
    assert decision.priority == 3


async def test_no_model_answer_still_produces_a_decision(monkeypatch: pytest.MonkeyPatch):
    _triage_answer(monkeypatch, None)
    decision = await mailbox.triage(_message(subject="Urgente"))
    assert decision.needs_human is True
    assert decision.summary == "Urgente"


def test_priority_is_bounded():
    with pytest.raises(ValueError):
        MailTriage(priority=0)
    with pytest.raises(ValueError):
        MailTriage(priority=6)
