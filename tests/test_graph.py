"""Triaging one message to the front desk."""
import asyncio

import pytest

from core import content
from core.services import graph


@pytest.fixture(autouse=True)
def fresh_graph():
    graph._compiled = None
    yield
    graph._compiled = None


def _triage_answer(monkeypatch: pytest.MonkeyPatch, payload):
    async def fake_complete_json(*args, **kwargs):
        return payload

    async def fake_complete(*args, **kwargs):
        return "Claro, posso ajudar com isso."

    monkeypatch.setattr(graph, "complete_json", fake_complete_json)
    monkeypatch.setattr(graph, "complete", fake_complete)


async def test_routing_never_leaves_the_event_loop(monkeypatch: pytest.MonkeyPatch):
    """A sync node or edge makes LangGraph borrow a thread from the loop's
    default executor, and closing the loop then waits for that thread — the
    hang seen at teardown. Every step of the graph stays a coroutine."""
    _triage_answer(monkeypatch, {"intent": "booking", "summary": "", "confidence": 0.95})
    loop = asyncio.get_running_loop()

    def no_threads(executor, func, *args):
        pytest.fail(f"routing ran {func!r} on a thread")

    monkeypatch.setattr(loop, "run_in_executor", no_threads)

    await graph.route("queria marcar", "555")


async def test_a_question_is_answered(monkeypatch: pytest.MonkeyPatch):
    _triage_answer(monkeypatch, {"intent": "faq", "summary": "horario", "confidence": 0.9})
    triage, reply = await graph.route("voces abrem sabado?", "555")
    assert triage.intent == "faq"
    assert reply.text


async def test_a_booking_is_left_for_the_caller_to_schedule(
    monkeypatch: pytest.MonkeyPatch,
):
    """The graph must not touch a real calendar; the handler owns that."""
    _triage_answer(monkeypatch, {
        "intent": "booking", "summary": "quer marcar", "confidence": 0.95,
    })
    triage, reply = await graph.route("queria marcar uma consulta", "555")
    assert triage.intent == "booking"
    assert reply.text == ""


async def test_a_low_confidence_booking_never_reaches_the_calendar(
    monkeypatch: pytest.MonkeyPatch,
):
    """A misread 'book' would put a stranger in someone's Tuesday."""
    _triage_answer(monkeypatch, {"intent": "booking", "summary": "", "confidence": 0.2})
    _, reply = await graph.route("hmm", "555")
    assert reply.text  # it asked instead of scheduling


async def test_a_low_confidence_cancel_never_cancels(monkeypatch: pytest.MonkeyPatch):
    _triage_answer(monkeypatch, {"intent": "cancel", "summary": "", "confidence": 0.3})
    _, reply = await graph.route("nao sei", "555")
    assert reply.text == content.text("reply.not_understood")


async def test_a_low_confidence_faq_still_gets_a_real_answer_attempt(
    monkeypatch: pytest.MonkeyPatch,
):
    """The confidence floor only guards scheduling actions. A hesitant 'faq'
    reading still deserves retrieval — an empty result is its own refusal."""
    _triage_answer(monkeypatch, {"intent": "faq", "summary": "", "confidence": 0.2})

    async def retrieve(_question):
        return [{"heading": "Horario", "body": "Atendemos das 9 as 18."}]

    triage, reply = await graph.route("aberto agora?", "555", retrieve=retrieve)
    assert triage.confidence < 0.6
    assert reply.text == "Claro, posso ajudar com isso."


async def test_a_greeting_gets_the_welcome_message(monkeypatch: pytest.MonkeyPatch):
    _triage_answer(monkeypatch, {"intent": "greeting", "summary": "", "confidence": 0.95})
    triage, reply = await graph.route("oi", "555")
    assert triage.intent == "greeting"
    assert reply.text == content.text("reply.welcome")


async def test_small_talk_gets_a_reply_and_never_the_faq_refusal(
    monkeypatch: pytest.MonkeyPatch,
):
    """ "eu só te cumprimentei" is a reaction, not a question: it gets a
    conversational answer from a prompt that carries no facts to state."""
    prompts: list[str] = []

    async def fake_complete_json(*args, **kwargs):
        return {"intent": "smalltalk", "summary": "", "confidence": 0.9}

    async def fake_complete(system, user, **kwargs):
        prompts.append(system)
        return "Desculpe! Olá, tudo bem? Como posso ajudar?"

    async def retrieve(_question):
        raise AssertionError("small talk is not looked up in the FAQ")

    monkeypatch.setattr(graph, "complete_json", fake_complete_json)
    monkeypatch.setattr(graph, "complete", fake_complete)

    triage, reply = await graph.route("eu só te cumprimentei.", "555", retrieve=retrieve)

    assert triage.intent == "smalltalk"
    assert reply.text == "Desculpe! Olá, tudo bem? Como posso ajudar?"
    assert reply.needs_human is False
    assert "NAO afirme nenhum fato sobre a organizacao" in prompts[0]


async def test_small_talk_without_a_model_answer_still_replies(monkeypatch: pytest.MonkeyPatch):
    async def fake_complete_json(*args, **kwargs):
        return {"intent": "smalltalk", "summary": "", "confidence": 0.9}

    async def no_answer(*args, **kwargs):
        return None

    monkeypatch.setattr(graph, "complete_json", fake_complete_json)
    monkeypatch.setattr(graph, "complete", no_answer)

    _, reply = await graph.route("valeu!", "555")

    assert reply.text == content.text("reply.smalltalk_fallback")


async def test_asking_what_is_booked_is_left_to_the_caller_at_any_confidence(
    monkeypatch: pytest.MonkeyPatch,
):
    """Listing one's own bookings changes nothing, so no confidence floor —
    the handler answers it from the database."""
    _triage_answer(monkeypatch, {"intent": "appointments", "summary": "", "confidence": 0.3})
    triage, reply = await graph.route("tenho algo marcado?", "555")
    assert triage.intent == "appointments"
    assert reply.text == ""


async def test_a_bare_yes_is_searched_with_what_it_accepted(monkeypatch: pytest.MonkeyPatch):
    """ "sim" to "quer conhecer as aplicações?" finds nothing on its own; the
    triage summary carries what was accepted into the search."""
    _triage_answer(monkeypatch, {
        "intent": "faq", "confidence": 0.9,
        "summary": "quer conhecer as aplicacoes de agentes de IA da Converge",
    })
    searched: list[str] = []

    async def retrieve(query):
        searched.append(query)
        return [{"heading": "Aplicacoes", "body": "Assistente Juridico..."}]

    await graph.route("sim", "555", retrieve=retrieve)

    assert "aplicacoes de agentes" in searched[0] and searched[0].startswith("sim")


async def test_triage_is_told_what_day_it_is(monkeypatch: pytest.MonkeyPatch):
    """Without today's date the model cannot read "quinta" as a day."""
    prompts: list[str] = []

    async def fake_complete_json(system, user, **kwargs):
        prompts.append(system)
        return {"intent": "booking", "summary": "", "confidence": 0.9}

    monkeypatch.setattr(graph, "complete_json", fake_complete_json)
    await graph.route("quero marcar", "555")

    assert f"Hoje e {graph._WEEKDAY_NAMES[graph._today().weekday()]}" in prompts[0]
    assert graph._today().isoformat() in prompts[0]


async def test_a_day_in_the_past_from_triage_is_dropped(monkeypatch: pytest.MonkeyPatch):
    """A model guessing the wrong year must not send the search to today."""
    _triage_answer(monkeypatch, {
        "intent": "booking", "confidence": 0.9, "preferred_day": "2020-10-01",
    })
    triage, _ = await graph.route("quinta 1/10", "555")
    assert triage.preferred_day is None


async def test_escalation_flags_a_human(monkeypatch: pytest.MonkeyPatch):
    _triage_answer(monkeypatch, {
        "intent": "escalation", "summary": "reclamacao", "confidence": 0.9,
    })
    _, reply = await graph.route("quero falar com o responsavel", "555")
    assert reply.needs_human is True


async def test_an_unparseable_triage_degrades_safely(monkeypatch: pytest.MonkeyPatch):
    _triage_answer(monkeypatch, {"intent": "not-an-intent", "confidence": 9})
    triage, reply = await graph.route("qualquer coisa", "555")
    assert triage.intent == "other"
    assert reply.text


async def test_no_model_answer_at_all_still_replies(monkeypatch: pytest.MonkeyPatch):
    _triage_answer(monkeypatch, None)
    triage, reply = await graph.route("bom dia", "555")
    assert triage.intent == "other"
    assert reply.text


def test_the_proposal_intent_is_accepted():
    from core.models import Triage

    assert Triage.model_validate({"intent": "proposal"}).intent == "proposal"


def test_the_triage_prompt_knows_the_proposal_intent():
    assert "proposal (" in graph._TRIAGE_SYSTEM


@pytest.mark.parametrize("message", [
    "Prefiro resolver por aqui msm", "isso não está funcionando", "não gostei da resposta"])
async def test_only_an_explicit_request_for_a_person_goes_to_the_team(
        monkeypatch: pytest.MonkeyPatch, message):
    """The triage model took "prefiro resolver por aqui" for a request for a
    person. Only words that ask for a person, the team or a department hand the
    conversation over; anything else stays with the desk."""
    _triage_answer(monkeypatch, {"intent": "escalation", "summary": "", "confidence": 0.9})
    triage, reply = await graph.route(message, "555")
    assert triage.intent != "escalation"
    assert reply.text != content.text("reply.escalation")


async def test_asking_for_the_team_still_goes_to_the_team(monkeypatch: pytest.MonkeyPatch):
    _triage_answer(monkeypatch, {"intent": "escalation", "summary": "", "confidence": 0.9})
    triage, reply = await graph.route("quero falar com alguém da equipe", "555")
    assert triage.intent == "escalation" and reply.needs_human is True


@pytest.mark.parametrize("message", [
    "Olá! Vi o anúncio e quero conhecer o agente de IA para minha empresa.",
    "Oi, quero saber mais sobre os agentes de vocês",
    "Bom dia! Tenho interesse",
])
async def test_an_opening_without_a_scheduling_request_gets_the_welcome(
        monkeypatch: pytest.MonkeyPatch, message):
    """The ad's prefilled "quero conhecer o agente" was read as a booking and
    got the contact request "antes de ver os horários". A first message that
    never asks to schedule is an opening: it gets the welcome, which offers
    knowing the solutions, a proposal or a conversation."""
    _triage_answer(monkeypatch, {"intent": "booking", "summary": "", "confidence": 0.9})
    triage, reply = await graph.route(message, "555")
    assert triage.intent == "greeting"
    assert reply.text == content.text("reply.welcome")


@pytest.mark.parametrize("message", [
    "Oi! Quero agendar uma conversa", "Olá, dá pra marcar uma reunião amanhã?",
    "Quais horários vocês têm?"])
async def test_a_first_message_asking_to_schedule_still_books(
        monkeypatch: pytest.MonkeyPatch, message):
    _triage_answer(monkeypatch, {"intent": "booking", "summary": "", "confidence": 0.9})
    triage, _ = await graph.route(message, "555")
    assert triage.intent == "booking"


async def test_a_yes_to_an_offered_conversation_still_books(monkeypatch: pytest.MonkeyPatch):
    """Mid-conversation, a short "quero" accepting the offer stays a booking."""
    _triage_answer(monkeypatch, {"intent": "booking", "summary": "marcar conversa",
                                 "confidence": 0.9})
    triage, _ = await graph.route("quero", "555",
                                  history_text="assistant: Quer marcar uma conversa?")
    assert triage.intent == "booking"
