"""Every triaged turn leaves one log line with what the model decided — the
intent, its confidence, the day and period it understood, and whether an
offer was open — never the words or the model's summary of them. It is what
explains, days later, why the desk answered as it did."""
# ruff: noqa: F811
import logging

from core.handlers import message as handler
from core.models import AgentReply, Triage
from tests.test_desk_flow import CONN, desk, update  # noqa: F401


async def test_the_decision_is_logged_without_the_words(monkeypatch, desk, caplog):
    async def route(body, chat_id, history_text="", organization="", retrieve=None):
        return (Triage(intent="faq", confidence=0.82, summary="SEGREDO DO RESUMO",
                       preferred_period="morning"),
                AgentReply(text="Claro.", intent="faq"))

    monkeypatch.setattr(handler, "route", route)
    with caplog.at_level(logging.INFO, logger="core.handlers.message"):
        await handler.process_message(CONN, update("minha senha é SEGREDO DA MENSAGEM"))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Triage")]
    assert len(lines) == 1
    line = lines[0]
    for part in ("intent=faq", "confidence=0.82", "period=morning", "day=-", "pending_offer=no"):
        assert part in line, line
    assert "SEGREDO" not in " ".join(r.getMessage() for r in caplog.records)
