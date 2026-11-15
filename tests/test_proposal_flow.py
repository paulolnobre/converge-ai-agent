"""The proposal-by-e-mail questionnaire inside the conversation: the offer,
the typed answers, a question in the middle, stopping, correcting and sending.

Driven through process_message and process_callback with the Desk of
tests/test_desk_flow.py; the questionnaire's repository is the in-memory fake
of tests/test_proposal_briefing.py, so the service runs for real."""
# The fixtures are imported and taken as parameters: that is how pytest shares
# them, and ruff reads each use as a redefinition.
# ruff: noqa: F811
from datetime import datetime

import pytest

from core import content
from core.config import settings
from core.handlers import message as handler
from core.models import AgentReply, ProposalBriefing, Triage
from core.services import proposal_briefing as pb
from tests.test_choose_day import choose  # noqa: F401
from tests.test_desk_flow import (  # noqa: F401
    CONN,
    ZONE,
    _triaged_as,
    buttons,
    desk,
    tap,
    update,
)
from tests.test_proposal_briefing import world  # noqa: F401

ANSWERS = ["Ana, Clínica Sol, saúde, dona", "agenda e atendimento", "duas pessoas o dia todo"]
# Every question of a company, typed or tapped, in order.
WALK = [("msg", ANSWERS[0]), ("msg", ANSWERS[1]),
        ("tap", "prop:pick:areas:atendimento"), ("tap", "prop:done:areas"),
        ("msg", ANSWERS[2]),
        ("tap", "prop:pick:canais_lista:whatsapp"), ("tap", "prop:pick:canais_lista:telefone"),
        ("tap", "prop:done:canais_lista"),
        ("tap", "prop:pick:volume:ate_500"),
        ("msg", "planilhas"), ("msg", "pronto"),
        ("tap", "prop:pick:processo:sim"),
        ("tap", "prop:pick:usuarios_faixa:3_5"),
        ("tap", "prop:pick:pagamento:cartao"),
        ("msg", "março")]
WALKED = {"quem": ANSWERS[0], "desafio": ANSWERS[1], "areas": "Atendimento e agenda",
          "custo_hoje": ANSWERS[2], "canais_lista": "WhatsApp, Telefone", "volume": "Até 500",
          "sistemas_lista": "planilhas", "processo": "Sim, já está definido",
          "usuarios_faixa": "3 a 5", "pagamento": "Cartão de crédito", "prazo": "março"}


def _no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_model(*args, **kwargs):
        raise AssertionError("an answer to the questionnaire needs no model")
    monkeypatch.setattr(handler, "route", no_model)


def _only(world) -> ProposalBriefing:
    assert len(world.rows) == 1
    return next(iter(world.rows.values()))


async def _answer_everything(monkeypatch: pytest.MonkeyPatch, desk) -> None:
    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    for n, (kind, value) in enumerate(WALK):
        if kind == "msg":
            await handler.process_message(CONN, update(value))
        else:
            await handler.process_callback(CONN, tap(value, f"walk{n}"))


async def test_asking_for_a_proposal_offers_both_paths(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    _triaged_as(monkeypatch, "proposal")

    await handler.process_message(CONN, update("quanto custa para contratar vocês"))

    assert desk.sent == [content.text("proposal.offer")]
    assert buttons(desk.keyboards[-1]) == ["prop:start", "prop:meet"]
    assert ("assistant", content.text("proposal.offer"), "proposal") in desk.recorded
    assert desk.stages == ["respondido"]


async def test_choosing_the_proposal_starts_the_questions_without_offering_again(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    _triaged_as(monkeypatch, "proposal")

    await handler.process_message(CONN, update("quero a proposta por aqui"))

    assert desk.sent == [content.text("proposal.ask_quem")]
    assert buttons(desk.keyboards[-1]) == ["prop:skip", "prop:stop", "prop:team"]
    assert content.text("proposal.offer") not in desk.sent


async def test_choosing_to_be_served_by_the_ai_does_not_call_the_team(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    _no_model(monkeypatch)

    await handler.process_message(CONN, update("Quero ser atendido por vc msm"))

    assert desk.sent == [content.text("reply.stay_with_ai")]
    assert buttons(desk.keyboards[-1]) == ["prop:start"], "staying with the desk leads to the proposal"
    assert world.team == []
    assert ("assistant", content.text("reply.stay_with_ai"), "stay_with_ai") in desk.recorded
    assert desk.stages == ["respondido"]


async def test_the_welcome_offers_the_three_paths(monkeypatch: pytest.MonkeyPatch, desk, world):
    _no_model(monkeypatch)

    await handler.process_message(CONN, update("oi"))

    assert desk.sent == [content.text("reply.welcome")]
    assert buttons(desk.keyboards[-1]) == ["info:start", "prop:start", "prop:meet"]


async def test_more_information_opens_the_topics(desk, world):
    await handler.process_callback(CONN, tap("info:start"))

    assert desk.sent[-1] == content.text("reply.info_topics")


async def test_resolving_it_here_stays_with_the_desk(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    _no_model(monkeypatch)

    await handler.process_message(CONN, update("Prefiro resolver por aqui msm"))

    assert desk.sent == [content.text("reply.stay_with_ai")]
    assert world.team == []


async def test_meet_goes_to_the_times(monkeypatch: pytest.MonkeyPatch, desk, world):
    await handler.process_callback(CONN, tap("book:start"))
    await handler.process_callback(CONN, tap("prop:meet"))

    assert desk.sent[1] == desk.sent[0] == content.text("scheduling.period_question")
    assert buttons(desk.keyboards[1]) == buttons(desk.keyboards[0])
    assert buttons(desk.keyboards[1])[0].startswith("period:")


async def test_meet_lists_the_days_when_the_person_chooses_the_day(
        monkeypatch: pytest.MonkeyPatch, desk, world, choose):
    await handler.process_callback(CONN, tap("book:start"))
    await handler.process_callback(CONN, tap("prop:meet"))

    assert desk.sent[1] == desk.sent[0] == content.text("booking.days_prompt")


async def test_typed_answers_walk_the_questionnaire(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    await _answer_everything(monkeypatch, desk)

    assert desk.sent[0] == content.text("proposal.ask_quem")
    assert buttons(desk.keyboards[0]) == ["prop:skip", "prop:stop", "prop:team"]
    assert desk.sent[1] == content.text("proposal.ask_desafio")
    last = desk.sent[-1]
    for step in pb.STEPS:
        assert content.text(f"proposal.label_{step}") in last
    assert buttons(desk.keyboards[-1]) == ["prop:ok", "prop:fix"]
    b = _only(world)
    assert b.answers == WALKED and b.step is None
    assert b.status == "open", "nothing goes to the team before the person confirms"
    assert ("user", "planilhas", "proposal") in desk.recorded
    assert world.team == []


async def test_a_question_mid_questionnaire_is_answered_then_the_question_returns(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_message(CONN, update(ANSWERS[0]))
    _triaged_as(monkeypatch, "faq")

    await handler.process_message(CONN, update("vocês atendem aos sábados? "))

    assert desk.sent[-2] == "Claro."
    assert desk.sent[-1] == content.render(
        "proposal.resume", pergunta=content.text("proposal.ask_desafio"))
    assert buttons(desk.keyboards[-1]) == ["prop:skip", "prop:stop", "prop:team"]
    b = _only(world)
    assert b.answers == {"quem": ANSWERS[0]} and b.step == "desafio"

    _no_model(monkeypatch)
    await handler.process_message(CONN, update(ANSWERS[1]))
    assert _only(world).answers["desafio"] == ANSWERS[1]


async def test_a_hello_mid_questionnaire_brings_the_question_back(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    from core.repositories import contacts as contacts_repo
    from tests.test_desk_flow import _value

    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_message(CONN, update(ANSWERS[0]))
    monkeypatch.setattr(contacts_repo, "last_message_at",
                        lambda conn, chat_id: _value(datetime.now(ZONE)))

    await handler.process_message(CONN, update("boa tarde"))

    assert desk.sent[-1] == content.render("reply.hello_resume", retomada=content.render(
        "proposal.resume", pergunta=content.text("proposal.ask_desafio")))
    assert buttons(desk.keyboards[-1]) == ["prop:skip", "prop:stop", "prop:team"]
    b = _only(world)
    assert b.answers == {"quem": ANSWERS[0]} and b.step == "desafio", "the hello is no answer"


async def test_a_skip_leaves_the_answer_empty(monkeypatch: pytest.MonkeyPatch, desk, world):
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_callback(CONN, tap("prop:skip"))

    assert desk.sent[-1] == content.text("proposal.ask_desafio")
    assert _only(world).answers == {"quem": ""}


async def test_stop_cancels_and_offers_times(monkeypatch: pytest.MonkeyPatch, desk, world):
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_callback(CONN, tap("prop:stop"))

    assert _only(world).status == "cancelled"
    assert desk.sent[-1].startswith(content.text("proposal.stopped"))
    assert content.text("scheduling.period_question") in desk.sent[-1]
    assert buttons(desk.keyboards[-1])[0].startswith("period:")

    _triaged_as(monkeypatch, "faq")
    await handler.process_message(CONN, update("e o preço?"))
    assert desk.sent[-1] == "Claro.", "a cancelled questionnaire takes no more answers"


async def test_ok_with_email_submits(monkeypatch: pytest.MonkeyPatch, desk, world):
    await _answer_everything(monkeypatch, desk)

    await handler.process_callback(CONN, tap("prop:ok"))

    assert desk.sent[-1] == content.text("proposal.done")
    assert _only(world).status == "sent"
    assert len(world.team) == 1
    assert desk.pending is None


async def test_ok_without_email_asks_for_it_then_submits(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    desk.known = {}
    await _answer_everything(monkeypatch, desk)

    await handler.process_callback(CONN, tap("prop:ok"))

    assert desk.sent[-1] == content.render(
        "contact.ask_before_proposal", faltando=content.text("contact.need_both"))
    assert desk.pending == "proposal"
    assert _only(world).status == "open" and world.team == []

    await handler.process_message(CONN, update("ana@x.com"))

    assert desk.details == {"email": "ana@x.com"}
    assert desk.sent[-1] == f"{content.text('contact.noted')}\n\n{content.text('proposal.done')}"
    assert _only(world).status == "sent" and len(world.team) == 1
    assert desk.pending is None


async def test_phone_is_not_required(monkeypatch: pytest.MonkeyPatch, desk, world):
    desk.known = {"email": "ana@example.com"}
    await _answer_everything(monkeypatch, desk)

    await handler.process_callback(CONN, tap("prop:ok"))

    assert desk.sent[-1] == content.text("proposal.done")
    assert desk.pending is None and desk.requested is None
    assert _only(world).status == "sent"


async def test_only_a_phone_is_asked_for_the_email_once_then_left_waiting(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    desk.known = {}
    await _answer_everything(monkeypatch, desk)
    await handler.process_callback(CONN, tap("prop:ok"))

    await handler.process_message(CONN, update("91 90000-0001"))

    assert desk.sent[-1] == content.text("contact.ask_email")
    assert desk.pending == "proposal:email"
    assert _only(world).status == "open"

    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update("prefiro não passar"))

    assert desk.sent[-2] == "Claro."
    assert desk.sent[-1] == content.text("proposal.waiting_email")
    assert buttons(desk.keyboards[-1]) == ["prop:ok", "prop:fix"]
    assert desk.pending is None
    assert _only(world).status == "open" and world.team == []

    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update("tudo certo então"))
    assert desk.sent[-1] == "Claro.", "not asked again on every message"


async def test_someone_already_asked_is_asked_for_the_email_alone(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    desk.known = {"phone": "+55 91 90000-0001"}
    desk.requested = datetime.now(ZONE)
    await _answer_everything(monkeypatch, desk)

    await handler.process_callback(CONN, tap("prop:ok"))

    assert desk.sent[-1] == content.render(
        "contact.ask_before_proposal", faltando=content.text("contact.need_email"))
    assert desk.pending == "proposal:email"

    await handler.process_message(CONN, update("ana@x.com"))
    assert desk.sent[-1].endswith(content.text("proposal.done"))
    assert _only(world).status == "sent"


async def test_fix_menu_and_correction(monkeypatch: pytest.MonkeyPatch, desk, world):
    await _answer_everything(monkeypatch, desk)

    await handler.process_callback(CONN, tap("prop:fix"))
    assert desk.sent[-1] == content.text("proposal.fix_which")
    assert buttons(desk.keyboards[-1]) == [f"prop:fix:{s}" for s in pb.STEPS]

    await handler.process_callback(CONN, tap("prop:fix:nada"))
    assert desk.sent[-1] == content.text("proposal.fix_which")
    assert _only(world).step is None

    await handler.process_callback(CONN, tap("prop:fix:prazo"))
    assert desk.sent[-1] == content.text("proposal.ask_prazo")
    assert _only(world).step == "prazo"

    await handler.process_message(CONN, update("abril"))
    assert f"{content.text('proposal.label_prazo')}: abril" in desk.sent[-1]
    assert buttons(desk.keyboards[-1]) == ["prop:ok", "prop:fix"]
    assert _only(world).answers["prazo"] == "abril"


async def test_a_proposal_request_mid_questionnaire_does_not_start_over(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    await _answer_everything(monkeypatch, desk)
    _triaged_as(monkeypatch, "proposal")

    await handler.process_message(CONN, update("e a proposta?"))

    assert buttons(desk.keyboards[-1]) == ["prop:ok", "prop:fix"]
    assert len(world.rows) == 1


async def test_an_old_button_with_no_questionnaire_offers_both_paths_again(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    await handler.process_callback(CONN, tap("prop:skip"))

    assert desk.sent == [content.text("proposal.offer")]
    assert world.rows == {}



async def _two_answers_then(monkeypatch: pytest.MonkeyPatch, intent, text: str) -> None:
    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_message(CONN, update(ANSWERS[0]))
    await handler.process_message(CONN, update(ANSWERS[1]))
    _triaged_as(monkeypatch, intent, "other")
    await handler.process_message(CONN, update(text))


async def test_a_booking_mid_questionnaire_pauses_it(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    await _two_answers_then(monkeypatch, "booking", "dá para marcar uma conversa amanhã?")

    assert "tenho" in desk.sent[-1] and buttons(desk.keyboards[-1])[0].startswith("slot:")
    resume = content.text("proposal.resume").split("{")[0]
    assert not any(s.startswith(resume) for s in desk.sent)
    b = _only(world)
    assert b.status == "open" and b.step is None
    assert b.answers == {"quem": ANSWERS[0], "desafio": ANSWERS[1]}

    await handler.process_message(CONN, update("obrigada"))
    assert _only(world).answers == {"quem": ANSWERS[0], "desafio": ANSWERS[1]}
    assert ("user", "obrigada", "proposal") not in desk.recorded


async def test_a_request_for_a_person_mid_questionnaire_pauses_it(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    monkeypatch.setattr(settings, "team_chat_id", "-100123")

    async def fake_route(body, chat_id, history_text="", organization="", retrieve=None):
        intent = "escalation" if body.endswith("?") else "other"
        return (Triage(intent=intent, confidence=0.9),
                AgentReply(text="Claro.", intent=intent, needs_human=intent == "escalation"))

    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_message(CONN, update(ANSWERS[0]))
    monkeypatch.setattr(handler, "route", fake_route)
    await handler.process_message(CONN, update("posso falar com uma pessoa?"))

    resume = content.text("proposal.resume").split("{")[0]
    assert not any(s.startswith(resume) for s in desk.sent)
    assert _only(world).step is None

    await handler.process_message(CONN, update("ok"))
    assert _only(world).answers == {"quem": ANSWERS[0]}


async def test_asking_for_the_proposal_again_resumes_where_it_stopped(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    await _two_answers_then(monkeypatch, "booking", "dá para marcar uma conversa amanhã?")
    _triaged_as(monkeypatch, "proposal")

    await handler.process_message(CONN, update("prefiro a proposta por e-mail"))

    assert desk.sent[-1].startswith(content.render(
        "proposal.resume", pergunta=content.text("proposal.ask_biz_areas")))
    assert _only(world).step == "areas"

    _no_model(monkeypatch)
    await handler.process_message(CONN, update("vendas"))  # a typed name picks it and moves on
    assert _only(world).answers["areas"] == "Vendas"
    await handler.process_message(CONN, update(ANSWERS[2]))
    assert _only(world).answers["custo_hoje"] == ANSWERS[2]
    assert desk.sent[-1].startswith(content.text("proposal.ask_canais_lista"))


# ── Leaving the questionnaire by a tap or a fixed phrase ─────────────────────

def _offer_slots(desk) -> list[str]:
    return [b for b in buttons(desk.keyboards[-1]) if b.startswith("slot:")]


async def test_meet_tap_mid_questionnaire_pauses_it(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    """The final review's reproduction: "Conversar 30 min" from the earlier
    offer, then the period typed and a thank-you — none of it is an answer."""
    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_message(CONN, update(ANSWERS[0]))
    await handler.process_callback(CONN, tap("prop:meet"))
    assert desk.sent[-1] == content.text("scheduling.period_question")
    b = _only(world)
    assert b.step is None and b.status == "open" and b.answers == {"quem": ANSWERS[0]}

    _triaged_as(monkeypatch, "other", "other")
    await handler.process_message(CONN, update("de manhã"))
    await handler.process_message(CONN, update("obrigada"))

    b = _only(world)
    assert b.answers == {"quem": ANSWERS[0]} and b.step is None
    assert ("user", "obrigada", "proposal") not in desk.recorded


async def test_book_start_tap_mid_questionnaire_pauses_it(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_message(CONN, update(ANSWERS[0]))
    await handler.process_callback(CONN, tap("book:start"))

    b = _only(world)
    assert b.step is None and b.answers == {"quem": ANSWERS[0]}


async def test_a_typed_slot_choice_after_meet_books_instead_of_answering(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_message(CONN, update(ANSWERS[0]))
    await handler.process_callback(CONN, tap("prop:meet"))
    await handler.process_callback(CONN, tap("period:any:none:none"))
    assert _offer_slots(desk), desk.sent[-1]

    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update("a primeira"))

    assert len(desk.appointments) == 1
    assert _only(world).answers == {"quem": ANSWERS[0]}


async def test_typed_text_with_a_live_offer_is_not_an_answer(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    """An offer on the table and a question pending at once: the typed slot
    goes to the offer, and the questionnaire pauses."""
    from core.models import Offer
    from core.repositories import offers as offers_repo
    from tests.test_desk_flow import OFFERED

    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_message(CONN, update(ANSWERS[0]))
    await offers_repo.save(CONN, Offer(chat_id="42", calendar_id="desk@example.com", slots=OFFERED), 30)
    assert _only(world).step == "desafio"

    _triaged_as(monkeypatch, "other")
    await handler.process_message(CONN, update("a primeira"))

    assert len(desk.appointments) == 1
    b = _only(world)
    assert b.answers == {"quem": ANSWERS[0]} and b.step is None


async def test_choosing_the_questionnaire_drops_the_times_on_the_table(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    from core.repositories import offers as offers_repo

    await handler.process_callback(CONN, tap("book:start"))
    await handler.process_callback(CONN, tap("period:any:none:none"))
    assert await offers_repo.pending_for_chat(CONN, 42) is not None

    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_message(CONN, update(ANSWERS[0]))

    assert await offers_repo.pending_for_chat(CONN, 42) is None
    assert _only(world).answers == {"quem": ANSWERS[0]}


async def test_text_after_the_team_button_is_not_an_answer(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    """The handoff button asks for the details first; the e-mail typed next
    is for the handoff, not the pending question."""
    monkeypatch.setattr(settings, "team_chat_id", "-100123")
    desk.known = {}
    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_message(CONN, update(ANSWERS[0]))
    await handler.process_callback(CONN, tap("handoff"))
    assert desk.pending == "escalation:botao"
    assert _only(world).step is None

    await handler.process_message(CONN, update("ana@x.com"))

    assert desk.details == {"email": "ana@x.com"}
    assert _only(world).answers == {"quem": ANSWERS[0]}
    assert desk.sent[-1].endswith(content.text("reply.escalation"))


async def test_every_question_offers_the_team(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    await handler.process_callback(CONN, tap("prop:start"))

    assert buttons(desk.keyboards[-1]) == ["prop:skip", "prop:stop", "prop:team"]


async def test_the_team_button_pauses_and_calls_the_team(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    monkeypatch.setattr(settings, "team_chat_id", "-100123")
    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_message(CONN, update(ANSWERS[0]))

    await handler.process_callback(CONN, tap("prop:team"))

    assert desk.sent[-1] == content.text("reply.escalation")
    assert len(desk.sessions) == 1, "the same handoff as the team button"
    b = _only(world)
    assert b.step is None and b.status == "open" and b.answers == {"quem": ANSWERS[0]}


async def test_the_team_button_asks_for_details_like_the_handoff_button(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    monkeypatch.setattr(settings, "team_chat_id", "-100123")
    desk.known = {}
    await handler.process_callback(CONN, tap("prop:start"))

    await handler.process_callback(CONN, tap("prop:team"))

    assert desk.pending == "escalation:botao"
    assert _only(world).step is None


async def test_a_typed_request_for_a_person_is_not_stored_as_an_answer(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    monkeypatch.setattr(settings, "team_chat_id", "-100123")
    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_message(CONN, update(ANSWERS[0]))

    await handler.process_message(CONN, update("quero falar com um atendente"))

    b = _only(world)
    assert b.answers == {"quem": ANSWERS[0]} and b.step is None
    assert desk.sent[-1] == content.text("reply.escalation")
    assert len(desk.sessions) == 1


async def test_a_form_lead_picks_what_to_automate_by_tapping(
        monkeypatch: pytest.MonkeyPatch, desk, world):
    """From the form's "Pedir proposta": the clinic's questions, the areas by
    buttons, and what was tapped on record for whoever reads the conversation."""
    from core.repositories import contacts as contacts_repo
    from tests.test_proposal_briefing import FORM_HISTORY

    _no_model(monkeypatch)
    user_line, _, assistant_line = FORM_HISTORY.partition("\nassistant: ")

    async def history(conn, chat_id, limit=10):
        return [("user", user_line.removeprefix("user: ")), ("assistant", assistant_line)]
    monkeypatch.setattr(contacts_repo, "recent_history", history)

    await handler.process_callback(CONN, tap("prop:start"))
    assert desk.sent[-1].startswith(content.text("proposal.ask_areas"))

    await handler.process_callback(CONN, tap("prop:pick:areas:triagem", "cb2"))
    await handler.process_callback(CONN, tap("prop:area:receitas", "cb3"))  # an older message
    assert desk.sent[-1].startswith(content.render("proposal.areas_chosen",
                                           lista="Pré-triagem, Receitas e documentos"))
    assert buttons(desk.keyboards[-1])[-1] == "prop:done:areas"

    await handler.process_callback(CONN, tap("prop:areas:done", "cb4"))
    assert desk.sent[-1] == content.render("proposal.ask_como_hoje_areas",
                                           areas="pré-triagem, receitas e documentos")
    assert _only(world).answers["areas"] == "Pré-triagem, Receitas e documentos"


# ── WhatsApp: the Flow, and what happens when WhatsApp refuses it ───────────

@pytest.fixture
def wa_sends(monkeypatch: pytest.MonkeyPatch):
    """The chat is a WhatsApp chat; what goes out is recorded. A Flow is
    refused when `refuse_flow` is set."""
    from core import channels

    state = {"sent": [], "refuse_flow": False}

    async def send(chat_id, text, keyboard=None, parse_mode="Markdown"):
        flow = (keyboard or {}).get("_flow")
        state["sent"].append((text, keyboard))
        return not (flow and state["refuse_flow"])

    monkeypatch.setattr(channels, "send", send)
    monkeypatch.setattr(channels, "is_whatsapp", lambda chat_id: True)
    return state


async def test_on_whatsapp_the_proposal_starts_with_the_flow(
        monkeypatch: pytest.MonkeyPatch, desk, world, wa_sends):
    _flows(monkeypatch)
    _no_model(monkeypatch)

    await handler.process_callback(CONN, tap("prop:start"))
    assert wa_sends["sent"][-1][0] == content.text("proposal.ask_ramo")
    await handler.process_callback(CONN, tap("prop:ramo:comercio", "cb2"))

    text, keyboard = wa_sends["sent"][-1]
    assert text == content.text("proposal.flow_intro") and keyboard["_flow"]["flow_id"] == "F-EMP"
    assert keyboard["_flow"]["data"]["ramo"] == content.text("proposal.ramo_comercio")
    assert _only(world).step is None

    _triaged_as(monkeypatch, "faq")
    await handler.process_message(CONN, update("vocês atendem aos sábados?"))
    answered = {k: v for k, v in _only(world).answers.items() if k in pb.ALL_STEPS}
    assert answered == {}, "a typed message while the form is open is not an answer"


async def test_a_refused_flow_falls_back_to_the_questions(
        monkeypatch: pytest.MonkeyPatch, desk, world, wa_sends):
    _flows(monkeypatch)
    _no_model(monkeypatch)
    wa_sends["refuse_flow"] = True

    await handler.process_callback(CONN, tap("prop:start"))
    await handler.process_callback(CONN, tap("prop:ramo:comercio", "cb2"))

    assert [t for t, _ in wa_sends["sent"]][-2:] == [
        content.text("proposal.flow_intro"), content.text("proposal.ask_quem_sem_ramo")]
    assert _only(world).step == "quem"
    await handler.process_message(CONN, update("Ana, Loja Sol, dona"))
    assert _only(world).answers["quem"] == (
        f"Ana, Loja Sol, dona · {content.text('proposal.ramo_comercio')}")


async def test_a_refused_flow_after_a_typed_line_of_business_falls_back_too(
        monkeypatch: pytest.MonkeyPatch, desk, world, wa_sends):
    _flows(monkeypatch)
    _no_model(monkeypatch)
    wa_sends["refuse_flow"] = True
    await handler.process_callback(CONN, tap("prop:start"))

    await handler.process_message(CONN, update("padaria"))

    assert wa_sends["sent"][-1][0] == content.text("proposal.ask_quem_sem_ramo")
    assert _only(world).step == "quem"


def _flows(monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    import tempfile
    from pathlib import Path

    from core.services import forms

    folder = Path(tempfile.mkdtemp()) / "forms"
    shutil.copytree(Path(__file__).resolve().parent.parent / "content" / "forms", folder)
    (folder / "flows.toml").write_text(
        '[proposta_atendimento]\nflow_id = "F-EMP"\n[proposta_clinica]\nflow_id = "F-CLI"\n',
        encoding="utf-8")
    monkeypatch.setattr(forms, "_dir", lambda: folder)
    forms.reset()


def _flow_answer(token: str, answers: dict) -> dict:
    """The Flow's answer as Meta's webhook sends it, through the real converter."""
    import json

    from core.integrations.whatsapp_updates import to_updates

    reply = {"type": "nfm_reply", "nfm_reply": {
        "name": "flow", "body": "Sent",
        "response_json": json.dumps({"flow_token": token, **answers})}}
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "W", "changes": [{
        "field": "messages", "value": {
            "metadata": {"phone_number_id": "PN1"},
            "contacts": [{"profile": {"name": "Ana"}, "wa_id": "5591999990000"}],
            "messages": [{"from": "5591999990000", "id": "wamid.f1",
                          "timestamp": str(int(datetime.now().timestamp())),
                          "type": "interactive", "interactive": reply}]}}]}]}
    update, = to_updates(payload, "PN1")
    return update


async def test_the_flow_answer_goes_to_the_summary_and_then_to_the_team(
        monkeypatch: pytest.MonkeyPatch, desk, world, wa_sends):
    from core.repositories import proposal_briefings as briefings_repo
    from tests.test_proposal_briefing import COMPANY_ANSWER

    async def get(conn, briefing_id):
        return world.rows.get(briefing_id)

    monkeypatch.setattr(briefings_repo, "get", get)
    _flows(monkeypatch)
    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))

    await handler.process_callback(CONN, _flow_answer("proposta:1", dict(COMPANY_ANSWER)))

    text, keyboard = wa_sends["sent"][-1]
    assert content.text("proposal.label_pagamento") in text
    assert buttons(keyboard) == ["prop:ok", "prop:fix"]
    assert ("user", f"(tocou no botão) {content.text('forms.sent_label')}", "proposal") \
        in desk.recorded

    await handler.process_callback(CONN, tap("prop:ok", "cb9"))

    assert _only(world).status == "sent" and len(world.team) == 1
    assert wa_sends["sent"][-1][0] == content.text("proposal.done")


async def test_a_typed_line_of_business_sends_the_clinics_flow(
        monkeypatch: pytest.MonkeyPatch, desk, world, wa_sends):
    _flows(monkeypatch)
    _no_model(monkeypatch)
    await handler.process_callback(CONN, tap("prop:start"))

    await handler.process_message(CONN, update("sou dentista, tenho um consultório"))

    text, keyboard = wa_sends["sent"][-1]
    assert text == content.text("proposal.flow_intro") and keyboard["_flow"]["flow_id"] == "F-CLI"
