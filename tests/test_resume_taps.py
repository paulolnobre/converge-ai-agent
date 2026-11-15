"""The two buttons of the "conversa ficou pela metade" template: "Quero
continuar" is a person writing back, "Agora não" is left alone."""
# ruff: noqa: F811
import pytest

from core import content
from core.handlers import message as handler
from core.models import AgentReply, Triage
from core.repositories import automations as automations_repo
from tests.test_desk_flow import CONN, desk, tap  # noqa: F401


@pytest.fixture
def reasons(monkeypatch: pytest.MonkeyPatch):
    saved: list[tuple] = []

    async def record_reason(conn, queue_id, contact_id, reason):
        saved.append((queue_id, reason))
        return True

    monkeypatch.setattr(automations_repo, "record_reason", record_reason)
    return saved


async def test_not_now_is_acknowledged_and_noted(desk, reasons):
    assert await handler.process_callback(CONN, tap("resume:no:5")) == "resume"

    assert desk.sent[-1] == content.text("automation.resume_no_reply")
    assert reasons == [(5, "agora_nao")]
    assert ("user", f"(tocou no botão) {content.text('automation.resume_no')}", None) in desk.recorded


async def test_wanting_to_continue_goes_through_the_normal_conversation(
    monkeypatch: pytest.MonkeyPatch, desk, reasons
):
    heard: list[str] = []

    async def route(body, chat_id, history_text="", organization="", retrieve=None):
        heard.append(body)
        return (Triage(intent="other", confidence=0.9),
                AgentReply(text="Claro, vamos continuar. Me conta o que você precisa.", intent="other"))

    monkeypatch.setattr(handler, "route", route)

    await handler.process_callback(CONN, tap("resume:yes:5"))

    assert heard == [content.text("automation.resume_yes")]
    assert desk.sent[-1] == "Claro, vamos continuar. Me conta o que você precisa."
    assert reasons == []


async def test_a_conversation_a_person_holds_gets_the_tap_not_the_agent(
    monkeypatch: pytest.MonkeyPatch, desk, reasons
):
    async def route(*args, **kwargs):
        raise AssertionError("the agent must stay out of a conversation a person holds")

    monkeypatch.setattr(handler, "route", route)
    desk.session_by_chat["42"] = 1
    desk.sessions[1] = {"id": 1, "contact_id": 1, "chat_id": "42", "team_chat_id": "t",
                        "reason": "pedido", "status": "claimed", "claimed_by_id": None,
                        "claimed_by_name": "Victor", "claimed_by_user_id": 2, "relay_chat_id": "t"}

    assert await handler.process_callback(CONN, tap("resume:yes:5")) == "handoff"


async def test_wanting_to_continue_spends_the_rate_limit_once(
    monkeypatch: pytest.MonkeyPatch, desk
):
    asked: list[str] = []
    original = handler._allowed

    async def counting(chat_id):
        asked.append(chat_id)
        return await original(chat_id)

    async def route(body, chat_id, history_text="", organization="", retrieve=None):
        return (Triage(intent="other", confidence=0.9), AgentReply(text="Claro.", intent="other"))

    monkeypatch.setattr(handler, "_allowed", counting)
    monkeypatch.setattr(handler, "route", route)

    await handler.process_callback(CONN, tap("resume:yes:5"))

    assert len(asked) == 1
