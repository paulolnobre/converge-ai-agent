"""The contract PDF made from the model (docs/modelos): the fixed text in
content/contrato/, the parties from the contract data, the Anexo I from the
request and its quote, and the Anexo III from the quote and the price table."""
import io
from datetime import date, datetime

import pytest
from pypdf import PdfReader

from core import content
from core.models import Contact, ProposalBriefing, ProposalQuote
from core.services import contract_pdf, pricing, proposal_quote

NOW = datetime(2026, 11, 10, 12, 0)
TODAY = date(2026, 11, 10)
ANA = Contact(id=7, chat_id="wa:5591999990000", name="Ana Souza", channel="whatsapp")
DETAILS = {"razao_social": "Sol Engenharia Ltda", "documento": "11222333000181",
           "cep": "66000000", "endereco": "Av. Exemplo, 100", "bairro": "Centro",
           "cidade": "Belém", "uf": "PA", "assinante_nome": "Ana Souza",
           "assinante_cpf": "52998224725", "assinante_email": "ana@sol.com.br",
           "assinante_cargo": "Sócia"}
COMPANY = {"areas": "Atendimento e agenda, Vendas, RH", "canais_lista": "WhatsApp, Site",
           "sistemas_lista": "Google Agenda, RD Station"}
CLINIC = {"trilha": "clinica", "areas": "Atendimento e agenda, Pré-triagem, Cobrança e pagamentos",
          "canais_lista": "WhatsApp, Instagram", "sistemas_lista": "nenhum"}


def _t(key: str) -> str:
    return content.text(f"contract_pdf.{key}")


def _briefing(answers: dict[str, str]) -> ProposalBriefing:
    return ProposalBriefing(id=4, contact_id=7, status="proposal_sent", answers=answers,
                            created_at=NOW, updated_at=NOW)


def _quote(scope: dict | None = None, premises: list[str] | None = None) -> ProposalQuote:
    return ProposalQuote(
        briefing_id=4, inputs={}, scope=scope or {"conversas": 1250, "usuarios": 5},
        detail={}, premises=premises or [], table_digest="x", calc_setup_cents=900_000,
        calc_setup_low_cents=850_000, calc_setup_high_cents=990_000,
        calc_monthly_cents=150_000, setup_cents=1_150_000, monthly_cents=190_000,
        created_at=NOW)


def _table() -> pricing.Table:
    table = pricing.load_table()
    assert table is not None
    return table


def _data(answers=None, quote=None, details=None, contact=ANA) -> contract_pdf.Data:
    return contract_pdf.assemble(_briefing(answers or COMPANY), contact, quote or _quote(),
                                 _table(), details or DETAILS, TODAY)


def _text(pdf: bytes) -> str:
    return " ".join(" ".join(page.extract_text() for page in
                             PdfReader(io.BytesIO(pdf)).pages).split())


def _row(rows, label: str) -> str:
    return dict(rows)[_t(label)]


def test_the_shipped_text_is_complete():
    assert contract_pdf.problems() == []


def test_every_area_label_of_the_questionnaires_has_its_text():
    labels = {content.text(f"proposal.{k}") for k in contract_pdf.AREA_KEYS}
    assert labels <= set(contract_pdf.functions())


def test_a_missing_area_is_named(tmp_path, monkeypatch):
    (tmp_path / "corpo.txt").write_text("1.1. x\n", encoding="utf-8")
    (tmp_path / "anexo_ii.txt").write_text("II.1. x\n", encoding="utf-8")
    (tmp_path / "funcoes.toml").write_text('["Vendas"]\nfuncao = "x"\naceite = "y"\n',
                                           encoding="utf-8")
    monkeypatch.setattr(contract_pdf, "_root", lambda: tmp_path)
    contract_pdf.reset()
    try:
        found = contract_pdf.problems(["Vendas", "RH"])
    finally:
        contract_pdf.reset()
    assert any("needs finalidade" in p for p in found)
    assert any('no ["RH"]' in p for p in found)


def test_the_parties_come_from_the_contract_data():
    data = _data()
    assert "Sol Engenharia Ltda" in data.contratante
    assert "CNPJ sob o nº 11.222.333/0001-81" in data.contratante
    assert "Av. Exemplo, 100, Centro, Belém/PA, CEP 66000-000" in data.contratante
    assert "CPF 529.982.247-25" in data.contratante and "ana@sol.com.br" in data.contratante


def test_a_person_signs_with_a_cpf():
    data = _data(details={**DETAILS, "documento": "52998224725"})
    assert "inscrita no CPF sob o nº 529.982.247-25" in data.contratante


def test_annex_i_follows_the_request_and_the_quote():
    data = _data()
    assert [f.area for f in data.funcoes] == ["Atendimento e agenda", "Vendas", "RH"]
    assert _row(data.ficha, "f_canais") == "WhatsApp e Site"
    assert _row(data.ficha, "f_integracoes") == "Google Agenda; RD Station"
    assert _row(data.ficha, "f_volume") == "1.250 Conversas por mês"
    assert _row(data.ficha, "f_usuarios") == "5"
    assert _row(data.ficha, "f_sensiveis") == _t("nao")
    third = _row(data.tratamento, "d_terceiros")
    assert _t("terceiros_meta") in third and _t("terceiros_google") in third
    assert content.render("contract_pdf.terceiros_outro", nome="RD Station") in third
    assert _t("terceiros_asaas") not in third
    assert "Ana Souza, (91) 99999-0000" in _row(data.tratamento, "d_responsavel")


def test_a_clinic_has_sensitive_data_and_the_urgency_notice():
    data = _data(answers=CLINIC)
    assert _row(data.ficha, "f_sensiveis") == _t("sim")
    assert _row(data.ficha, "f_integracoes") == _t("nenhuma_integracao")
    assert _t("responsavel_clinica") in _row(data.tratamento, "d_responsavel")
    assert _row(data.tratamento, "d_categorias") == _t("categorias_clinica")
    assert _t("terceiros_asaas") not in _row(data.tratamento, "d_terceiros"), \
        "billing chosen, but no Asaas named: never a provider the customer did not name"
    with_asaas = _data(answers={**CLINIC, "sistemas_lista": "Asaas"})
    assert _t("terceiros_asaas") in _row(with_asaas.tratamento, "d_terceiros")


def test_an_area_the_quote_did_not_price_is_left_to_the_team():
    quote = _quote(premises=[f"{proposal_quote.UNPRICED_AREA}Vendas"])
    assert [f.area for f in _data(quote=quote).funcoes] == ["Atendimento e agenda", "RH"]


def test_annex_iii_carries_the_quote_and_the_table():
    table = _table()
    data = _data(quote=_quote(scope={"conversas": 1250, "usuarios": 5, "sla": True}))
    assert _row(data.comercial, "c_implantacao").startswith(
        "R$ 11.500,00: 50% (R$ 5.750,00) na assinatura e 50% (R$ 5.750,00) no aceite")
    assert "sem juros" not in _row(data.comercial, "c_implantacao")
    monthly = _data(quote=_quote().model_copy(update={"forma_implantacao": "parcelado"}))
    assert _row(monthly.comercial, "c_implantacao").startswith(
        "R$ 11.500,00, em 6 parcelas mensais: R$ 1.916,70 na assinatura e 5 de R$ 1.916,66")
    assert _row(data.comercial, "c_mensalidade").startswith("R$ 1.900,00 por mês")
    assert _row(data.comercial, "c_sla").startswith("contratado")
    assert _row(data.comercial, "c_validade") == f"{table.validade_dias} dias"
    assert _row(_data().comercial, "c_sla").startswith("não contratado")


def test_the_pdf_has_the_whole_text_and_the_stamp():
    pdf = contract_pdf.build(_data())
    text = _text(pdf)
    assert pdf.startswith(b"%PDF")
    assert _t("selo") in text
    assert "12.10. Normas futuras de IA" in text
    assert "II.10. Fim do tratamento" in text
    assert "Anexo III: Condições comerciais" in text
    assert "Belém, 10 de novembro de 2026." in text
    assert "Sol Engenharia Ltda" in text


def test_what_the_customer_typed_is_shown_as_written():
    pdf = contract_pdf.build(_data(details={**DETAILS, "razao_social": "<b>Sol</b> & Cia"}))
    assert "<b>Sol</b> & Cia" in _text(pdf)


def test_the_file_is_named_after_the_company():
    assert contract_pdf.filename(_data()) == "Contrato Converge - Sol Engenharia Ltda.pdf"


@pytest.mark.parametrize("raw, shown", [
    ("wa:5591999990000", "(91) 99999-0000"), ("wa:559133334444", "(91) 3333-4444"),
    ("42", "")])
def test_the_phone_is_written_the_brazilian_way(raw, shown):
    contact = ANA.model_copy(update={"chat_id": raw, "phone": None})
    assert contract_pdf.phone(contact) == shown


async def test_no_contract_before_the_data_arrives(monkeypatch):
    from core.repositories import contract_details as details_repo
    from core.repositories import proposal_briefings as repo
    from core.repositories import proposal_quotes as quotes_repo

    async def get(conn, briefing_id):
        return _briefing(COMPANY)

    async def quote(conn, briefing_id):
        return _quote()

    async def nothing(conn, briefing_id):
        return None

    monkeypatch.setattr(repo, "get", get)
    monkeypatch.setattr(quotes_repo, "get", quote)
    monkeypatch.setattr(details_repo, "get", nothing)
    with pytest.raises(contract_pdf.NoDetails):
        await contract_pdf.generate(None, 4)


@pytest.mark.parametrize("tool, provider", [
    ("Planilha Microsoft Excel", "terceiros_microsoft"), ("Planilha Google", "terceiros_google"),
    ("Gmail", "terceiros_google"), ("Asaas", "terceiros_asaas"),
    ("Office 365", "terceiros_microsoft")])
def test_a_named_provider_is_listed_once_and_right(tool, provider):
    third = _row(_data(answers={**COMPANY, "sistemas_lista": tool}).tratamento, "d_terceiros")
    assert _t(provider) in third
    others = {"terceiros_microsoft", "terceiros_google", "terceiros_asaas"} - {provider}
    assert not any(_t(key) in third for key in others)


@pytest.mark.parametrize("tool", ["Planilha", "LibreOffice Calc"])
def test_a_tool_of_no_known_provider_is_kept_as_written(tool):
    third = _row(_data(answers={**COMPANY, "sistemas_lista": tool}).tratamento,
                 "d_terceiros")
    assert content.render("contract_pdf.terceiros_outro", nome=tool) in third
    assert _t("terceiros_google") not in third and _t("terceiros_microsoft") not in third
