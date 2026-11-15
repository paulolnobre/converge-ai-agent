"""The proposal-by-e-mail questionnaire: what is asked, in what order, what is
shown back and that the team hears once. The repository is an in-memory fake."""
import json
from datetime import datetime

import pytest

from core import content
from core.integrations import llm
from core.models import Contact, ProposalBriefing
from core.repositories import proposal_briefings as repo
from core.services import inbox, pricing
from core.services import proposal_briefing as pb

NOW = datetime(2026, 10, 31, 12, 0)
STEPS = ("quem", "desafio", "areas", "custo_hoje", "canais_lista", "volume", "sistemas_lista",
         "processo", "usuarios_faixa", "pagamento", "prazo")
CLINIC_STEPS = ("quem", "areas", "como_hoje", "sistemas_lista", "equipe", "canais_lista", "volume",
                "processo", "usuarios_faixa", "pagamento", "prazo")


async def answer_all(world, reply: str = "r") -> tuple[str, dict | None]:
    """Answer every question left: a typed reply, then "pronto" where several
    options are picked (a typed reply there is one more option)."""
    text, kb = "", None
    while world.rows[1].step is not None:
        step = world.rows[1].step
        text, kb = await pb.answer(None, CONTACT, world.rows[1], reply)
        if step in pb.MULTI and world.rows[1].step == step:
            text, kb = await pb.answer(None, CONTACT, world.rows[1], "pronto")
    return text, kb


class World:
    def __init__(self):
        self.rows: dict[int, ProposalBriefing] = {}
        self.notified: set[tuple[int, str]] = set()
        self.team: list[str] = []
        self.team_ok = True
        self.llm_result: object = None


@pytest.fixture
def world(monkeypatch):
    w = World()

    async def open_for(conn, contact_id):
        return next((b for b in w.rows.values() if b.contact_id == contact_id
                     and b.status in ("open", "incomplete_sent")), None)

    async def create(conn, contact_id, answers, prefilled, step):
        b = ProposalBriefing(id=len(w.rows) + 1, contact_id=contact_id, status="open", step=step,
                             answers=dict(answers), prefilled=list(prefilled),
                             created_at=NOW, updated_at=NOW)
        w.rows[b.id] = b
        return b

    async def save(conn, briefing_id, *, answers, step, prefilled=None):
        update: dict = {"answers": dict(answers), "step": step}
        if prefilled is not None:
            update["prefilled"] = list(prefilled)
        w.rows[briefing_id] = w.rows[briefing_id].model_copy(update=update)

    async def set_status(conn, briefing_id, status):
        w.rows[briefing_id] = w.rows[briefing_id].model_copy(update={"status": status})

    async def mark_notified(conn, briefing_id, kind):
        if (briefing_id, kind) in w.notified:
            return False
        w.notified.add((briefing_id, kind))
        return True

    async def notify_team(text, contact_id):
        w.team.append(text)
        return w.team_ok

    async def complete_json(system, user, **kw):
        return w.llm_result

    for name, fn in [("open_for", open_for), ("create", create), ("save", save),
                     ("set_status", set_status), ("mark_notified", mark_notified)]:
        monkeypatch.setattr(repo, name, fn)
    monkeypatch.setattr(inbox, "notify_team", notify_team)
    monkeypatch.setattr(inbox, "push_to_team", lambda text, contact: None)
    monkeypatch.setattr(llm, "complete_json", complete_json)
    return w


CONTACT = Contact(id=7, chat_id="c1", channel="telegram", name="Ana")
FOUR = {"nome": "Ana", "empresa": "Sol Engenharia", "ramo": "construção", "cargo": "dona"}


async def test_prefill_keeps_only_known_keys(world):
    world.llm_result = {"nome": "Ana", "extra": "x", "empresa": 123, "ramo": "a" * 500}
    got = await pb.prefill("conversa")
    assert got == {"nome": "Ana", "ramo": "a" * 300}


async def test_prefill_failure_fills_nothing(world):
    world.llm_result = None
    assert await pb.prefill("conversa") == {}


async def test_prefill_exception_fills_nothing(monkeypatch, world):
    async def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(llm, "complete_json", boom)
    assert await pb.prefill("conversa") == {}


def test_prefilled_answers_quem_only_with_all_four():
    assert pb.prefilled_answers(FOUR) == {"quem": "Ana · Sol Engenharia · construção · dona"}
    assert pb.prefilled_answers({**FOUR, "desafio": "agenda"})["desafio"] == "agenda"
    partial = {k: v for k, v in FOUR.items() if k != "cargo"}
    assert "quem" not in pb.prefilled_answers(partial)


async def test_a_clinic_already_named_skips_nothing_but_desafio_when_only_desafio_is_known(world):
    world.llm_result = {"desafio": "agenda"}
    text, kb = await pb.start(None, CONTACT, "oi")
    b = world.rows[1]
    assert b.answers == {"desafio": "agenda"} and b.prefilled == ["desafio"]
    assert b.step == "quem"
    assert text == content.text("proposal.ask_quem")


async def test_quem_is_skipped_only_when_all_four_are_known(world):
    world.llm_result = dict(FOUR)
    text, _ = await pb.start(None, CONTACT, "oi")
    assert world.rows[1].step == "desafio"
    assert text == content.text("proposal.ask_desafio")


async def test_start_with_everything_known_goes_to_confirmation(world, monkeypatch):
    monkeypatch.setattr(pb, "STEPS", ("quem",))
    world.llm_result = dict(FOUR)
    text, kb = await pb.start(None, CONTACT, "oi")
    assert world.rows[1].step is None
    assert content.text("proposal.button_ok") in str(kb)


def test_next_step_is_first_missing():
    assert pb.next_step({}) == "quem"
    assert pb.next_step({"quem": "x", "desafio": ""}) == "areas"
    assert pb.next_step({s: "" for s in STEPS}) is None


def test_question_has_skip_stop_and_team():
    text, kb = pb.question("custo_hoje")
    assert text == content.text("proposal.ask_custo_hoje")
    assert kb["inline_keyboard"] == [
        [{"text": content.text("proposal.button_skip"), "callback_data": "prop:skip"}],
        [{"text": content.text("proposal.button_stop"), "callback_data": "prop:stop"}],
        [{"text": content.text("proposal.button_team"), "callback_data": "prop:team"}]]


async def test_offer_buttons():
    text, kb = await pb.offer()
    assert text == content.text("proposal.offer")
    data = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert data == ["prop:start", "prop:meet"], "the proposal comes first"


async def test_each_answer_leads_to_the_next_question(world):
    await pb.start(None, CONTACT, "oi")
    b = world.rows[1]
    text, _ = await pb.answer(None, CONTACT, b, "Ana, Clínica")
    assert world.rows[1].answers == {"quem": "Ana, Clínica"}
    assert world.rows[1].step == "desafio"
    assert text == content.text("proposal.ask_desafio")


async def test_skip_records_empty_and_moves_on(world):
    await pb.start(None, CONTACT, "oi")
    text, _ = await pb.skip(None, CONTACT, world.rows[1])
    assert world.rows[1].answers == {"quem": ""}
    assert text == content.text("proposal.ask_desafio")


async def test_last_answer_shows_the_confirmation(world):
    await pb.start(None, CONTACT, "oi")
    text, kb = await answer_all(world)
    assert kb is not None
    assert world.rows[1].step is None
    assert text.startswith(content.render("proposal.confirm", resumo="").strip()[:10])
    data = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert data == ["prop:ok", "prop:fix"]


def test_summary_is_exactly_what_was_written():
    raw = "Olá — açaí 🚀\nsegunda linha"
    b = ProposalBriefing(id=1, contact_id=7, status="open", created_at=NOW, updated_at=NOW,
                         answers={"quem": "Ana · X · y · z", "desafio": raw, "prazo": ""},
                         prefilled=["quem"])
    lines = pb.summary(b).split("\n", 1)[0]
    assert lines == f"{content.text('proposal.label_quem')}: Ana · X · y · z " \
                    f"{content.text('proposal.prefilled_mark')}"
    assert f"{content.text('proposal.label_desafio')}: {raw}" in pb.summary(b)
    assert content.text("proposal.label_prazo") not in pb.summary(b)


async def test_correct_repeats_only_that_question(world):
    await pb.start(None, CONTACT, "oi")
    await answer_all(world)
    text, kb = await pb.correct(None, world.rows[1], "prazo")
    assert world.rows[1].step == "prazo" and "prazo" not in world.rows[1].answers
    assert text == content.text("proposal.ask_prazo")
    text, _ = await pb.answer(None, CONTACT, world.rows[1], "novo")
    assert world.rows[1].step is None and world.rows[1].answers["prazo"] == "novo"


async def test_correcting_a_multi_choice_starts_it_empty(world):
    await pb.start(None, CONTACT, "oi")
    await answer_all(world)
    text, kb = await pb.correct(None, world.rows[1], "canais_lista")
    assert world.rows[1].step == "canais_lista" and "canais_lista" not in world.rows[1].answers
    assert text.startswith(content.text("proposal.ask_canais_lista"))
    await pb.pick(None, world.rows[1], "canais_lista", "site")
    await pb.pick_done(None, world.rows[1], "canais_lista")
    assert world.rows[1].answers["canais_lista"] == "Site" and world.rows[1].step is None


def test_fix_menu_one_row_per_step():
    b = ProposalBriefing(id=1, contact_id=7, status="open", created_at=NOW, updated_at=NOW)
    text, kb = pb.fix_menu(b)
    assert text == content.text("proposal.fix_which")
    assert [r[0]["callback_data"] for r in kb["inline_keyboard"]] == [f"prop:fix:{s}" for s in STEPS]


async def test_asking_again_resumes_the_open_briefing(world):
    await pb.start(None, CONTACT, "oi")
    await pb.answer(None, CONTACT, world.rows[1], "Ana")
    text, _ = await pb.start(None, CONTACT, "oi")
    assert len(world.rows) == 1
    assert text == content.render("proposal.resume", pergunta=content.text("proposal.ask_desafio"))


async def test_submit_tells_the_team_once(world):
    await pb.start(None, CONTACT, "oi")
    b = world.rows[1]
    assert await pb.submit(None, CONTACT, b) == content.text("proposal.done")
    await pb.submit(None, CONTACT, world.rows[1])
    assert len(world.team) == 1 and world.rows[1].status == "sent"
    assert "Ana" in world.team[0]


async def test_submit_without_team_group_still_records(world):
    world.team_ok = False
    await pb.start(None, CONTACT, "oi")
    text = await pb.submit(None, CONTACT, world.rows[1])
    assert world.rows[1].status == "sent" and text == content.text("proposal.done")


async def test_submit_after_incomplete_notice_still_sends(world):
    await pb.start(None, CONTACT, "oi")
    assert await pb.notify_incomplete(None, CONTACT, world.rows[1]) is True
    assert await pb.notify_incomplete(None, CONTACT, world.rows[1]) is False
    await repo.set_status(None, 1, "incomplete_sent")
    await pb.submit(None, CONTACT, world.rows[1])
    assert len(world.team) == 2 and world.rows[1].status == "sent"


async def test_stop_cancels(world):
    await pb.start(None, CONTACT, "oi")
    await pb.stop(None, world.rows[1])
    assert world.rows[1].status == "cancelled"


async def test_answers_over_1000_chars_are_cut(world):
    await pb.start(None, CONTACT, "oi")
    await pb.answer(None, CONTACT, world.rows[1], "x" * 1500)
    assert world.rows[1].answers["quem"] == "x" * 1000 + "…"
    await pb.answer(None, CONTACT, world.rows[1], "y" * 1000)
    assert world.rows[1].answers["desafio"] == "y" * 1000


async def test_resuming_an_incomplete_sent_briefing_keeps_its_status(world):
    await pb.start(None, CONTACT, "oi")
    await pb.answer(None, CONTACT, world.rows[1], "Ana")
    await repo.set_status(None, 1, "incomplete_sent")
    text, _ = await pb.start(None, CONTACT, "oi")
    assert world.rows[1].status == "incomplete_sent"
    assert text == content.render("proposal.resume", pergunta=content.text("proposal.ask_desafio"))
    await pb.submit(None, CONTACT, world.rows[1])
    assert world.rows[1].status == "sent"


async def test_a_corrected_or_overwritten_answer_is_no_longer_prefilled(world):
    world.llm_result = {**FOUR, "desafio": "agenda"}
    await pb.start(None, CONTACT, "oi")
    assert world.rows[1].prefilled == ["quem", "desafio"]
    await answer_all(world)
    assert world.rows[1].step is None
    await pb.correct(None, world.rows[1], "desafio")
    assert world.rows[1].prefilled == ["quem"]
    await pb.answer(None, CONTACT, world.rows[1], "novo")
    await pb.correct(None, world.rows[1], "quem")
    assert world.rows[1].prefilled == []


@pytest.fixture
def sweep_world(world, monkeypatch):
    """The fake repo plus `abandoned` (open rows) and a contact lookup."""
    contacts = {7: CONTACT}

    async def abandoned(conn, older_than):
        return [b for b in world.rows.values()
                if b.status == "open" and (b.id, "incomplete") not in world.notified]

    async def get_by_id(conn, contact_id):
        return contacts.get(contact_id)

    async def close_abandoned(conn, briefing_id, older_than):
        b = world.rows[briefing_id]
        if b.status != "open" or (briefing_id, "incomplete") in world.notified:
            return False
        world.rows[briefing_id] = b.model_copy(update={"status": "incomplete_sent"})
        return True

    monkeypatch.setattr(repo, "abandoned", abandoned)
    monkeypatch.setattr(repo, "close_abandoned", close_abandoned)
    monkeypatch.setattr(pb.contacts_repo, "get_by_id", get_by_id)
    world.contacts = contacts
    return world


async def test_abandoned_after_24h_tells_the_team_once_as_incomplete(sweep_world):
    w = sweep_world
    await pb.start(None, CONTACT, "oi")
    await pb.answer(None, CONTACT, w.rows[1], "Ana")
    assert await pb.sweep_abandoned(None) == 1
    assert w.rows[1].status == "incomplete_sent"
    assert len(w.team) == 1
    assert await pb.sweep_abandoned(None) == 0
    assert len(w.team) == 1


async def test_coming_back_after_incomplete_continues(sweep_world):
    w = sweep_world
    await pb.start(None, CONTACT, "oi")
    await pb.answer(None, CONTACT, w.rows[1], "Ana")
    await pb.sweep_abandoned(None)
    text, _ = await pb.answer(None, CONTACT, w.rows[1], "Falta cliente")
    assert w.rows[1].answers["desafio"] == "Falta cliente" and w.rows[1].status == "incomplete_sent"
    assert text.startswith(content.text("proposal.ask_biz_areas"))
    await pb.submit(None, CONTACT, w.rows[1])
    assert w.rows[1].status == "sent"
    assert len(w.team) == 2  # one incomplete, one sent; no second incomplete


async def test_status_is_set_even_when_the_notice_is_not_sent(sweep_world):
    w = sweep_world
    w.team_ok = False
    await pb.start(None, CONTACT, "oi")
    assert await pb.sweep_abandoned(None) == 0
    assert w.rows[1].status == "incomplete_sent"


async def test_a_row_whose_contact_is_gone_is_still_closed(sweep_world):
    w = sweep_world
    await pb.start(None, CONTACT, "oi")
    w.contacts.clear()
    assert await pb.sweep_abandoned(None) == 0
    assert w.rows[1].status == "incomplete_sent" and w.team == []


async def test_a_row_changed_after_listing_is_left_alone(sweep_world, monkeypatch):
    w = sweep_world
    await pb.start(None, CONTACT, "oi")
    stale = w.rows[1]

    async def listed(conn, older_than):
        return [stale]

    monkeypatch.setattr(repo, "abandoned", listed)
    await repo.set_status(None, 1, "sent")  # submitted after the listing
    assert await pb.sweep_abandoned(None) == 0
    assert w.rows[1].status == "sent" and w.team == []


async def test_team_notice_with_no_company_has_no_empty_parentheses(world):
    await pb.start(None, CONTACT, "oi")
    await pb.submit(None, CONTACT, world.rows[1])
    await pb.notify_incomplete(None, CONTACT, world.rows[1])
    assert len(world.team) == 2
    for text in world.team:
        assert "()" not in text and " · \n" not in text and not text.rstrip().endswith("·")
    assert world.team[0].startswith("📝 Ana pediu proposta por e-mail\n")


async def test_team_notice_names_the_company_when_known(world):
    contact = CONTACT.model_copy(update={"organization": "Clínica Sol"})
    await pb.start(None, contact, "oi")
    await pb.submit(None, contact, world.rows[1])
    assert world.team[0].startswith("📝 Ana pediu proposta por e-mail · Clínica Sol\n")


@pytest.mark.parametrize("value", [
    "null", "None", "NULL", "não informado", "Nao Informado", "NÃO INFORMADO", "n/a", "N/A",
    "-", "   ", " null "])
async def test_prefill_drops_null_like_values(world, value):
    world.llm_result = {"nome": value, "empresa": "Clínica Sol"}
    assert await pb.prefill("conversa") == {"empresa": "Clínica Sol"}


@pytest.mark.parametrize("body", [
    "quero falar com um atendente", "Quero falar com uma pessoa", "posso falar com alguém",
    "falar com alguem da equipe", "quero um humano", "uma pessoa de verdade, por favor",
    "QUERO FALAR COM ATENDENTE", "atendimento humano", "me chama um atendente",
    "me passa pra um atendente", "gostaria de falar com uma pessoa"])
def test_a_typed_request_for_a_person_is_read_by_code(body):
    assert pb.wants_a_person(body)


@pytest.mark.parametrize("body", [
    "Ana, Clínica Sol, saúde, dona", "uma atendente o dia todo", "recursos humanos",
    "duas pessoas", "WhatsApp e telefone", "a equipe toda, umas cinco pessoas",
    "queria um atendente virtual no whatsapp", "quero um atendente virtual",
    "os clientes querem falar com uma pessoa e ninguém responde",
    "o cliente liga pra falar com alguém da equipe", "preciso de um atendente só pra isso",
    "quero um atendente automático", "quero um atendente com IA"])
def test_an_ordinary_answer_is_not_a_request_for_a_person(body):
    assert not pb.wants_a_person(body)


@pytest.mark.parametrize("body", [
    "quero a proposta por aqui", "prefiro receber a proposta por e-mail",
    "pode ser a proposta por aqui mesmo", "me manda uma proposta",
    "quero pedir um orçamento", "Quero uma proposta"])
def test_choosing_the_proposal_is_read_by_code(body):
    assert pb.chose_the_proposal(body)


@pytest.mark.parametrize("body", [
    "quanto custa para contratar vocês", "qual o valor?", "vocês fazem proposta?",
    "proposta de quê?", "quero marcar uma conversa", "o que é um orçamento?"])
def test_a_price_question_is_not_a_choice_of_the_proposal(body):
    assert not pb.chose_the_proposal(body)


@pytest.mark.parametrize("body", [
    "Quero ser atendido por vc msm", "quero ser atendida por você mesma",
    "prefiro continuar com você", "quero falar com você", "pode seguir por aqui mesmo",
    "falar com vc mesmo", "quero continuar por aqui"])
def test_choosing_to_be_served_by_the_ai_is_read_by_code(body):
    assert pb.chose_the_ai(body)


@pytest.mark.parametrize("body", [
    "quero falar com um atendente", "quero falar com uma pessoa", "me passa pra um atendente",
    "quero um humano", "quero a proposta por aqui mesmo", "prefiro receber o orçamento por aqui",
    "quero marcar uma conversa", "como funciona o atendimento por você?"])
def test_asking_for_a_person_or_the_proposal_is_not_choosing_the_ai(body):
    assert not pb.chose_the_ai(body)


@pytest.mark.parametrize("body", [
    "Prefiro resolver por aqui msm", "prefiro por aqui", "pode ser por aqui mesmo",
    "quero resolver tudo por aqui", "prefiro falar com a IA", "pode continuar com o agente"])
def test_choosing_this_channel_is_choosing_the_ai(body):
    assert pb.chose_the_ai(body)


@pytest.mark.parametrize("body", [
    "pode marcar por aqui mesmo", "quero agendar por aqui", "quero falar com alguém da equipe",
    "prefiro falar com uma pessoa por aqui"])
def test_booking_or_a_person_here_is_not_choosing_the_ai(body):
    assert not pb.chose_the_ai(body)


@pytest.mark.parametrize("body", [
    "quero falar com alguém da equipe", "quero ser atendido por alguém da equipe",
    "quero falar com o financeiro", "me passa pro suporte", "quero falar com o setor comercial",
    "quero falar com o responsável", "me transfere para um atendente"])
def test_asking_for_the_team_or_a_department_is_read_by_code(body):
    assert pb.wants_a_person(body)


# ── A clinic's questionnaire ────────────────────────────────────────────────

FORM_HISTORY = """user: Olá! Preenchi seu formulário e gostaria de saber mais sobre sua empresa.

Qual é a sua função na clínica?: Proprietário ou sócio
Full name: Izabel Ataide
Qual é o nome da clínica?: Clínica Bem Saúde
Aproximadamente quantas mensagens a clínica recebe por dia?: Até 20
Qual é hoje o principal desafio no atendimento?: Falta de organização dos contatos
assistant: Olá, Izabel! Obrigado por preencher o formulário."""


def _areas_buttons(kb) -> list[str]:
    return [b["callback_data"] for row in kb["inline_keyboard"] for b in row]


async def test_the_lead_form_starts_the_clinic_questions_without_the_model(world):
    world.llm_result = AssertionError  # never read: the form is read by code

    text, kb = await pb.start(None, CONTACT, FORM_HISTORY)

    b = world.rows[1]
    assert pb.is_clinic(b.answers) and b.step == "areas"
    assert b.answers["quem"] == "Izabel Ataide · Clínica Bem Saúde · Clínica · Proprietário ou sócio"
    assert b.answers["canais"] == content.render("proposal.form_volume", volume="Até 20")
    assert sorted(b.prefilled) == ["canais", "quem"]
    assert text.startswith(content.text("proposal.ask_areas"))
    assert _areas_buttons(kb) == [f"prop:pick:areas:{k}" for k in pb.AREAS] + ["prop:done:areas"]


async def test_areas_are_picked_one_by_one_then_done(world):
    await pb.start(None, CONTACT, FORM_HISTORY)

    text, kb = await pb.pick_area(None, world.rows[1], "triagem")
    assert text.startswith(content.render("proposal.areas_chosen", lista="Pré-triagem"))
    assert "prop:pick:areas:triagem" not in _areas_buttons(kb)

    await pb.pick_area(None, world.rows[1], "pagamentos")
    await pb.pick_area(None, world.rows[1], "triagem")  # twice: kept once
    text, _ = await pb.answer(None, CONTACT, world.rows[1], "emissão de atestados")
    assert text.startswith(content.render(
        "proposal.areas_chosen", lista="Pré-triagem, Cobrança e pagamentos, emissão de atestados"))

    text, _ = await pb.answer(None, CONTACT, world.rows[1], "Pronto")
    b = world.rows[1]
    assert b.answers["areas"] == "Pré-triagem, Cobrança e pagamentos, emissão de atestados"
    assert b.step == "como_hoje"
    assert text == content.render(
        "proposal.ask_como_hoje_areas",
        areas="pré-triagem, cobrança e pagamentos, emissão de atestados")


async def test_the_clinic_questions_end_in_a_summary_with_the_areas(world):
    await pb.start(None, CONTACT, FORM_HISTORY)
    await pb.pick_area(None, world.rows[1], "prontuario")
    await pb.areas_done(None, world.rows[1])
    text, _ = await answer_all(world, "3 médicas")

    b = world.rows[1]
    assert b.step is None and pb.next_step(b.answers) is None
    assert f"{content.text('proposal.label_areas')}: Prontuários" in text
    assert f"{content.text('proposal.label_equipe')}: 3 médicas" in text
    assert "trilha" not in text and content.text("proposal.label_desafio") not in text
    _, menu = pb.fix_menu(b)
    assert _areas_buttons(menu) == [f"prop:fix:{s}" for s in pb.CLINIC_STEPS]


async def test_closing_the_areas_with_none_moves_on(world):
    await pb.start(None, CONTACT, FORM_HISTORY)

    text, _ = await pb.areas_done(None, world.rows[1])

    assert world.rows[1].answers["areas"] == ""
    assert text == content.text("proposal.ask_como_hoje")


async def test_an_old_area_tap_after_the_areas_changes_nothing(world):
    await pb.start(None, CONTACT, FORM_HISTORY)
    await pb.areas_done(None, world.rows[1])

    await pb.pick_area(None, world.rows[1], "agenda")

    assert world.rows[1].answers["areas"] == "" and world.rows[1].step == "como_hoje"


async def test_a_clinic_said_in_the_conversation_gets_the_clinic_questions(world):
    world.llm_result = {"nome": "Bia", "empresa": "Sorriso", "ramo": "Odontologia",
                        "cargo": "dentista", "desafio": "agenda"}

    text, _ = await pb.start(None, CONTACT, "user: tenho um consultório odontológico")

    b = world.rows[1]
    assert pb.is_clinic(b.answers) and b.step == "areas" and "desafio" not in b.answers
    assert b.answers["quem"] == "Bia · Sorriso · Odontologia · dentista"


async def test_a_pause_before_done_comes_back_to_the_areas(world):
    """A person asked for mid-areas clears the step (message._pause_proposal):
    picking up must offer the areas again, with what was picked."""
    await pb.start(None, CONTACT, FORM_HISTORY)
    await pb.pick_area(None, world.rows[1], "agenda")
    await repo.save(None, 1, answers=world.rows[1].answers, step=None)

    text, kb = await pb.start(None, CONTACT, FORM_HISTORY)

    assert world.rows[1].step == "areas" and "areas" not in world.rows[1].answers
    assert content.render("proposal.areas_chosen", lista="Atendimento e agenda") in text
    assert "prop:done:areas" in _areas_buttons(kb)


async def test_typed_areas_stay_short_enough_to_send(world):
    await pb.start(None, CONTACT, FORM_HISTORY)
    for n in range(10):
        await pb.answer(None, CONTACT, world.rows[1], f"{n} " + "x" * 1000)

    text, _ = await pb.areas_done(None, world.rows[1])

    areas = world.rows[1].answers["areas"]
    assert 0 < len(areas) <= 500 and all(len(a) <= 120 for a in areas.split(", "))
    assert len(text) < 1024


# ── Questions answered with buttons ─────────────────────────────────────────

def _t(key: str) -> str:
    return content.text(f"proposal.{key}")


def test_steps_of_a_company_and_of_a_clinic():
    assert pb.STEPS == STEPS
    assert pb.CLINIC_STEPS == CLINIC_STEPS


async def _at(world, step: str, clinic: bool = False) -> ProposalBriefing:
    """A questionnaire whose next question is `step`."""
    steps = CLINIC_STEPS if clinic else STEPS
    answers = {s: "x" for s in steps[:steps.index(step)]}
    if clinic:
        answers[pb.TRACK] = "clinica"
    world.rows[1] = ProposalBriefing(id=1, contact_id=7, status="open", step=step,
                                     answers=answers, created_at=NOW, updated_at=NOW)
    return world.rows[1]


async def test_channels_are_picked_then_done(world):
    await _at(world, "canais_lista")
    text, kb = await pb.pick(None, world.rows[1], "canais_lista", "whatsapp")
    assert text.startswith(content.render("proposal.chosen", lista="WhatsApp"))
    assert "prop:pick:canais_lista:whatsapp" not in _areas_buttons(kb)
    assert _areas_buttons(kb)[-1] == "prop:done:canais_lista"
    await pb.pick(None, world.rows[1], "canais_lista", "site")
    text, _ = await pb.pick_done(None, world.rows[1], "canais_lista")
    assert world.rows[1].answers["canais_lista"] == "WhatsApp, Site"
    assert world.rows[1].step == "volume"
    assert text == _t("ask_volume")


async def test_a_typed_channel_counts_and_pronto_closes(world):
    await _at(world, "canais_lista")
    await pb.answer(None, CONTACT, world.rows[1], "Telegram")
    assert world.rows[1].answers["canais_lista_rascunho"] == "Telegram"
    await pb.answer(None, CONTACT, world.rows[1], "pronto")
    assert world.rows[1].answers["canais_lista"] == "Telegram"
    assert "canais_lista_rascunho" not in world.rows[1].answers


async def test_volume_is_one_tap(world):
    await _at(world, "volume")
    text, kb = pb.question("volume", world.rows[1].answers)
    data = _areas_buttons(kb)
    assert data[:4] == [f"prop:pick:volume:{k}" for k in
                        ("ate_500", "ate_2000", "ate_5000", "mais_5000")]
    assert data[4:] == ["prop:skip", "prop:stop", "prop:team"]
    text, _ = await pb.pick(None, world.rows[1], "volume", "ate_2000")
    assert world.rows[1].answers["volume"] == "Até 2.000"
    assert world.rows[1].step == "sistemas_lista"
    assert text.startswith(_t("ask_sistemas_lista"))


async def test_a_typed_single_answer_is_kept_as_written(world):
    await _at(world, "volume")
    await pb.answer(None, CONTACT, world.rows[1], "umas mil")
    assert world.rows[1].answers["volume"] == "umas mil"
    assert world.rows[1].step == "sistemas_lista"


async def test_no_system_closes_at_once(world):
    await _at(world, "sistemas_lista")
    await pb.pick(None, world.rows[1], "sistemas_lista", "nenhum")
    assert world.rows[1].answers["sistemas_lista"] == "Nenhum"
    assert world.rows[1].step == "processo"


async def test_no_system_after_a_pick_keeps_the_pick(world):
    await _at(world, "sistemas_lista")
    await pb.pick(None, world.rows[1], "sistemas_lista", "crm")
    await pb.pick(None, world.rows[1], "sistemas_lista", "nenhum")
    assert world.rows[1].answers["sistemas_lista"] == "CRM"


def test_a_company_picks_the_table_areas_and_a_clinic_its_own():
    assert pb.options_for("areas", {}) == pricing.AREAS
    assert pb.options_for("areas", {pb.TRACK: "clinica"}) == pb.AREAS


async def test_a_company_areas_question(world):
    await _at(world, "areas")
    text, kb = pb.question("areas", world.rows[1].answers)
    assert text.startswith(_t("ask_biz_areas"))
    assert _areas_buttons(kb) == [f"prop:pick:areas:{k}" for k in pricing.AREAS] + \
        ["prop:done:areas"]


async def test_a_tap_for_another_question_records_nothing(world):
    await _at(world, "volume")
    before = dict(world.rows[1].answers)
    text, _ = await pb.pick(None, world.rows[1], "canais_lista", "site")
    assert world.rows[1].answers == before and world.rows[1].step == "volume"
    assert text == _t("ask_volume")


async def test_an_unknown_option_records_nothing(world):
    await _at(world, "volume")
    before = dict(world.rows[1].answers)
    await pb.pick(None, world.rows[1], "volume", "um_milhao")
    assert world.rows[1].answers == before


async def test_an_old_request_goes_on_with_the_new_questions(world):
    old = {"quem": "Ana", "desafio": "d", "custo_hoje": "c", "canais": "WhatsApp, 300/mês",
           "ferramentas": "planilha", "usuarios": "3", "prazo": "dez"}
    assert pb.next_step(old) == "areas"
    shown = pb.shown_steps(old)
    assert {"canais", "ferramentas", "usuarios"} <= set(shown)
    b = ProposalBriefing(id=1, contact_id=7, status="open", answers=old,
                         created_at=NOW, updated_at=NOW)
    assert f"{_t('label_ferramentas')}: planilha" in pb.summary(b)


def test_new_options_fit():
    keys = ([f"canal_{k}" for k in ("whatsapp", "instagram", "site", "telefone", "email")]
            + [f"biz_area_{k}" for k in pricing.AREAS]
            + [f"sistema_{k}" for k in ("google_agenda", "planilha", "crm", "gestao", "nenhum")]
            + [f"volume_{k}" for k in ("ate_500", "ate_2000", "ate_5000", "mais_5000")]
            + [f"processo_{k}" for k in ("sim", "parcial", "nao")]
            + [f"usuarios_{k}" for k in ("1_2", "3_5", "6_10", "mais_10")]
            + [f"label_{s}" for s in ("canais_lista", "volume", "sistemas_lista", "processo",
                                      "usuarios_faixa")])
    assert all(0 < len(_t(k)) <= 24 for k in keys)


# ── The preliminary quote, saved when the request reaches the team ──────────

@pytest.fixture
def quotes(world, monkeypatch):
    from core.repositories import proposal_quotes as quotes_repo

    saved: dict[int, object] = {}

    async def insert_once(conn, briefing_id, computed):
        if briefing_id in saved:
            return False
        saved[briefing_id] = computed
        return True

    monkeypatch.setattr(quotes_repo, "insert_once", insert_once)
    return saved


def _complete_company() -> dict[str, str]:
    return {"quem": "Ana", "desafio": "d", "areas": _t("biz_area_vendas"), "custo_hoje": "c",
            "canais_lista": "WhatsApp", "volume": _t("volume_ate_500"),
            "sistemas_lista": _t("sistema_nenhum"), "processo": _t("processo_sim"),
            "usuarios_faixa": _t("usuarios_1_2"), "prazo": "dez"}


async def _with(world, answers: dict[str, str]) -> ProposalBriefing:
    world.rows[1] = ProposalBriefing(id=1, contact_id=7, status="open", step=None,
                                     answers=answers, created_at=NOW, updated_at=NOW)
    return world.rows[1]


async def test_submit_saves_the_quote(world, quotes):
    from core.services import proposal_quote

    b = await _with(world, _complete_company())
    text = await pb.submit(None, CONTACT, b)
    assert isinstance(quotes[1], proposal_quote.Computed)
    expected = proposal_quote.compute(b.answers, clinic=False)
    assert isinstance(expected, proposal_quote.Computed)
    assert quotes[1].quote == expected.quote
    assert text == content.text("proposal.done"), "the customer is told nothing new"


async def test_an_incomplete_request_with_the_five_saves_the_quote(world, quotes):
    answers = _complete_company()
    del answers["prazo"], answers["usuarios_faixa"]
    b = await _with(world, answers)
    assert await pb.notify_incomplete(None, CONTACT, b)
    assert 1 in quotes


async def test_an_incomplete_request_without_the_volume_saves_nothing(world, quotes):
    answers = _complete_company()
    del answers["volume"]
    b = await _with(world, answers)
    await pb.notify_incomplete(None, CONTACT, b)
    assert quotes == {}


async def test_a_failing_quote_never_loses_the_request(world, quotes, monkeypatch):
    from core.services import proposal_quote

    def broken(answers, clinic):
        raise RuntimeError("tabela quebrada")
    monkeypatch.setattr(proposal_quote, "compute", broken)
    b = await _with(world, _complete_company())
    text = await pb.submit(None, CONTACT, b)
    assert world.rows[1].status == "sent" and len(world.team) == 1
    assert text == content.text("proposal.done") and quotes == {}


async def test_a_clinic_quote_is_priced_as_a_clinic(world, quotes):
    answers = {**_complete_company(), pb.TRACK: "clinica", "areas": _t("area_prontuario")}
    b = await _with(world, answers)
    await pb.submit(None, CONTACT, b)
    assert quotes[1].scope.infra == "dedicada_lgpd"


async def test_reaching_the_team_twice_keeps_the_first_quote(world, quotes):
    b = await _with(world, _complete_company())
    await pb.notify_incomplete(None, CONTACT, b)
    first = quotes[1]
    await pb.submit(None, CONTACT, world.rows[1])
    assert quotes[1] is first


# ── Several choices typed at once (WhatsApp lists take one per message) ─────

def _numbered(step: str, answers: dict[str, str]) -> list[str]:
    return [f"{n}. {pb._label(step, answers, k)}"
            for n, k in enumerate(pb.options_for(step, answers), 1)]


async def test_a_multi_choice_lists_every_option_numbered_with_the_hint(world):
    await _at(world, "areas")
    text, _ = pb.question("areas", world.rows[1].answers)
    assert text.startswith(_t("ask_biz_areas"))
    assert "\n".join(_numbered("areas", {})) in text
    assert _t("multi_hint") in text


async def test_the_numbers_stay_the_same_after_a_pick(world):
    await _at(world, "areas")
    text, _ = await pb.pick(None, world.rows[1], "areas", "vendas")
    assert "\n".join(_numbered("areas", {})) in text


async def test_typed_numbers_pick_several_and_move_on(world):
    await _at(world, "areas")
    text, _ = await pb.answer(None, CONTACT, world.rows[1], "1, 3 e 4")
    labels = [_t(f"biz_area_{k}") for k in ("atendimento", "financeiro", "documentos_juridico")]
    assert world.rows[1].answers["areas"] == ", ".join(labels)
    assert world.rows[1].step == "custo_hoje" and text == _t("ask_custo_hoje")


async def test_typed_numbers_join_what_was_tapped(world):
    await _at(world, "areas")
    await pb.pick(None, world.rows[1], "areas", "vendas")
    await pb.answer(None, CONTACT, world.rows[1], "2 3")
    assert world.rows[1].answers["areas"] == f"{_t('biz_area_vendas')}, {_t('biz_area_financeiro')}"


async def test_typed_names_pick_several_and_move_on(world):
    await _at(world, "canais_lista")
    await pb.answer(None, CONTACT, world.rows[1], "whatsapp e instagram")
    assert world.rows[1].answers["canais_lista"] == "WhatsApp, Instagram"
    assert world.rows[1].step == "volume"


async def test_a_number_not_on_the_list_records_nothing(world):
    await _at(world, "areas")
    before = dict(world.rows[1].answers)
    text, _ = await pb.answer(None, CONTACT, world.rows[1], "9")
    assert world.rows[1].answers == before and world.rows[1].step == "areas"
    assert _t("multi_hint") in text


async def test_free_text_is_still_one_typed_option(world):
    await _at(world, "areas")
    await pb.answer(None, CONTACT, world.rows[1], "controle de estoque")
    assert world.rows[1].answers["areas_rascunho"] == "controle de estoque"
    assert world.rows[1].step == "areas"


async def test_no_system_typed_alone_or_with_others(world):
    await _at(world, "sistemas_lista")
    await pb.answer(None, CONTACT, world.rows[1], "5")
    assert world.rows[1].answers["sistemas_lista"] == _t("sistema_nenhum")
    await _at(world, "sistemas_lista")
    await pb.answer(None, CONTACT, world.rows[1], "1, 5")
    assert world.rows[1].answers["sistemas_lista"] == _t("sistema_google_agenda")


async def test_a_clinic_types_the_numbers_of_its_own_areas(world):
    await _at(world, "areas", clinic=True)
    await pb.answer(None, CONTACT, world.rows[1], "2 e 3")
    assert world.rows[1].answers["areas"] == f"{_t('area_triagem')}, {_t('area_prontuario')}"
    assert world.rows[1].step == "como_hoje"



# ── How the customer prefers to pay ─────────────────────────────────────────

async def test_the_payment_method_is_one_tap_before_the_deadline(world):
    await _at(world, "pagamento")
    text, kb = pb.question("pagamento", world.rows[1].answers)
    assert text == _t("ask_pagamento")
    assert _areas_buttons(kb) == ["prop:pick:pagamento:cartao", "prop:pick:pagamento:boleto",
                                  "prop:pick:pagamento:pix", "prop:skip", "prop:stop",
                                  "prop:team"]
    text, _ = await pb.pick(None, world.rows[1], "pagamento", "pix")
    assert world.rows[1].answers["pagamento"] == _t("pagamento_pix")
    assert world.rows[1].step == "prazo" and text == _t("ask_prazo")


def test_the_payment_options_fit():
    keys = [f"pagamento_{k}" for k in ("cartao", "boleto", "pix")] + ["label_pagamento"]
    assert all(0 < len(_t(k)) <= 24 for k in keys)


# ── On WhatsApp, the request starts with the Flow ───────────────────────────

@pytest.fixture
def flows(monkeypatch, tmp_path):
    """The shipped Flows, published: each with an id."""
    import shutil
    from pathlib import Path

    from core.services import forms

    shipped = Path(__file__).resolve().parent.parent / "content" / "forms"
    shutil.copytree(shipped, tmp_path / "forms")
    (tmp_path / "forms" / "flows.toml").write_text(
        '[proposta_atendimento]\nflow_id = "F-EMP"\n[proposta_clinica]\nflow_id = "F-CLI"\n',
        encoding="utf-8")
    monkeypatch.setattr(forms, "_dir", lambda: tmp_path / "forms")
    forms.reset()
    yield
    forms.reset()


def _flow_of(keyboard) -> dict:
    assert keyboard is not None and "_flow" in keyboard, keyboard
    return keyboard["_flow"]


def _datas(keyboard: dict) -> list[str]:
    return [b["callback_data"] for row in keyboard["inline_keyboard"] for b in row]


async def test_on_whatsapp_the_line_of_business_comes_first_when_nothing_said_it(world, flows):
    text, kb = await pb.start(None, CONTACT, "oi", whatsapp=True)

    assert text == _t("ask_ramo") and kb is not None and "_flow" not in kb
    assert _datas(kb) == [f"prop:ramo:{k}" for k in (*pb.RAMOS, "outro")]
    assert world.rows[1].step == pb.RAMO_STEP


async def test_on_whatsapp_the_request_starts_with_the_form(world, flows):
    await pb.start(None, CONTACT, "oi", whatsapp=True)

    text, kb = await pb.set_ramo(None, CONTACT, world.rows[1], key="comercio")

    assert text == _t("flow_intro")
    assert _flow_of(kb) == {"flow_id": "F-EMP", "token": "proposta:1", "cta": _t("flow_cta"),
                            "screen": "SOBRE",
                            "data": {"nome": "Ana", "empresa": "", "ramo": _t("ramo_comercio"),
                                     "cargo": "", "email": ""}}
    assert world.rows[1].step is None, "a typed message is not an answer while the form is open"


async def test_health_sends_the_clinics_form(world, flows):
    await pb.start(None, CONTACT, "oi", whatsapp=True)

    _, kb = await pb.set_ramo(None, CONTACT, world.rows[1], key="saude")

    assert _flow_of(kb)["flow_id"] == "F-CLI" and pb.is_clinic(world.rows[1].answers)


@pytest.mark.parametrize("typed, flow_id, ramo", [
    ("Odontologia", "F-CLI", "Odontologia"), ("  padaria   artesanal ", "F-EMP", "padaria artesanal")])
async def test_a_typed_line_of_business_picks_the_form_and_fills_it_in(
        world, flows, typed, flow_id, ramo):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    await pb.set_ramo(None, CONTACT, world.rows[1], key="outro")
    assert world.rows[1].step == pb.RAMO_STEP

    _, kb = await pb.answer(None, CONTACT, world.rows[1], typed)

    assert _flow_of(kb)["flow_id"] == flow_id
    assert world.rows[1].answers[pb.RAMO_STEP] == ramo
    if flow_id == "F-EMP":
        assert _flow_of(kb)["data"]["ramo"] == ramo


async def test_other_asks_for_it_typed(world, flows):
    await pb.start(None, CONTACT, "oi", whatsapp=True)

    text, kb = await pb.set_ramo(None, CONTACT, world.rows[1], key="outro")

    assert text == _t("ask_ramo_typed") and kb is None


async def test_an_old_line_of_business_tap_changes_nothing(world, flows):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    await pb.set_ramo(None, CONTACT, world.rows[1], key="comercio")

    await pb.set_ramo(None, CONTACT, world.rows[1], key="saude")

    assert not pb.is_clinic(world.rows[1].answers)


async def test_what_the_conversation_said_of_the_person_is_not_asked_again(world, flows):
    world.llm_result = {"nome": "Ana", "ramo": "comércio de roupas", "cargo": "dona"}

    _, kb = await pb.start(None, CONTACT.model_copy(update={"name": "Ana Paula"}), "oi",
                           whatsapp=True)

    data = _flow_of(kb)["data"]
    assert data == {"nome": "Ana", "empresa": "", "ramo": "comércio de roupas",
                    "cargo": "dona", "email": ""}


async def test_an_old_list_sends_the_open_form_again(world, flows):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    await pb.set_ramo(None, CONTACT, world.rows[1], key="comercio")

    text, kb = await pb.set_ramo(None, CONTACT, world.rows[1], key="saude")

    assert text == _t("flow_intro") and _flow_of(kb)["flow_id"] == "F-EMP"


async def test_a_clinic_said_late_in_a_long_answer_is_still_a_clinic(world, flows):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    long = "Trabalhamos com atendimento ao público há muitos anos aqui na cidade, " * 2 + \
        "somos uma clínica odontológica"

    _, kb = await pb.answer(None, CONTACT, world.rows[1], long)

    assert _flow_of(kb)["flow_id"] == "F-CLI"
    assert len(world.rows[1].answers[pb.RAMO_STEP]) <= 100


async def test_without_the_form_what_is_known_is_not_asked(world, monkeypatch):
    """Only the company's Flow is published; a clinic answers the questions,
    and its name, clinic and role, said in the conversation, are not asked."""
    import shutil
    from pathlib import Path

    from core.services import forms

    folder = Path(__file__).resolve().parent.parent / "content" / "forms"
    local = Path(__import__("tempfile").mkdtemp()) / "forms"
    shutil.copytree(folder, local)
    (local / "flows.toml").write_text('[proposta_atendimento]\nflow_id = "F-EMP"\n',
                                      encoding="utf-8")
    monkeypatch.setattr(forms, "_dir", lambda: local)
    forms.reset()
    world.llm_result = {"nome": "Ana", "empresa": "Clínica Sol", "cargo": "dona"}
    await pb.start(None, CONTACT, "oi", whatsapp=True)

    text, _ = await pb.set_ramo(None, CONTACT, world.rows[1], key="saude")

    b = world.rows[1]
    assert b.answers["quem"] == f"Ana · Clínica Sol · {_t('clinic_line')} · dona"
    assert b.step == "areas" and text.startswith(_t("ask_areas")[:20])


async def test_the_line_of_business_follows_the_form(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    await pb.set_ramo(None, CONTACT, world.rows[1], key="comercio")

    await pb.apply_flow(None, CONTACT, "proposta:1", {**COMPANY_ANSWER, "ramo": "Engenharia"})

    assert world.rows[1].answers[pb.RAMO_STEP] == "Engenharia"


@pytest.mark.parametrize("ramo, typed, kept", [
    ("Comércio", "Ana · Sol · Sócia", f"Ana, Sol, Sócia{pb._SEP}Comércio"),
    ("Comércio", "Ana, Sol, comércio, Sócia", "Ana, Sol, comércio, Sócia"),
    ("Comércio", "Ana · Sol · Sócia · Comércio", "Ana, Sol, Sócia, Comércio"),
    ("Construção · reformas", "Ana, Sol, Construção · reformas, Sócia",
     "Ana, Sol, Construção, reformas, Sócia"),
    ("Construção · reformas · manutenção", "Ana · Sol · Sócia",
     f"Ana, Sol, Sócia{pb._SEP}Construção, reformas, manutenção")])
async def test_the_typed_line_keeps_the_known_line_of_business_once(world, ramo, typed, kept):
    world.llm_result = {"ramo": ramo}
    await pb.start(None, CONTACT, "oi")
    assert world.rows[1].step == "quem"

    await pb.answer(None, CONTACT, world.rows[1], typed)

    quem = world.rows[1].answers["quem"]
    assert quem == kept
    assert len(quem.split(pb._SEP)) != 4, "never read back as name, company, line, role"


async def test_telegram_is_not_asked_the_line_of_business(world, flows):
    text, _ = await pb.start(None, CONTACT, "oi", whatsapp=False)
    assert text == _t("ask_quem")


async def test_what_is_known_is_filled_in(world, flows):
    world.llm_result = dict(FOUR)
    contact = CONTACT.model_copy(update={"email": "ana@sol.com.br"})

    _, kb = await pb.start(None, contact, "oi", whatsapp=True)

    assert _flow_of(kb)["data"] == {"nome": "Ana", "empresa": "Sol Engenharia",
                                    "ramo": "construção", "cargo": "dona",
                                    "email": "ana@sol.com.br"}


async def test_a_clinic_gets_the_clinic_form(world, flows):
    _, kb = await pb.start(None, CONTACT, FORM_HISTORY, whatsapp=True)
    flow = _flow_of(kb)
    assert flow["flow_id"] == "F-CLI"
    assert flow["data"]["nome"].startswith("Izabel Ataide")


async def test_without_a_published_flow_the_questions_are_asked(world, monkeypatch):
    from core.services import forms
    monkeypatch.setattr(forms, "_flow_ids", dict)
    forms.reset()
    text, kb = await pb.start(None, CONTACT, "oi", whatsapp=True)
    forms.reset()
    assert text == _t("ask_quem") and "_flow" not in (kb or {})
    assert world.rows[1].step == "quem"


async def test_telegram_gets_the_questions(world, flows):
    text, _ = await pb.start(None, CONTACT, "oi", whatsapp=False)
    assert text == _t("ask_quem") and world.rows[1].step == "quem"


async def test_asking_again_sends_the_same_form(world, flows):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    again, _ = await pb.start(None, CONTACT, "oi", whatsapp=True)
    assert again == _t("ask_ramo") and len(world.rows) == 1
    await pb.set_ramo(None, CONTACT, world.rows[1], key="servicos")

    text, kb = await pb.start(None, CONTACT, "oi", whatsapp=True)
    assert len(world.rows) == 1 and _flow_of(kb)["token"] == "proposta:1"
    assert text == _t("flow_intro")


async def test_a_request_half_answered_by_questions_gets_the_form(world, flows):
    await pb.start(None, CONTACT, "oi")              # before the Flow was published
    await pb.answer(None, CONTACT, world.rows[1], "Ana, Sol")
    assert world.rows[1].step == "desafio"

    _, kb = await pb.start(None, CONTACT, "oi", whatsapp=True)

    assert _flow_of(kb)["data"]["nome"] == "Ana, Sol" and world.rows[1].step is None


async def test_a_finished_request_shows_its_summary_not_the_form(world, flows):
    await pb.start(None, CONTACT, "oi")
    await answer_all(world)
    text, kb = await pb.start(None, CONTACT, "oi", whatsapp=True)
    assert "_flow" not in (kb or {}) and "prop:ok" in str(kb)


async def test_a_form_that_could_not_be_sent_falls_back_to_the_questions(world, flows):
    await pb.start(None, CONTACT, "oi", whatsapp=True)

    fallback = await pb.fall_back(None, CONTACT)

    assert fallback is not None and fallback[0] == _t("ask_quem")
    assert world.rows[1].step == "quem"


async def test_falling_back_without_a_request_does_nothing(world):
    assert await pb.fall_back(None, CONTACT) is None


# ── The Flow's answer, applied to the request ───────────────────────────────

@pytest.fixture
def answered(world, monkeypatch):
    """The request can be read back by id, and the contact's details are kept."""
    from core.repositories import contacts as contacts_repo

    saved: dict = {}

    async def get(conn, briefing_id):
        return world.rows.get(briefing_id)

    async def update_details(conn, contact_id, **fields):
        saved.update(fields)

    monkeypatch.setattr(repo, "get", get)
    monkeypatch.setattr(contacts_repo, "update_details", update_details)
    return saved


COMPANY_ANSWER = {
    "nome": "Ana", "empresa": "Sol", "ramo": "Engenharia", "cargo": "Sócia",
    "desafio": "atender no WhatsApp",
    "areas": ["vendas", "atendimento"], "custo_hoje": "duas pessoas",
    "canais": ["whatsapp", "site"], "volume": "ate_2000",
    "sistemas": "Outlook, Excel,  RD Station", "processo": "parcial", "usuarios": "3_5",
    "pagamento": "pix", "prazo": "dezembro", "email": "ana@sol.com.br",
}


async def test_the_answer_fills_the_request_with_the_labels(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)

    text, kb = await pb.apply_flow(None, CONTACT, "proposta:1", dict(COMPANY_ANSWER))

    b = world.rows[1]
    assert b.step is None and pb.next_step(b.answers) is None
    assert {k: v for k, v in b.answers.items() if k in pb.STEPS} == {
        "quem": "Ana · Sol · Engenharia · Sócia",
    "desafio": "atender no WhatsApp",
        "areas": f"{_t('biz_area_vendas')}, {_t('biz_area_atendimento')}",
        "custo_hoje": "duas pessoas", "canais_lista": "WhatsApp, Site",
        "volume": _t("volume_ate_2000"), "sistemas_lista": "Outlook, Excel, RD Station",
        "processo": _t("processo_parcial"), "usuarios_faixa": _t("usuarios_3_5"),
        "pagamento": _t("pagamento_pix"), "prazo": "dezembro"}
    assert text.startswith(content.render("proposal.confirm", resumo="").strip()[:10])
    assert "prop:ok" in str(kb) and "prop:fix" in str(kb)
    assert answered == {"email": "ana@sol.com.br"}


async def test_a_clinic_answer_uses_the_clinic_areas(world, flows, answered):
    await pb.start(None, CONTACT, FORM_HISTORY, whatsapp=True)
    payload = {**COMPANY_ANSWER, "areas": ["triagem", "prontuario"],
               "como_hoje": "a recepção, à mão", "equipe": "3 médicas, só particular"}

    await pb.apply_flow(None, CONTACT, "proposta:1", payload)

    b = world.rows[1]
    assert b.answers["areas"] == f"{_t('area_triagem')}, {_t('area_prontuario')}"
    assert b.answers["equipe"] == "3 médicas, só particular"
    assert "desafio" not in b.answers and pb.next_step(b.answers) is None


async def test_the_contacts_record_fills_what_the_conversation_did_not(world, flows):
    contact = CONTACT.model_copy(update={"name": "Ana Lima", "organization": "Sol Ltda"})
    await pb.start(None, contact, "oi", whatsapp=True)

    _, kb = await pb.set_ramo(None, contact, world.rows[1], key="comercio")

    data = _flow_of(kb)["data"]
    assert data["nome"] == "Ana Lima" and data["empresa"] == "Sol Ltda"


async def test_the_conversation_wins_over_the_contacts_record(world, flows):
    world.llm_result = dict(FOUR)
    contact = CONTACT.model_copy(update={"name": "Ana Lima", "organization": "Sol Ltda"})

    _, kb = await pb.start(None, contact, "oi", whatsapp=True)

    data = _flow_of(kb)["data"]
    assert data["nome"] == "Ana" and data["empresa"] == "Sol Engenharia"


async def test_the_four_fields_become_the_one_line_of_the_questionnaire(world, flows, answered):
    await pb.start(None, CONTACT, FORM_HISTORY, whatsapp=True)
    payload = {**COMPANY_ANSWER, "nome": " Ana  ·  Lima ", "empresa": "Sol", "ramo": "x",
               "cargo": "Sócia", "areas": ["triagem"]}

    await pb.apply_flow(None, CONTACT, "proposta:1", payload)

    assert world.rows[1].answers["quem"] == f"Ana Lima · Sol · {_t('clinic_line')} · Sócia"


async def test_a_missing_name_part_leaves_the_line_to_be_asked(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)

    await pb.apply_flow(None, CONTACT, "proposta:1", {**COMPANY_ANSWER, "cargo": "  "})

    assert world.rows[1].answers["quem"] == ""


async def test_a_bad_name_part_keeps_the_line_already_known(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    await pb.apply_flow(None, CONTACT, "proposta:1", dict(COMPANY_ANSWER))
    before = world.rows[1].answers["quem"]

    await pb.apply_flow(None, CONTACT, "proposta:1", {**COMPANY_ANSWER, "cargo": []})
    assert world.rows[1].answers["quem"] == before

    await pb.apply_flow(None, CONTACT, "proposta:1", {"nome": "Só o nome"})
    assert world.rows[1].answers["quem"] == before


async def test_empty_bottlenecks_keep_the_challenge_from_the_conversation(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    world.rows[1].answers["desafio"] = "Integrar o ERP com o WhatsApp"
    payload = {k: v for k, v in COMPANY_ANSWER.items() if k != "desafio"}

    await pb.apply_flow(None, CONTACT, "proposta:1", {**payload, "gargalos": "", "pedidos": " "})

    assert world.rows[1].answers["desafio"] == "Integrar o ERP com o WhatsApp"


async def test_how_it_is_done_today_is_not_cut_at_the_text_area_limit(world, flows, answered):
    await pb.start(None, CONTACT, FORM_HISTORY, whatsapp=True)
    long = "a" * 425

    await pb.apply_flow(None, CONTACT, "proposta:1", {
        **COMPANY_ANSWER, "areas": ["triagem"], "como_hoje": long, "gargalos": "", "pedidos": ""})

    assert world.rows[1].answers["como_hoje"] == long


async def test_the_flow_limits_for_the_two_text_areas_match_what_is_kept():
    from core.services import forms
    for name in ("proposta_atendimento", "proposta_clinica"):
        path = forms._dir() / f"{name}.json"
        areas = {c["name"]: c for s in json.loads(path.read_text(encoding="utf-8"))["screens"]
                 for c in s["layout"]["children"] if c.get("name") in ("gargalos", "pedidos")}
        assert {k: v["max-length"] for k, v in areas.items()} == {
            "gargalos": pb._ASK_PART_MAX, "pedidos": pb._ASK_PART_MAX}


async def test_the_bottlenecks_and_the_request_become_the_challenge(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    payload = {k: v for k, v in COMPANY_ANSWER.items() if k != "desafio"}

    await pb.apply_flow(None, CONTACT, "proposta:1", {
        **payload, "gargalos": " responder fora do horário ", "pedidos": "enviar boletos"})

    assert world.rows[1].answers["desafio"] == (
        f"responder fora do horário\n{_t('flow_pedido')}: enviar boletos")


async def test_a_clinic_keeps_how_it_is_done_today_and_adds_both(world, flows, answered):
    await pb.start(None, CONTACT, FORM_HISTORY, whatsapp=True)
    payload = {**COMPANY_ANSWER, "areas": ["triagem"], "como_hoje": "a recepção, à mão",
               "equipe": "3 médicas", "gargalos": "faltas", "pedidos": "lembrete"}

    await pb.apply_flow(None, CONTACT, "proposta:1", payload)

    assert world.rows[1].answers["como_hoje"] == (
        f"a recepção, à mão\n{_t('flow_gargalos')}: faltas\n{_t('flow_pedido')}: lembrete")
    assert "desafio" not in world.rows[1].answers


async def test_a_request_alone_still_leaves_the_challenge_filled(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    payload = {k: v for k, v in COMPANY_ANSWER.items() if k != "desafio"}

    await pb.apply_flow(None, CONTACT, "proposta:1", {**payload, "pedidos": "x", "gargalos": 5})

    assert world.rows[1].answers["desafio"] == f"{_t('flow_pedido')}: x"
    assert pb.next_step(world.rows[1].answers) is None


async def test_without_the_challenge_the_areas_stand_in_for_it(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    payload = {k: v for k, v in COMPANY_ANSWER.items() if k != "desafio"}

    await pb.apply_flow(None, CONTACT, "proposta:1", payload)

    b = world.rows[1]
    assert b.answers["desafio"] == b.answers["areas"] != ""
    assert pb.next_step(b.answers) is None


async def test_unknown_ids_and_odd_values_are_left_out(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    payload = {**COMPANY_ANSWER, "areas": ["vendas", "robotica"], "volume": "um_milhao",
               "prazo": 42, "desafio": "x" * 1500, "sistemas": "Nenhum."}

    await pb.apply_flow(None, CONTACT, "proposta:1", payload)

    b = world.rows[1]
    assert b.answers["areas"] == _t("biz_area_vendas")
    assert b.answers["volume"] == "" and b.answers["prazo"] == ""
    assert b.answers["desafio"] == "x" * 1000 + "…"
    assert b.answers["sistemas_lista"] == _t("sistema_nenhum")


@pytest.mark.parametrize("typed, stored", [
    ("Sankhya\nTotvs; Excel, excel", "Sankhya, Totvs, Excel"),
    ("nenhum", None), ("  Nenhuma  ", None), ("não uso", None),
    ("Excel, nenhum", "Excel"),
    ("", ""), (["google_agenda"], ""), (42, ""),
])
async def test_the_tools_are_typed_and_split_by_comma(world, flows, answered, typed, stored):
    await pb.start(None, CONTACT, "oi", whatsapp=True)

    await pb.apply_flow(None, CONTACT, "proposta:1", {**COMPANY_ANSWER, "sistemas": typed})

    expected = _t("sistema_nenhum") if stored is None else stored
    assert world.rows[1].answers["sistemas_lista"] == expected


async def test_the_typed_tools_are_capped(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    lots = ", ".join(f"Sistema {n}" for n in range(60))

    await pb.apply_flow(None, CONTACT, "proposta:1", {**COMPANY_ANSWER, "sistemas": lots})

    kept = world.rows[1].answers["sistemas_lista"].split(", ")
    assert len(kept) == 20 and kept[0] == "Sistema 0"


async def test_an_invalid_email_is_not_kept(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    await pb.apply_flow(None, CONTACT, "proposta:1", {**COMPANY_ANSWER, "email": "não tenho"})
    assert answered == {} and world.rows[1].answers["quem"].startswith("Ana")


async def test_the_answered_fields_are_no_longer_prefilled(world, flows, answered):
    world.llm_result = dict(FOUR)
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    assert world.rows[1].prefilled == ["quem"]
    await pb.apply_flow(None, CONTACT, "proposta:1", dict(COMPANY_ANSWER))
    assert world.rows[1].prefilled == []


async def test_an_answer_for_someone_else_records_nothing(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    other = CONTACT.model_copy(update={"id": 99})

    text, _ = await pb.apply_flow(None, other, "proposta:1", dict(COMPANY_ANSWER))

    assert text == _t("offer") and world.rows[1].answers == {} and answered == {}


@pytest.mark.parametrize("token", ["proposta:abc", "proposta:", "proposta:2", "outra:1"])
async def test_an_unknown_token_records_nothing(world, flows, answered, token):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    text, _ = await pb.apply_flow(None, CONTACT, token, dict(COMPANY_ANSWER))
    assert text == _t("offer") and world.rows[1].answers == {}


async def test_an_answer_after_the_request_was_sent_records_nothing(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    await pb.apply_flow(None, CONTACT, "proposta:1", dict(COMPANY_ANSWER))
    await pb.submit(None, CONTACT, world.rows[1])
    before = dict(world.rows[1].answers)

    text, _ = await pb.apply_flow(None, CONTACT, "proposta:1", {**COMPANY_ANSWER, "prazo": "já"})

    assert text == _t("offer") and world.rows[1].answers == before


@pytest.mark.parametrize("token", ["proposta:²", "proposta:" + "9" * 4301, "proposta:１"])
async def test_a_token_that_is_not_a_plain_number_records_nothing(world, flows, answered, token):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    text, _ = await pb.apply_flow(None, CONTACT, token, dict(COMPANY_ANSWER))
    assert text == _t("offer") and world.rows[1].answers == {}


@pytest.mark.parametrize("email", [{"other": "intruso@example.com"}, ["intruso@example.com"],
                                   "x" * 300 + "@example.com", 42])
async def test_an_email_that_is_not_plain_text_is_not_kept(world, flows, answered, email):
    await pb.start(None, CONTACT, "oi", whatsapp=True)
    await pb.apply_flow(None, CONTACT, "proposta:1", {**COMPANY_ANSWER, "email": email})
    assert answered == {} and world.rows[1].answers["quem"].startswith("Ana")


async def test_a_bare_number_of_hours_keeps_its_unit(world, flows, answered):
    await pb.start(None, CONTACT, "oi", whatsapp=True)

    await pb.apply_flow(None, CONTACT, "proposta:1", {**COMPANY_ANSWER, "custo_hoje": " 10 "})
    assert world.rows[1].answers["custo_hoje"] == \
        content.render("proposal.horas_semana", horas="10")

    await pb.apply_flow(None, CONTACT, "proposta:1", {**COMPANY_ANSWER, "custo_hoje": "10h, 2 pessoas"})
    assert world.rows[1].answers["custo_hoje"] == "10h, 2 pessoas"


def test_a_challenge_copied_from_the_areas_is_not_shown_twice():
    areas = f"{_t('biz_area_vendas')}, {_t('biz_area_atendimento')}"
    b = ProposalBriefing(id=1, contact_id=7, status="open", created_at=NOW, updated_at=NOW,
                         answers={"desafio": areas, "areas": areas})
    assert content.text("proposal.label_desafio") not in pb.summary(b)
    assert f"{content.text('proposal.label_areas')}: {areas}" in pb.summary(b)

    b.answers["desafio"] = "Integrar o ERP"
    assert f"{content.text('proposal.label_desafio')}: Integrar o ERP" in pb.summary(b)
