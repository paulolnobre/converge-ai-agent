"""The proposal PDF made from the model (docs/modelos): the request's answers
and its quote as the team left it, in the Converge layout."""
import io
from datetime import date, datetime

import pytest
from pypdf import PdfReader

from core import content
from core.models import Contact, ProposalBriefing, ProposalQuote
from core.services import pricing, proposal_pdf

NOW = datetime(2026, 11, 9, 12, 0)
TODAY = date(2026, 11, 9)


def _t(key: str) -> str:
    return content.text(f"proposal_pdf.{key}")


def _briefing(answers: dict[str, str]) -> ProposalBriefing:
    return ProposalBriefing(id=4, contact_id=7, status="sent", answers=answers,
                            created_at=NOW, updated_at=NOW)


COMPANY = {
    "quem": "Ana Souza · Sol Engenharia · Construção · Sócia",
    "desafio": "Responder orçamentos fora do horário",
    "areas": "Atendimento e agenda, Vendas, RH",
    "custo_hoje": "duas pessoas meio período",
    "canais_lista": "WhatsApp, Site",
    "sistemas_lista": "Outlook, RD Station",
    "pagamento": "Pix",
}


def _quote(setup: int = 1_150_000, monthly: int = 190_000) -> ProposalQuote:
    return ProposalQuote(
        briefing_id=4, inputs={}, scope={"conversas": 1250, "usuarios": 5}, detail={},
        premises=[], table_digest="x", calc_setup_cents=900_000, calc_setup_low_cents=850_000,
        calc_setup_high_cents=990_000, calc_monthly_cents=150_000, setup_cents=setup,
        monthly_cents=monthly, created_at=NOW)


def _table() -> pricing.Table:
    table = pricing.load_table()
    assert table is not None, "content/pricing.toml ships with the repository"
    return table


def _text(pdf: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_installments_match_the_model():
    assert proposal_pdf.installments(1_150_000, 6) == (191_670, 191_666)
    assert proposal_pdf.installments(600_000, 6) == (100_000, 100_000)
    assert proposal_pdf.installments(500_000, 1) == (500_000, 500_000)


def test_money_reads_as_in_brazil():
    assert proposal_pdf.brl(1_150_000) == "R$ 11.500,00"
    assert proposal_pdf.brl(40) == "R$ 0,40"


def test_the_pdf_carries_the_request_and_the_edited_quote():
    data = proposal_pdf.assemble(_briefing(COMPANY), Contact(id=7, name="Ana"), _quote(),
                                 _table(), TODAY)

    pdf = proposal_pdf.build(data)

    assert pdf.startswith(b"%PDF-")
    text = _flat(_text(pdf))
    for expected in ("Sol Engenharia", "Ana Souza, Sócia", "09/11/2026", "16/11/2026",
                     "Responder orçamentos fora do horário",
                     "atendimento e agenda, vendas e RH", "Outlook e RD Station",
                     "WhatsApp e Site", "até 1.250 conversas por mês", "5 pessoas da equipe",
                     "R$ 11.500,00", "50% na assinatura (R$ 5.750,00) e 50% na entrega da implantação (R$ 5.750,00)",
                     "Forma de pagamento escolhida: Pix", "R$ 1.900,00", "R$ 1.710,00",
                     "R$ 0,50", _flat(_t("dados_empresa")), "CNPJ 11.222.333/0001-81"):
        assert expected in text, expected
    assert "R$ 9.000,00" not in text, "the table's value, not the team's, would be wrong"


def test_a_clinic_reads_as_a_clinic():
    answers = {"trilha": "clinica", "quem": "Ana · Clínica Sol · Clínica · Sócia",
               "como_hoje": "a recepção, à mão", "equipe": "3 médicas",
               "areas": "Pré-triagem", "canais_lista": "WhatsApp", "sistemas_lista": "Nenhum"}

    text = _flat(_text(proposal_pdf.build(proposal_pdf.assemble(
        _briefing(answers), Contact(id=7), _quote(), _table(), TODAY))))

    assert "a recepção, à mão" in text and "3 médicas" in text
    assert _flat(_t("dados_clinica")) in text
    assert _t("nenhuma_integracao") in text


def test_what_the_person_typed_cannot_break_the_layout():
    answers = {**COMPANY, "desafio": "<b>urgente</b> & <script>x</script>"}

    text = _text(proposal_pdf.build(proposal_pdf.assemble(
        _briefing(answers), Contact(id=7), _quote(), _table(), TODAY)))

    assert "<b>urgente</b> & <script>x</script>" in text


def test_without_the_four_parts_the_contact_names_the_client():
    data = proposal_pdf.assemble(_briefing({**COMPANY, "quem": "Ana, da Sol"}),
                                 Contact(id=7, name="Ana Souza", organization="Sol Ltda"),
                                 _quote(), _table(), TODAY)

    assert data.empresa == "Sol Ltda" and data.cuidados == "Ana Souza"
    assert proposal_pdf.filename(data) == "Proposta Converge - Sol Ltda.pdf"


async def test_no_quote_no_proposal(monkeypatch: pytest.MonkeyPatch):
    from core.repositories import contacts as contacts_repo
    from core.repositories import proposal_briefings as repo
    from core.repositories import proposal_quotes as quotes_repo

    async def get(conn, briefing_id):
        return _briefing(COMPANY)

    async def no_quote(conn, briefing_id):
        return None

    async def contact(conn, contact_id):
        return Contact(id=7)

    monkeypatch.setattr(repo, "get", get)
    monkeypatch.setattr(quotes_repo, "get", no_quote)
    monkeypatch.setattr(contacts_repo, "get_by_id", contact)

    with pytest.raises(proposal_pdf.NoQuote):
        await proposal_pdf.generate(None, 4, today=TODAY)


def test_the_loyalty_price_is_exactly_the_discount_announced():
    data = proposal_pdf.assemble(_briefing(COMPANY), Contact(id=7), _quote(monthly=60_050),
                                 _table(), TODAY)
    assert data.fidelidade_cents == 54_045
    assert data.fidelidade_pct == 10


def test_an_area_the_quote_did_not_price_is_not_promised():
    quote = _quote().model_copy(update={"premises": ["área não calculada: Estoque"]})
    answers = {**COMPANY, "areas": "Vendas, Estoque"}

    data = proposal_pdf.assemble(_briefing(answers), Contact(id=7), quote, _table(), TODAY)

    assert dict(data.escopo)[_t("escopo_funcoes")] == "Vendas"
    assert "vendas e estoque" in " ".join(data.contou), "what they told us stays"
