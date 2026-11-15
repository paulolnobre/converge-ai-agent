"""What the contract needs, asked after "Aceitar proposta": the WhatsApp Flow
`dados_contrato`, opened with what is known, checked, and kept sealed."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from core import channels, content
from core.models import Contact, ProposalBriefing
from core.repositories import billing as billing_repo
from core.repositories import contract_details as details_repo
from core.repositories import proposal_briefings as repo
from core.services import forms, inbox, proposal_contract

NOW = datetime(2026, 11, 10, 12, 0)
ANA = Contact(id=7, chat_id="wa:5591999990000", name="Ana Souza", email="ana@sol.com.br",
              channel="whatsapp")
GOOD = {"razao_social": "Sol Engenharia Ltda", "documento": "11.222.333/0001-81",
        "cep": "66000-000", "endereco": "Av. Exemplo, 100", "bairro": "Centro",
        "cidade": "Belém", "uf": "pa", "assinante_nome": "Ana Souza",
        "assinante_cpf": "529.982.247-25", "assinante_email": "ana@sol.com.br",
        "assinante_cargo": "Sócia"}


def _briefing(status: str = "proposal_sent", contact_id: int = 7) -> ProposalBriefing:
    return ProposalBriefing(id=4, contact_id=contact_id, status=status,  # type: ignore[arg-type]
                            answers={"quem": "Ana Souza · Sol Engenharia · Construção · Sócia"},
                            created_at=NOW, updated_at=NOW)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import shutil
    from pathlib import Path

    folder = tmp_path / "forms"
    shutil.copytree(Path(__file__).resolve().parent.parent / "content" / "forms", folder)
    (folder / "flows.toml").write_text('[dados_contrato]\nflow_id = "F-CON"\n',
                                       encoding="utf-8")
    monkeypatch.setattr(forms, "_dir", lambda: folder)
    forms.reset()
    w = SimpleNamespace(briefing=_briefing(), saved={}, team=[], pushed=[], earlier=None,
                        tax_id=None)

    async def get(conn, briefing_id):
        return w.briefing if briefing_id == 4 else None

    async def proposal_sent_for(conn, contact_id):
        return w.briefing if w.briefing.status == "proposal_sent" else None

    async def save(conn, briefing_id, data):
        w.saved[briefing_id] = dict(data)

    async def load(conn, briefing_id):
        return w.saved.get(briefing_id)

    async def latest_of_contact(conn, contact_id, but):
        return w.earlier

    async def customer_of(conn, contact_id):
        return {"id": 1, "provider_id": "cus_1", "tax_id": w.tax_id} if w.tax_id else None

    async def notify_team(text, contact_id):
        w.team.append(text)
        return True

    monkeypatch.setattr(repo, "get", get)
    monkeypatch.setattr(repo, "proposal_sent_for", proposal_sent_for)
    monkeypatch.setattr(details_repo, "save", save)
    monkeypatch.setattr(details_repo, "get", load)
    monkeypatch.setattr(details_repo, "latest_of_contact", latest_of_contact)
    monkeypatch.setattr(billing_repo, "customer_of", customer_of)
    monkeypatch.setattr(inbox, "notify_team", notify_team)
    monkeypatch.setattr(inbox, "push_to_team", lambda text, contact: w.pushed.append(text))
    return w


def _flow(keyboard) -> dict:
    return keyboard["_flow"]


def test_the_shipped_flow_keeps_the_contract():
    assert not [p for p in forms.problems() if "dados_contrato" in p]


def test_good_data_is_kept_in_digits_and_capitals():
    kept, wrong = proposal_contract.check(dict(GOOD))

    assert wrong == []
    assert kept["documento"] == "11222333000181" and kept["assinante_cpf"] == "52998224725"
    assert kept["cep"] == "66000000" and kept["uf"] == "PA"


@pytest.mark.parametrize("key, value", [
    ("documento", "11.222.333/0001-80"), ("documento", "123"),
    ("assinante_cpf", "529.982.247-24"), ("assinante_cpf", "11.222.333/0001-81"),
    ("assinante_email", "ana@"), ("assinante_email", "x ana@sol.com.br"),
    ("cep", "6606"), ("uf", "XX"), ("razao_social", ""), ("assinante_cargo", "")])
def test_what_is_wrong_is_named(key, value):
    _, wrong = proposal_contract.check({**GOOD, key: value})
    assert wrong == [key]


async def test_after_accepting_the_flow_opens_with_what_is_known(world):
    found = await proposal_contract.after_accept(None, ANA)
    assert found is not None
    text, keyboard = found
    flow = _flow(keyboard)
    assert text == content.text("contract.flow_intro")
    assert flow["flow_id"] == "F-CON" and flow["token"] == "contrato:4"
    assert flow["screen"] == "EMPRESA"
    assert flow["data"]["razao_social"] == "Sol Engenharia"
    assert flow["data"]["assinante_nome"] == "Ana Souza"
    assert flow["data"]["assinante_email"] == "ana@sol.com.br"
    assert flow["data"]["assinante_cargo"] == "Sócia"
    assert flow["data"]["documento"] == "", "only what is known"


async def test_what_was_sent_before_opens_the_flow_again(world):
    world.saved[4] = {**GOOD, "razao_social": "Sol Engenharia S.A."}

    found = await proposal_contract.after_accept(None, ANA)

    assert found is not None
    assert _flow(found[1])["data"]["razao_social"] == "Sol Engenharia S.A."


async def test_an_earlier_contract_and_a_charge_fill_what_they_know(world):
    world.tax_id = "52998224725"
    world.earlier = {**GOOD, "documento": "11222333000181", "assinante_cargo": "Diretora",
                     "estranho": "x", "cidade": ["não é texto"]}

    found = await proposal_contract.after_accept(None, ANA)

    assert found is not None
    data = _flow(found[1])["data"]
    assert data["documento"] == "11222333000181", "the earlier contract is more formal"
    assert data["endereco"] == GOOD["endereco"] and data["assinante_cargo"] == "Diretora"
    assert data["cidade"] == "", "only text is reused"
    assert "estranho" not in data


async def test_a_charge_alone_fills_the_document(world):
    world.tax_id = "11222333000181"

    found = await proposal_contract.after_accept(None, ANA)

    assert found is not None and _flow(found[1])["data"]["documento"] == "11222333000181"


async def test_what_was_sent_for_this_proposal_wins_over_an_earlier_contract(world):
    world.earlier = {**GOOD, "cep": "66000000"}
    world.saved[4] = {**GOOD, "cep": "66000000"}

    found = await proposal_contract.after_accept(None, ANA)

    assert found is not None and _flow(found[1])["data"]["cep"] == "66000000"


async def test_no_flow_outside_whatsapp_or_unpublished_and_the_team_asks(world, monkeypatch):
    telegram = ANA.model_copy(update={"chat_id": "42", "channel": "telegram"})
    assert await proposal_contract.after_accept(None, telegram) is None
    assert len(world.team) == 1

    monkeypatch.setattr(forms, "_flow_ids", dict)
    forms.reset()
    assert await proposal_contract.after_accept(None, ANA) is None
    assert len(world.team) == 2


async def test_no_proposal_sent_nothing_to_ask(world):
    world.briefing = _briefing(status="sent")

    assert await proposal_contract.after_accept(None, ANA) is None
    assert world.team == []


async def test_the_answer_is_kept_and_the_team_told(world):
    text, keyboard = await proposal_contract.apply(None, ANA, "contrato:4", dict(GOOD))

    assert text == content.text("contract.received") and keyboard is None
    assert world.saved[4]["documento"] == "11222333000181"
    assert len(world.team) == 1 and "Ana Souza" in world.team[0]


async def test_a_test_contact_is_not_announced_in_the_group(world):
    tester = ANA.model_copy(update={"test_mode": "novo"})

    await proposal_contract.apply(None, tester, "contrato:4", dict(GOOD))

    assert world.saved and world.team == [] and world.pushed


async def test_something_wrong_comes_back_with_what_was_sent(world):
    sent = {**GOOD, "assinante_cpf": "111.111.111-11", "cep": "123"}

    text, keyboard = await proposal_contract.apply(None, ANA, "contrato:4", sent)

    assert text == content.render("contract.fix", campos="o CEP e o CPF de quem assina")
    assert keyboard is not None
    data = _flow(keyboard)["data"]
    assert data["razao_social"] == "Sol Engenharia Ltda" and data["cep"] == "123"
    assert world.saved == {} and world.team == []


@pytest.mark.parametrize("token, briefing", [
    ("contrato:5", _briefing()), ("contrato:4", _briefing(status="sent")),
    ("contrato:4", _briefing(contact_id=8)), ("contrato:²", _briefing()),
    ("contrato:" + "9" * 30, _briefing())])
async def test_an_answer_for_no_open_proposal_keeps_nothing(world, token, briefing):
    world.briefing = briefing

    text, keyboard = await proposal_contract.apply(None, ANA, token, dict(GOOD))

    assert text == content.text("contract.not_open") and keyboard is None
    assert world.saved == {}


async def test_a_value_that_is_not_text_counts_as_missing(world):
    _, keyboard = await proposal_contract.apply(None, ANA, "contrato:4",
                                                {**GOOD, "cidade": ["Belém"]})
    assert keyboard is not None and world.saved == {}


def test_the_flow_carries_every_key_the_code_reads():
    form = forms.load("dados_contrato")
    assert form is not None
    assert set(form.fields) == set(proposal_contract.KEYS)
    assert set(proposal_contract.KEYS) <= set(form.prefill), "every field can be refilled"
    assert channels.is_whatsapp(ANA.chat_id)


# ── Through the handler: "Aceitar proposta", then the Flow ──────────────────

from tests.test_desk_flow import CONN, desk, tap  # noqa: E402, F401
from tests.test_proposal_flow import wa_sends  # noqa: E402, F401


async def test_accepting_sends_the_answer_then_the_contract_flow(desk, world, wa_sends):  # noqa: F811
    from core.handlers import message as handler

    await handler.process_callback(CONN, tap("propdoc:accept"))

    texts = [text for text, _ in wa_sends["sent"]]
    assert texts == [content.text("proposal_doc.accept_reply"),
                     content.text("contract.flow_intro")]
    assert wa_sends["sent"][-1][1]["_flow"]["token"] == "contrato:4"


async def test_declining_sends_no_flow(desk, world, wa_sends):  # noqa: F811
    from core.handlers import message as handler

    await handler.process_callback(CONN, tap("propdoc:decline"))

    assert all("_flow" not in (keyboard or {}) for _, keyboard in wa_sends["sent"])


async def test_the_flow_answer_is_kept_through_the_handler(desk, world, wa_sends):  # noqa: F811
    from core.handlers import message as handler

    update = tap("flow:contrato:4", "cbflow")
    update["callback_query"]["_flow"] = dict(GOOD)
    await handler.process_callback(CONN, update)

    assert world.saved[4]["assinante_cpf"] == "52998224725"
    assert wa_sends["sent"][-1][0] == content.text("contract.received")


async def test_with_the_conversation_held_the_answer_is_still_kept(desk, world, wa_sends):  # noqa: F811
    from core.handlers import message as handler
    from tests.test_desk_flow import _held_in_the_panel

    _held_in_the_panel(desk)
    update = tap("flow:contrato:4", "cbflow")
    update["callback_query"]["_flow"] = dict(GOOD)

    assert await handler.process_callback(CONN, update) == "handoff"

    assert world.saved[4]["documento"] == "11222333000181"
    assert wa_sends["sent"][-1][0] == content.text("contract.received")
    assert desk.sessions[1]["status"] == "claimed", "the person keeps the conversation"


async def test_a_refused_flow_tells_the_person_and_the_team(desk, world, wa_sends):  # noqa: F811
    from core.handlers import message as handler

    wa_sends["refuse_flow"] = True

    await handler.process_callback(CONN, tap("propdoc:accept"))

    assert wa_sends["sent"][-1][0] == content.text("contract.fallback")
    assert any("formulário do contrato não foi" in line for line in world.team)


def test_a_long_email_is_wrong_never_cut_into_another():
    long_email = "a" * 250 + "@x.com"
    _, wrong = proposal_contract.check({**GOOD, "assinante_email": long_email})
    assert wrong == ["assinante_email"]
    _, wrong = proposal_contract.check({**GOOD, "razao_social": "x" * 201})
    assert wrong == ["razao_social"]


async def test_a_refused_correction_tells_the_team_too(desk, world, wa_sends):  # noqa: F811
    from core.handlers import message as handler

    wa_sends["refuse_flow"] = True
    update = tap("flow:contrato:4", "cbflow")
    update["callback_query"]["_flow"] = {**GOOD, "cep": "1"}

    await handler.process_callback(CONN, update)

    assert wa_sends["sent"][-1][0] == content.text("contract.fallback")
    assert any("formulário do contrato não foi" in line for line in world.team)
