"""A test (/teste) is not announced in the team group: not the proposal it
sends, not the answer to a proposal document. The marked lines of
tests/test_test_mode.py cover the handoff, the new conversation and the
meetings; here, the two notices that take a contact and a text."""
from datetime import datetime

import pytest

from core import content
from core.models import Contact, ProposalBriefing
from core.services import inbox, proposal_briefing, proposal_send


def _ana(test_mode: str | None) -> Contact:
    return Contact(id=7, chat_id="wa:5511900000000", name="Ana", channel="whatsapp",
                   test_mode=test_mode)


@pytest.fixture
def group(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What reaches the team group; the push to the team's devices is not the group."""
    lines: list[str] = []

    async def notify_team(text, contact_id):
        lines.append(text)
        return True

    monkeypatch.setattr(inbox, "notify_team", notify_team)
    monkeypatch.setattr(inbox, "push_to_team", lambda text, contact: None)
    return lines


def _briefing() -> ProposalBriefing:
    now = datetime(2026, 11, 9, 12, 0)
    return ProposalBriefing(id=1, contact_id=7, answers={},
                            status="open", created_at=now, updated_at=now)


@pytest.mark.parametrize("test_mode, sent", [("novo", False), (None, True)])
async def test_the_proposal_notice(group, test_mode, sent):
    ana = _ana(test_mode)

    told = await proposal_briefing.tell_team("proposal.team_sent", ana, _briefing())

    assert told is True
    assert bool(group) == sent


@pytest.mark.parametrize("test_mode, sent", [("novo", False), (None, True)])
async def test_the_answer_to_a_proposal_document(group, test_mode, sent):
    ana = _ana(test_mode)

    label, reply = await proposal_send.decision(ana, "propdoc:accept")

    assert (label, reply) == (content.text("proposal_doc.button_accept"),
                              content.text("proposal_doc.accept_reply"))
    assert bool(group) == sent
