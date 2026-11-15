"""The "Pedidos de proposta" tab: what each request shows, in which order, by
when the team owes the proposal, and who marked it sent."""
# ruff: noqa: F811
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from core.config import settings
from core.models import Contact, ProposalBriefing
from core.repositories import contacts as contacts_repo
from core.repositories import funnel as funnel_repo
from core.repositories import proposal_briefings as repo
from core.services import proposal_requests
from tests.test_panel import EMAIL, _value, accounts, client, clock, signed_in  # noqa: F401

# Friday 30 Oct 2026, 15:00 in São Paulo.
FRIDAY = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)


def _briefing(briefing_id: int, contact_id: int, status: str, *, submitted: datetime | None,
              incomplete: datetime | None = None, answers: dict[str, str] | None = None,
              prefilled: list[str] | None = None) -> ProposalBriefing:
    return ProposalBriefing(
        id=briefing_id, contact_id=contact_id, status=status,  # type: ignore[arg-type]
        answers=answers or {}, prefilled=prefilled or [],
        created_at=FRIDAY, updated_at=FRIDAY, submitted_at=submitted,
        incomplete_notified_at=incomplete,
    )


# ── Deadline ────────────────────────────────────────────────────────────────

def test_deadline_skips_weekends_and_closed_days(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "office_timezone", "America/Sao_Paulo")
    monkeypatch.setattr(settings, "office_workdays", "1,2,3,4,5")
    assert proposal_requests.deadline(FRIDAY) == date(2026, 11, 3)        # Tuesday
    monkeypatch.setattr(settings, "office_workdays", "1,2,3,4,5,6")
    assert proposal_requests.deadline(FRIDAY) == date(2026, 11, 2)        # Monday


def test_deadline_counts_from_the_day_in_the_office_timezone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "office_timezone", "America/Sao_Paulo")
    monkeypatch.setattr(settings, "office_workdays", "1,2,3,4,5")
    # 01:00 UTC on Saturday is still Friday night in São Paulo.
    late_friday = datetime(2026, 10, 31, 1, 0, tzinfo=UTC)
    assert proposal_requests.deadline(late_friday) == date(2026, 11, 3)


# ── Listing ─────────────────────────────────────────────────────────────────

@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "office_timezone", "America/Sao_Paulo")
    monkeypatch.setattr(settings, "office_workdays", "1,2,3,4,5")
    rows = [
        # Sent on Monday 26th: due Wednesday 28th — late on the 30th.
        _briefing(1, 11, "sent", submitted=datetime(2026, 10, 26, 15, tzinfo=UTC),
                  answers={"quem": "Bia · Acme · Varejo · CEO", "desafio": "Atender no WhatsApp"},
                  prefilled=["quem"]),
        # Abandoned, never submitted: the notice on Wednesday 28th; due Friday 30th.
        _briefing(2, 12, "incomplete_sent", submitted=None,
                  incomplete=datetime(2026, 10, 28, 15, tzinfo=UTC),
                  answers={"quem": "Caio da Padaria Pão Bom"}),
        # Already handled: last, whatever its deadline.
        _briefing(3, 13, "proposal_sent", submitted=datetime(2026, 10, 20, 15, tzinfo=UTC)),
        # Sent today: due Tuesday.
        _briefing(4, 14, "sent", submitted=FRIDAY),
    ]
    people = {
        11: Contact(id=11, name="Beatriz", email="bia@acme.test", phone="+5511999990000"),
        12: Contact(id=12, name="Caio", email="caio@pao.test", organization="Pão Bom"),
        13: Contact(id=13, name="Duda"),
        14: Contact(id=14, name="Edu"),
    }
    monkeypatch.setattr(repo, "requests", lambda conn: _value(list(rows)))
    monkeypatch.setattr(repo, "get", lambda conn, briefing_id: _value(
        next((r for r in rows if r.id == briefing_id), None)))
    monkeypatch.setattr(contacts_repo, "get_by_id",
                        lambda conn, contact_id: _value(people.get(contact_id)))
    from core.repositories import proposal_quotes
    monkeypatch.setattr(proposal_quotes, "get", lambda conn, briefing_id: _value(None))
    from core.repositories import contract_signatures
    monkeypatch.setattr(contract_signatures, "get", lambda conn, briefing_id: _value(None))
    return rows


async def test_listing_orders_open_requests_by_deadline_and_flags_late(stored):
    listed = await proposal_requests.listing(None, now=FRIDAY)
    assert [r["id"] for r in listed] == [1, 2, 4, 3]
    first, incomplete, today, done = listed
    assert first["status"] == "novo" and first["atrasado"] and not first["vence_hoje"]
    assert first["prazo"] == date(2026, 10, 28)
    assert (first["nome"], first["empresa"], first["ramo"]) == ("Bia", "Acme", "Varejo")
    assert (first["email"], first["telefone"]) == ("bia@acme.test", "+5511999990000")
    assert incomplete["status"] == "incompleto" and incomplete["vence_hoje"]
    assert not incomplete["atrasado"]
    # A free-text "quem" is not guessed at: the contact's own fields fill in.
    assert (incomplete["nome"], incomplete["empresa"], incomplete["ramo"]) == ("Caio", "Pão Bom", None)
    assert incomplete["quem"] == "Caio da Padaria Pão Bom"
    assert today["prazo"] == date(2026, 11, 3) and not today["atrasado"] and not today["vence_hoje"]
    assert done["status"] == "proposta enviada" and not done["atrasado"] and not done["vence_hoje"]


async def test_detail_lists_every_answer_as_written_with_what_was_prefilled(stored):
    found = await proposal_requests.detail(None, 1, now=FRIDAY)
    assert found is not None
    answers = {a["rotulo"]: a for a in found["respostas"]}
    assert answers["Nome, empresa e cargo"] == {
        "rotulo": "Nome, empresa e cargo", "resposta": "Bia · Acme · Varejo · CEO", "do_preenchimento": True}
    assert answers["Desafio"]["resposta"] == "Atender no WhatsApp"
    assert not answers["Desafio"]["do_preenchimento"]
    assert answers["Prazo"]["resposta"] == ""
    assert len(found["respostas"]) == 11
    assert await proposal_requests.detail(None, 99, now=FRIDAY) is None


async def test_an_answer_from_before_the_buttons_is_still_shown(stored):
    stored[0] = stored[0].model_copy(update={"answers": {
        **stored[0].answers, "canais": "WhatsApp, 300 por mês", "ferramentas": "planilha"}})
    found = await proposal_requests.detail(None, 1, now=FRIDAY)
    assert found is not None
    answers = {a["rotulo"]: a["resposta"] for a in found["respostas"]}
    assert answers["Canais e volume"] == "WhatsApp, 300 por mês"
    assert answers["Ferramentas"] == "planilha"
    assert len(found["respostas"]) == 13


async def test_a_clinic_abandoned_mid_pick_still_shows_the_areas(stored, monkeypatch):
    """Left before "Pronto": the picks are a draft, and the team still sees them."""
    clinic = _briefing(5, 12, "incomplete_sent", submitted=None, incomplete=FRIDAY,
                       answers={"trilha": "clinica", "areas_rascunho": "Pré-triagem, Prontuários"})
    monkeypatch.setattr(repo, "get", lambda conn, briefing_id: _value(clinic))

    found = await proposal_requests.detail(None, 5, now=FRIDAY)

    assert found is not None
    answers = {a["rotulo"]: a["resposta"] for a in found["respostas"]}
    assert answers["O que automatizar"] == "Pré-triagem, Prontuários"
    assert "Desafio" not in answers and len(answers) == 11


# ── Marking the proposal sent ───────────────────────────────────────────────

@pytest.fixture
def marks(monkeypatch: pytest.MonkeyPatch, stored):
    calls = {"marked": [], "moved": []}
    states = {1: "sent", 2: "incomplete_sent", 3: "proposal_sent", 4: "sent"}

    async def mark_proposal_sent(conn, briefing_id, user_id):
        calls["marked"].append((briefing_id, user_id))
        return states.get(briefing_id) in ("sent", "incomplete_sent")

    async def move_stage(conn, contact_id, to, allowed_from):
        calls["moved"].append((contact_id, to, allowed_from))
        return True

    monkeypatch.setattr(repo, "mark_proposal_sent", mark_proposal_sent)
    monkeypatch.setattr(funnel_repo, "move_stage", move_stage)
    return calls


async def test_mark_sent_moves_the_funnel_and_records_who(marks):
    assert await proposal_requests.mark_sent(None, 1, {"user_id": 5, "email": EMAIL}) is True
    assert marks["marked"] == [(1, 5)]
    assert marks["moved"] == [(11, "proposta", ("novo", "respondido", "agendado"))]


async def test_mark_sent_twice_leaves_the_funnel_alone(marks):
    assert await proposal_requests.mark_sent(None, 3, {"user_id": 5, "email": EMAIL}) is False
    assert marks["moved"] == []


# ── Routes ──────────────────────────────────────────────────────────────────

def test_routes_need_a_session(client):
    assert client.get("/painel/api/proposals").status_code == 401
    assert client.get("/painel/api/proposals/1").status_code == 401
    assert client.post("/painel/api/proposals/1/sent").status_code == 401


def test_the_tab_lists_and_opens_requests(signed_in, stored):
    listed = signed_in.get("/painel/api/proposals")
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    assert [r["id"] for r in listed.json()["proposals"]][-1] == 3
    opened = signed_in.get("/painel/api/proposals/1")
    assert opened.status_code == 200 and len(opened.json()["respostas"]) == 11
    assert signed_in.get("/painel/api/proposals/99").status_code == 404


def test_marking_goes_as_whoever_is_signed_in(signed_in, accounts, marks):
    user_id = next(iter(accounts.users))
    response = signed_in.post("/painel/api/proposals/1/sent")
    assert response.status_code == 200, response.text
    assert marks["marked"] == [(1, user_id)]
    assert signed_in.post("/painel/api/proposals/3/sent").status_code == 409
    assert signed_in.post("/painel/api/proposals/99/sent").status_code == 404


def test_another_site_cannot_mark_a_proposal_sent(signed_in, marks):
    response = signed_in.post("/painel/api/proposals/1/sent",
                              headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert marks["marked"] == []


def test_the_tab_script_is_served(client):
    assert client.get("/painel/assets/propostas.js").status_code == 200


def test_the_proposals_list_never_rebuilds_the_board_under_a_dragged_card():
    """The 30 s reload skips while a card is dragged; the board refresh after
    the proposals load must too, or the drop lands on a board that is gone."""
    from core.web import panel

    script = (panel._STATIC / "painel.js").read_text(encoding="utf-8")
    handler = script.split('addEventListener("proposals:loaded"', 1)[1].split("});", 1)[0]
    assert "renderBoard()" in handler
    assert "if (dragId == null) renderBoard();" in handler


# ── The preliminary quote ───────────────────────────────────────────────────

from core.models import ProposalQuote  # noqa: E402
from core.repositories import panel_auth as auth_repo  # noqa: E402
from core.repositories import proposal_quotes as quotes_repo  # noqa: E402
from core.services import pricing  # noqa: E402


def _quote(**extra: Any) -> ProposalQuote:
    values: dict[str, Any] = dict(briefing_id=1, inputs={}, scope={"complexidade": "basico"},
                  detail={"base": 170000, "fator": "1.00"}, premises=["usuarios"],
                  table_digest="abc", calc_setup_cents=230000, calc_setup_low_cents=220000,
                  calc_setup_high_cents=250000, calc_monthly_cents=63000, setup_cents=230000,
                  monthly_cents=63000, created_at=FRIDAY)
    values.update(extra)
    return ProposalQuote(**values)


@pytest.fixture
def quoted(monkeypatch: pytest.MonkeyPatch, stored):
    rows: dict[int, ProposalQuote] = {1: _quote()}
    edits: list[dict] = []

    async def get(conn, briefing_id):
        return rows.get(briefing_id)

    async def edit(conn, briefing_id, *, setup_cents, monthly_cents, note, user_id,
                   forma=None):
        if briefing_id not in rows:
            return False
        edits.append({"id": briefing_id, "setup": setup_cents, "monthly": monthly_cents,
                      "note": note, "user": user_id, "forma": forma})
        rows[briefing_id] = rows[briefing_id].model_copy(update={
            "setup_cents": setup_cents, "monthly_cents": monthly_cents, "note": note,
            "forma_implantacao": forma or rows[briefing_id].forma_implantacao,
            "edited_by": user_id, "edited_at": FRIDAY})
        return True

    async def user_by_id(conn, user_id):
        return {"id": user_id, "name": "Paula"}

    monkeypatch.setattr(quotes_repo, "get", get)
    monkeypatch.setattr(quotes_repo, "edit", edit)
    monkeypatch.setattr(auth_repo, "user_by_id", user_by_id)
    return {"rows": rows, "edits": edits}


async def test_the_card_shows_the_payment_method(stored):
    stored[0] = stored[0].model_copy(update={"answers": {
        **stored[0].answers, "pagamento": "Pix"}})
    found = await proposal_requests.detail(None, 1, now=FRIDAY)
    assert found is not None
    answers = {a["rotulo"]: a["resposta"] for a in found["respostas"]}
    assert answers["Forma de pagamento"] == "Pix"


async def test_the_card_shows_the_quote(quoted):
    found = await proposal_requests.detail(None, 1, now=FRIDAY)
    assert found is not None
    quote = found["orcamento"]
    assert quote["calculado"] == {"implantacao": 230000, "faixa_min": 220000,
                                  "faixa_max": 250000, "mensalidade": 63000}
    assert quote["atual"] == {"implantacao": 230000, "mensalidade": 63000, "forma": "metade"}
    assert quote["plano"] == {"forma": "metade", "count": 2, "first": 115000, "each": 115000}
    table = pricing.load_table()
    assert table is not None and quote["parcelas_max"] == table.parcelas_max
    assert quote["detalhe"] == {"base": 170000, "fator": "1.00"}
    assert quote["premissas"] == ["Usuários do painel: padrão da tabela"]
    assert quote["avisos"] == [] and quote["editado_por"] is None


async def test_the_card_says_what_is_missing(quoted, stored):
    stored[3] = stored[3].model_copy(update={"answers": {
        "canais_lista": "WhatsApp", "volume": "", "areas": "Vendas"}})
    found = await proposal_requests.detail(None, 4, now=FRIDAY)
    assert found is not None
    assert found["orcamento"] == {"faltam": ["Sistemas", "Conversas por mês",
                                             "Processo definido"]}


async def test_premises_read_as_the_team_reads_them(quoted):
    quoted["rows"][1] = _quote(premises=["usuarios", "documentos", "sla",
                                         "área não calculada: Estoque"])
    found = await proposal_requests.detail(None, 1, now=FRIDAY)
    assert found is not None
    assert found["orcamento"]["premissas"] == [
        "Usuários do painel: padrão da tabela", "Documentos: padrão da tabela",
        "SLA: padrão da tabela", "área não calculada: Estoque"]


async def test_an_old_request_has_no_quote(quoted):
    found = await proposal_requests.detail(None, 2, now=FRIDAY)
    assert found is not None and found["orcamento"] is None


async def test_a_quote_below_the_floor_warns(quoted):
    quoted["rows"][1] = _quote(setup_cents=100, monthly_cents=100)
    found = await proposal_requests.detail(None, 1, now=FRIDAY)
    assert found is not None
    assert found["orcamento"]["avisos"] == ["Implantação abaixo do piso da tabela",
                                            "Mensalidade abaixo do piso da tabela"]


def test_editing_records_who(signed_in, accounts, quoted):
    user_id = next(iter(accounts.users))
    response = signed_in.patch("/painel/api/proposals/1/quote", json={
        "implantacao": "1500.50", "mensalidade": "600", "observacao": "desconto"})
    assert response.status_code == 200, response.text
    assert quoted["edits"] == [{"id": 1, "setup": 150050, "monthly": 60000,
                                "note": "desconto", "user": user_id, "forma": None}]
    quote = response.json()["orcamento"]
    assert quote["atual"] == {"implantacao": 150050, "mensalidade": 60000, "forma": "metade"}
    assert quote["editado_por"] == "Paula" and quote["observacao"] == "desconto"


def test_the_team_chooses_monthly_installments(signed_in, quoted):
    response = signed_in.patch("/painel/api/proposals/1/quote", json={
        "implantacao": "10000", "mensalidade": "600", "forma": "parcelado"})
    assert response.status_code == 200, response.text
    assert quoted["edits"][0]["forma"] == "parcelado"
    quote = response.json()["orcamento"]
    assert quote["atual"]["forma"] == "parcelado"
    assert quote["plano"]["count"] == quote["parcelas_max"]


def test_an_unknown_setup_terms_is_refused(signed_in, quoted):
    response = signed_in.patch("/painel/api/proposals/1/quote", json={
        "implantacao": "10000", "mensalidade": "600", "forma": "entrada"})
    assert response.status_code == 422 and quoted["edits"] == []


def test_editing_below_the_floor_saves_and_warns(signed_in, quoted):
    response = signed_in.patch("/painel/api/proposals/1/quote",
                               json={"implantacao": "1", "mensalidade": "1"})
    assert response.status_code == 200
    assert len(response.json()["orcamento"]["avisos"]) == 2


def test_editing_needs_a_session(client, quoted):
    response = client.patch("/painel/api/proposals/1/quote",
                            json={"implantacao": "1", "mensalidade": "1"})
    assert response.status_code == 401 and quoted["edits"] == []


def test_another_site_cannot_edit_a_quote(signed_in, quoted):
    response = signed_in.patch("/painel/api/proposals/1/quote",
                               json={"implantacao": "1", "mensalidade": "1"},
                               headers={"origin": "https://evil.example"})
    assert response.status_code == 403 and quoted["edits"] == []


@pytest.mark.parametrize("value", ["0", "-10", "10.123", "abc", "1,50"])
def test_an_invalid_value_is_refused(signed_in, quoted, value):
    response = signed_in.patch("/painel/api/proposals/1/quote",
                               json={"implantacao": value, "mensalidade": "600"})
    assert response.status_code == 422 and quoted["edits"] == []


def test_a_long_note_is_refused(signed_in, quoted):
    response = signed_in.patch("/painel/api/proposals/1/quote", json={
        "implantacao": "1", "mensalidade": "1", "observacao": "x" * 501})
    assert response.status_code == 422 and quoted["edits"] == []


def test_editing_without_a_quote_is_404(signed_in, quoted):
    response = signed_in.patch("/painel/api/proposals/4/quote",
                               json={"implantacao": "1", "mensalidade": "1"})
    assert response.status_code == 404
    assert signed_in.patch("/painel/api/proposals/99/quote",
                           json={"implantacao": "1", "mensalidade": "1"}).status_code == 404


async def test_a_failing_quote_still_opens_the_request(quoted, monkeypatch):
    async def broken(conn, briefing_id):
        raise RuntimeError("proposal_quotes indisponível")
    monkeypatch.setattr(quotes_repo, "get", broken)
    found = await proposal_requests.detail(None, 1, now=FRIDAY)
    assert found is not None and found["respostas"]
    assert found["orcamento"] == {"indisponivel": True}


@pytest.mark.parametrize("value", ["1.000", "21474836.48", "99999999"])
def test_a_value_the_database_cannot_hold_or_three_decimals_is_refused(signed_in, quoted, value):
    response = signed_in.patch("/painel/api/proposals/1/quote",
                               json={"implantacao": value, "mensalidade": "600"})
    assert response.status_code == 422 and quoted["edits"] == []


def test_the_largest_value_is_accepted(signed_in, quoted):
    response = signed_in.patch("/painel/api/proposals/1/quote",
                               json={"implantacao": "21474836.47", "mensalidade": "600.5"})
    assert response.status_code == 200
    assert quoted["edits"][0]["setup"] == 2147483647 and quoted["edits"][0]["monthly"] == 60050


# ── The proposal made from the model ────────────────────────────────────────

def test_the_generated_proposal_opens_for_checking(signed_in, quoted):
    response = signed_in.get("/painel/api/proposals/1/pdf")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("inline;") and "Acme" in disposition
    assert response.content.startswith(b"%PDF-")


def test_no_quote_no_generated_proposal(signed_in, quoted):
    from core import content

    response = signed_in.get("/painel/api/proposals/4/pdf")
    assert response.status_code == 409
    assert response.json()["detail"] == content.text("proposal_pdf.sem_orcamento")
    assert signed_in.get("/painel/api/proposals/99/pdf").status_code == 404


def test_the_generated_proposal_needs_a_session(client, quoted):
    assert client.get("/painel/api/proposals/1/pdf").status_code == 401
    assert client.post("/painel/api/proposals/1/document/generated").status_code == 401


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list:
    from core.services import proposal_send

    calls: list = []

    async def send(pool, user, token_hash, briefing_id, upload):
        calls.append((briefing_id, upload))
        return {"sent": True, "notice": "Proposta enviada."}

    monkeypatch.setattr(proposal_send, "send", send)
    return calls


def test_the_generated_proposal_goes_the_way_an_uploaded_one_does(signed_in, quoted, sent):
    response = signed_in.post("/painel/api/proposals/1/document/generated")

    assert response.status_code == 200, response.text
    (briefing_id, upload), = sent
    assert briefing_id == 1 and upload.mime == "application/pdf" and upload.kind == "document"
    assert upload.data.startswith(b"%PDF-")
    assert upload.filename == "Proposta Converge - Acme.pdf"


def test_without_a_quote_nothing_is_sent(signed_in, quoted, sent):
    assert signed_in.post("/painel/api/proposals/4/document/generated").status_code == 409
    assert sent == []


def test_another_site_cannot_send_the_generated_proposal(signed_in, quoted, sent):
    response = signed_in.post("/painel/api/proposals/1/document/generated",
                              headers={"origin": "https://evil.example"})
    assert response.status_code == 403 and sent == []


# ── Asking for more information ─────────────────────────────────────────────

@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list:
    from core.services import proposal_info

    calls: list = []

    async def ask(pool, user, token_hash, briefing_id):
        calls.append((briefing_id, user["user_id"]))
        return {"sent": True, "notice": "Pedido enviado."}

    monkeypatch.setattr(proposal_info, "ask", ask)
    return calls


def test_asking_for_information_goes_as_whoever_is_signed_in(signed_in, accounts, asked):
    user_id = next(iter(accounts.users))

    response = signed_in.post("/painel/api/proposals/1/info")

    assert response.status_code == 200, response.text
    assert response.json() == {"sent": True, "notice": "Pedido enviado."}
    assert asked == [(1, user_id)]


def test_asking_for_information_needs_a_session(client, asked):
    assert client.post("/painel/api/proposals/1/info").status_code == 401
    assert asked == []


def test_another_site_cannot_ask_for_information(signed_in, asked):
    response = signed_in.post("/painel/api/proposals/1/info",
                              headers={"origin": "https://evil.example"})
    assert response.status_code == 403 and asked == []


def test_a_refusal_reaches_the_screen_as_it_is(signed_in, monkeypatch):
    from core.services import proposal_info
    from core.services.inbox import InboxError

    async def refuse(pool, user, token_hash, briefing_id):
        raise InboxError(409, "Assuma a conversa para responder.")

    monkeypatch.setattr(proposal_info, "ask", refuse)

    response = signed_in.post("/painel/api/proposals/1/info")

    assert response.status_code == 409
    assert response.json()["detail"] == "Assuma a conversa para responder."


# ── The contract's data in the card ─────────────────────────────────────────

async def test_the_card_shows_the_contract_data_as_the_team_reads_it(stored, monkeypatch):
    from core.repositories import contract_details

    async def get(conn, briefing_id):
        return {"razao_social": "Acme Ltda", "documento": "11222333000181", "cep": "66000000",
                "endereco": "Av. Brasil, 10", "bairro": "Centro", "cidade": "Belém",
                "uf": "PA", "assinante_nome": "Bia", "assinante_cpf": "52998224725",
                "assinante_email": "bia@acme.test", "assinante_cargo": "CEO"} \
            if briefing_id == 1 else None

    monkeypatch.setattr(contract_details, "get", get)

    found = await proposal_requests.detail(None, 1, now=FRIDAY)
    assert found is not None
    assert [item["valor"] for item in found["contrato"]] == [
        "Acme Ltda", "11.222.333/0001-81", "Av. Brasil, 10, Centro, Belém/PA, CEP 66000-000",
        "Bia (CEO), CPF 529.982.247-25, bia@acme.test"]
    other = await proposal_requests.detail(None, 4, now=FRIDAY)
    assert other is not None and other["contrato"] is None


# ── The contract made from the model ────────────────────────────────────────

@pytest.fixture
def contract_data(monkeypatch: pytest.MonkeyPatch):
    from core.repositories import contract_details

    async def get(conn, briefing_id):
        return {"razao_social": "Acme Ltda", "documento": "11222333000181", "cep": "66000000",
                "endereco": "Av. Brasil, 10", "bairro": "Centro", "cidade": "Belém",
                "uf": "PA", "assinante_nome": "Bia", "assinante_cpf": "52998224725",
                "assinante_email": "bia@acme.test", "assinante_cargo": "CEO"} \
            if briefing_id == 1 else None

    monkeypatch.setattr(contract_details, "get", get)


def test_the_generated_contract_opens_for_checking(signed_in, quoted, contract_data):
    response = signed_in.get("/painel/api/proposals/1/contract.pdf")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("inline;") and "Contrato" in disposition
    assert response.content.startswith(b"%PDF-")


def test_no_contract_without_the_data_or_the_quote(signed_in, quoted, contract_data):
    from core import content

    quoted["rows"][4] = quoted["rows"][1]
    response = signed_in.get("/painel/api/proposals/4/contract.pdf")
    assert response.status_code == 409
    assert response.json()["detail"] == content.text("contract_pdf.sem_dados")
    del quoted["rows"][1]
    response = signed_in.get("/painel/api/proposals/1/contract.pdf")
    assert response.json()["detail"] == content.text("proposal_pdf.sem_orcamento")
    assert signed_in.get("/painel/api/proposals/99/contract.pdf").status_code == 404


def test_the_generated_contract_needs_a_session(client, quoted, contract_data):
    assert client.get("/painel/api/proposals/1/contract.pdf").status_code == 401


# ── Sending the contract to be signed ───────────────────────────────────────

async def test_the_card_has_no_signature_before_it_is_sent(stored, monkeypatch):
    monkeypatch.setattr(settings, "autentique_api_key", "")

    row = await proposal_requests.detail(None, 1, now=FRIDAY)

    assert row is not None
    assert row["assinatura"] is None and row["assinatura_pronta"] is False


async def test_the_card_says_who_sent_it_to_be_signed(stored, monkeypatch):
    from core.repositories import contract_signatures
    from core.repositories import panel_auth as auth_repo

    sent = contract_signatures.Sent(1, "sent", True, "doc1", "sig", 5, FRIDAY)
    monkeypatch.setattr(contract_signatures, "get", lambda conn, briefing_id: _value(sent))
    monkeypatch.setattr(auth_repo, "user_by_id",
                        lambda conn, user_id: _value({"name": "Paulo"} if user_id == 5 else None))

    row = await proposal_requests.detail(None, 1, now=FRIDAY)

    assert row is not None
    assert row["assinatura"] == {"status": "enviado", "sandbox": True, "enviado_em": FRIDAY,
                                 "enviado_por": "Paulo", "cliente_assinou_em": None,
                                 "converge_assinou_em": None, "assinado_em": None,
                                 "arquivo_assinado": None}


@pytest.fixture
def signed(monkeypatch: pytest.MonkeyPatch) -> list:
    from core.services import contract_sign

    calls: list = []

    async def send(pool, user, token_hash, briefing_id):
        calls.append((briefing_id, user["user_id"]))
        return {"sent": True, "sandbox": True, "notice": "Contrato enviado."}

    monkeypatch.setattr(contract_sign, "send", send)
    return calls


def test_sending_to_be_signed_goes_as_whoever_is_signed_in(signed_in, accounts, signed):
    response = signed_in.post("/painel/api/proposals/1/contract/sign")

    assert response.status_code == 200, response.text
    assert response.json()["notice"] == "Contrato enviado."
    assert signed == [(1, next(iter(accounts.users)))]


def test_sending_to_be_signed_needs_a_session(client, signed):
    assert client.post("/painel/api/proposals/1/contract/sign").status_code == 401
    assert signed == []


def test_another_site_cannot_send_to_be_signed(signed_in, signed):
    response = signed_in.post("/painel/api/proposals/1/contract/sign",
                              headers={"origin": "https://evil.example"})
    assert response.status_code == 403 and signed == []


def test_the_card_script_has_the_sign_button():
    script = (Path(__file__).resolve().parent.parent
              / "core/web/static/painel/propostas.js").read_text()
    assert "/contract/sign" in script and "Enviar para assinatura" in script
    assert "r.assinatura_pronta" in script


def test_releasing_needs_a_session_and_the_same_site(client, signed_in, monkeypatch):
    from core.services import contract_sign

    calls: list = []

    async def release(pool, user, token_hash, briefing_id):
        calls.append(briefing_id)
        return {"released": True, "notice": "Liberado."}

    monkeypatch.setattr(contract_sign, "release", release)
    assert signed_in.post("/painel/api/proposals/1/contract/release",
                          headers={"origin": "https://evil.example"}).status_code == 403
    assert signed_in.post("/painel/api/proposals/1/contract/release").status_code == 200
    assert calls == [1]
