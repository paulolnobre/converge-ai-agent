"""The organization's words, and the check that runs before they reach anyone.

The validator is the interesting part. A template that names something the code
cannot supply must stop the deployment, because the alternative is a patient
reading "{profissional}" in a confirmation.
"""
import json

import pytest

from core import content
from core.config import settings


@pytest.fixture
def custom_content(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point the loader at a content directory a test writes."""
    def write(messages: str):
        (tmp_path / "messages.toml").write_text(messages, encoding="utf-8")
        monkeypatch.setattr(settings, "content_dir", str(tmp_path))
        content.reset()
    yield write
    content.reset()


MINIMAL = '''
[voice]
tone = "Cordial."
never = ["Nunca dê diagnóstico."]

[reply]
out_of_hours = "Fechado."
escalation = "Um momento."
unknown = "Não sei."

[scheduling]
offer = "Tenho {slots}."
confirmation = "Marcado {data} ({dia_semana}) {horario} em {endereco}."
reminder = "Olá {nome_paciente}, {data} {horario}."
cancellation = "Cancelado {data} {horario}."
no_slots = "Sem horário."
which_day = "Qual dia?"
offer_dropped = "Tudo bem."
slot_taken = "Foi preenchido."
nothing_to_cancel = "Nada a cancelar."
could_not_book = "Não consegui."

[facts]
endereco = "Rua A, 1"
'''


# ── What ships in the repository ─────────────────────────────────────────────

def test_the_shipped_content_is_usable():
    content.reset()
    content.validate()


def test_the_shipped_content_forbids_quoting_projects_and_promising_results():
    """A price for custom work or a promised gain, stated by the bot, is a
    commitment the company never made. Losing these rules silently is the
    failure worth a test."""
    rules = content.voice_rules().lower()
    assert "sob medida" in rules
    assert "prometa resultado" in rules


def test_every_message_the_desk_says_is_accounted_for():
    """Nothing the handler renders may be missing from the content file."""
    content.reset()
    for path, placeholders in content._PLACEHOLDERS.items():
        values = dict.fromkeys(placeholders, "x")
        assert content.render(path, **values)


# ── The validator ────────────────────────────────────────────────────────────

def test_a_placeholder_nothing_supplies_refuses_the_deployment(custom_content):
    """The bug this prevents: "{profissional}" reaching a patient verbatim."""
    custom_content(MINIMAL.replace(
        'confirmation = "Marcado {data} ({dia_semana}) {horario} em {endereco}."',
        'confirmation = "Marcado {data} ({dia_semana}) {horario} com {profissional}."',
    ))
    with pytest.raises(content.ContentError, match="profissional"):
        content.validate()


def test_a_missing_message_refuses_the_deployment(custom_content):
    custom_content(MINIMAL.replace('unknown = "Não sei."', ""))
    with pytest.raises(content.ContentError, match="reply.unknown"):
        content.validate()


def test_an_empty_list_of_prohibitions_refuses_the_deployment(custom_content):
    """This desk talks to patients; the list is not optional."""
    custom_content(MINIMAL.replace('never = ["Nunca dê diagnóstico."]', "never = []"))
    with pytest.raises(content.ContentError, match="never"):
        content.validate()


def test_broken_toml_says_so_rather_than_starting_empty(custom_content):
    custom_content("[voice\ntone = ")
    with pytest.raises(content.ContentError, match="valid TOML"):
        content.messages()


# ── Rendering ────────────────────────────────────────────────────────────────

def test_a_message_renders_its_placeholders(custom_content):
    custom_content(MINIMAL)
    assert content.render("scheduling.cancellation", data="15/09", horario="14:00") == (
        "Cancelado 15/09 14:00."
    )


def test_a_missing_value_raises_instead_of_printing_braces(custom_content):
    custom_content(MINIMAL)
    with pytest.raises(content.ContentError, match="horario"):
        content.render("scheduling.cancellation", data="15/09")


def _teams() -> list[list[dict]]:
    """The committed example always; the real file too where it exists — it is
    kept out of git, so CI checks the shape and a developer checks both."""
    example = json.loads((content._root() / "staff.example.json").read_text(encoding="utf-8"))
    content.reset()
    return [example, content.staff()]


def test_the_conversation_rules_reach_the_answering_prompt():
    """The order products are presented in is the organization's policy, read
    from content — not hard-coded."""
    content.reset()
    rules = content.voice_rules()
    assert "Como conduzir a conversa:" in rules
    assert "agentes de IA, automações e integrações" in rules
    assert "duas frentes" not in rules
    assert rules.index("Como conduzir") < rules.index("Regras absolutas")


@pytest.mark.parametrize("which", [0, 1], ids=["example", "real"])
def test_the_team_loads_whole(which):
    """calendar_id is allowed to be missing — not everyone has a real calendar
    connected yet — but email never is, since staff are matched on it."""
    team = _teams()[which]
    assert all(member["email"] for member in team)
    # escalates_to is matched by e-mail (contacts_repo.set_escalation), never a
    # chat_id or anything else — and it never points outside the team.
    emails = {m["email"] for m in team}
    assert all(m["escalates_to"] in emails for m in team if m.get("escalates_to"))


def test_the_inbox_categories_are_the_organizations_own():
    content.reset()
    names = [item["name"] for item in content.email_categories()]
    assert "PROJETO_SOB_MEDIDA" in names and "ASSISTENTE_JURIDICO" in names
    assert "CONVERGE_CRM" not in names  # the product no longer exists
    assert all(item.get("description") for item in content.email_categories())


# ── The menu labels fit WhatsApp as they are rendered ────────────────────────

from pathlib import Path  # noqa: E402

SHIPPED = (Path(__file__).resolve().parent.parent / "content" / "messages.toml").read_text(
    encoding="utf-8")


def test_the_booking_texts_are_shipped():
    for key in ("days_prompt", "days_button", "days_count", "days_one", "days_only_morning",
                "days_only_afternoon", "days_next", "days_which", "days_tap", "times_prompt",
                "times_button", "times_more", "times_other_day", "day_full", "limit_one",
                "limit_many", "limit_reschedule", "limit_keep", "limit_kept",
                "stale_reschedule", "team_cancel_failed"):
        assert content.text(f"booking.{key}") if "{" not in content._lookup(
            f"booking.{key}") else content._lookup(f"booking.{key}"), key


@pytest.mark.parametrize("key, limit", [("days_button", 20), ("times_button", 20),
                                        ("limit_keep", 20), ("days_next", 24),
                                        ("times_other_day", 24), ("days_only_morning", 72)])
def test_a_label_too_long_for_whatsapp_refuses_the_deployment(custom_content, key, limit):
    import re

    pattern = re.compile(rf'^({key} = )"[^"]*"', re.M)
    assert pattern.search(SHIPPED), key
    custom_content(pattern.sub(lambda m: m.group(1) + '"' + "x" * (limit + 1) + '"', SHIPPED))
    with pytest.raises(content.ContentError, match=f"booking.{key}"):
        content.validate()


_PROPOSAL_STEPS = ("quem", "desafio", "custo_hoje", "canais", "ferramentas", "usuarios", "prazo")


def test_the_proposal_texts_are_shipped():
    keys = ["offer", "button_meet", "button_email", "button_skip", "button_stop", "button_team",
            "button_ok", "button_fix", "confirm", "fix_which", "prefilled_mark", "resume",
            "done", "stopped", "team_sent", "team_incomplete"]
    keys += [f"ask_{s}" for s in _PROPOSAL_STEPS] + [f"label_{s}" for s in _PROPOSAL_STEPS]
    for key in keys:
        assert content._lookup(f"proposal.{key}").strip(), key
    assert "2 dias úteis" in content.text("proposal.done")
    ask = content.text("proposal.ask_desafio")
    for example in ("atendimento e agenda", "documentos", "cobrança", "relatórios",
                    "obra", "jurídico", "site"):
        assert example in ask, example
    assert content.text("proposal.button_email") == "Pedir proposta"
    assert content.text("proposal.button_meet") == "Conversar 30 min"


def test_the_proposal_texts_fill_with_their_placeholders():
    assert "X" in content.render("proposal.confirm", resumo="X")
    assert "P" in content.render("proposal.resume", pergunta="P")
    for key in ("team_sent", "team_incomplete"):
        out = content.render(f"proposal.{key}", nome="Ana", empresa="Acme")
        assert "Ana" in out and "Acme" in out


@pytest.mark.parametrize("key, limit", [
    ("button_meet", 20), ("button_email", 20), ("button_skip", 20), ("button_stop", 20),
    ("button_ok", 20), ("button_fix", 20), ("button_team", 20), ("label_desafio", 24),
    ("label_custo_hoje", 24), ("canal_whatsapp", 24), ("biz_area_documentos_juridico", 24),
    ("sistema_gestao", 24), ("volume_mais_5000", 24), ("processo_sim", 24),
    ("usuarios_mais_10", 24), ("label_usuarios_faixa", 24)])
def test_a_proposal_label_too_long_for_whatsapp_refuses_the_deployment(
        custom_content, key, limit):
    import re

    section = SHIPPED.split("\n[proposal]", 1)[1].split("\n[", 1)[0]
    pattern = re.compile(rf'^({key} = )"[^"]*"', re.M)
    assert pattern.search(section), key
    long_section = pattern.sub(lambda m: m.group(1) + '"' + "x" * (limit + 1) + '"', section)
    custom_content(SHIPPED.replace(section, long_section))
    with pytest.raises(content.ContentError, match=f"proposal.{key}"):
        content.validate()


def test_the_proposal_questions_ask_what_the_owner_wants_to_know():
    quem = content.text("proposal.ask_quem")
    for part in ("nome", "empresa", "ramo", "cargo"):
        assert part in quem, part
    custo = content.text("proposal.ask_custo_hoje")
    assert "pessoas" in custo and "horas por semana" in custo
    canais = content.text("proposal.ask_canais")
    assert "chegam" in canais and "conversas" in canais and "por mês" in canais
    assert content.text("proposal.button_team") == "Falar com a equipe"


def test_stopping_the_questionnaire_leads_into_the_times():
    stopped = content.text("proposal.stopped")
    assert "Quando preferir" not in stopped and "é só me dizer" not in stopped
    assert stopped.endswith(":")


def test_the_team_notice_drops_the_company_part_whole():
    from core.services import proposal_briefing as pb

    for key in ("team_sent", "team_incomplete"):
        with_company = pb.team_notice(f"proposal.{key}", "Ana", "Acme")
        without = pb.team_notice(f"proposal.{key}", "Ana", "")
        assert " · Acme" in with_company and "Ana" in with_company
        assert "()" not in without and "·" not in without and "{" not in without


@pytest.mark.parametrize("key", ["team_sent", "team_incomplete"])
def test_a_team_notice_without_the_company_part_refuses_the_deployment(custom_content, key):
    import re

    pattern = re.compile(rf'^({key} = ".*?) · {{empresa}}', re.M)
    assert pattern.search(SHIPPED), key
    custom_content(pattern.sub(lambda m: m.group(1) + " ({empresa})", SHIPPED))
    with pytest.raises(content.ContentError, match=f"proposal.{key}"):
        content.validate()


def test_the_voice_offers_both_paths_to_someone_who_wants_to_hire():
    rules = content.voice_rules()
    assert "proposta por e-mail" in rules and "30 minutos" in rules


def test_staying_with_the_ai_promises_no_person_and_leads_to_the_proposal():
    text = content.text("reply.stay_with_ai")
    assert "chamar" not in text.lower() and "pessoa" not in text.lower()
    assert "por aqui" in text and "proposta" in text


def test_the_triage_does_not_read_choosing_the_ai_as_asking_for_a_person():
    from core.services import graph
    assert "atendida por voce" in graph._TRIAGE_SYSTEM
    assert "nao e escalation" in graph._TRIAGE_SYSTEM


def test_the_entry_buttons_fit_whatsapp():
    for key in ("reply.button_info", "reply.button_start_proposal",
                "proposal.button_email", "proposal.button_meet"):
        assert len(content.text(key)) <= 20, key


def test_a_price_question_gets_the_proposal_offer_not_a_price():
    offer = content.text("proposal.offer")
    assert "preço de tabela" in offer and "2 dias úteis" in offer and "R$" not in offer
    from core.services import graph
    assert "qualquer pergunta de preco" in graph._TRIAGE_SYSTEM


@pytest.mark.parametrize("key", ["ask_volume", "ask_biz_areas", "chosen", "sistema_nenhum",
                                 "label_processo"])
def test_a_missing_button_question_text_refuses_the_deployment(custom_content, key):
    import re

    pattern = re.compile(rf'^{key} = "[^"]*"\n', re.M)
    assert pattern.search(SHIPPED), key
    custom_content(pattern.sub("", SHIPPED, count=1))
    with pytest.raises(content.ContentError, match=f"proposal.{key}"):
        content.validate()
