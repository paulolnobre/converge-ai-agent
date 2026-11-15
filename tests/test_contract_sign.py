"""The contract to Autentique from the panel: who signs, sandbox or not, sent
once, released when Autentique surely refused, and the link in the
conversation only while it can go."""
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from core import content
from core.config import settings
from core.integrations import autentique
from core.models import Contact, ProposalBriefing
from core.repositories import contacts as contacts_repo
from core.repositories import contract_details as details_repo
from core.repositories import contract_signatures as signatures_repo
from core.repositories import proposal_briefings as repo
from core.services import contract_pdf, contract_sign, inbox, panel_uploads
from core.services.inbox import InboxError
from core.services.pricing import SetupPlan

TERMS = SetupPlan("metade", 2, 500_000, 500_000)

PAULO = {"user_id": 1, "name": "Paulo"}
NOW = datetime(2026, 11, 12, 12, 0)
DETAILS = {"razao_social": "Clínica Bem", "assinante_nome": "Ana Lima Souza",
           "assinante_email": "Ana@Cliente.com", "assinante_cpf": "52998224725"}


class _Conn:
    async def execute(self, sql, params=None):
        return None


class _Pool:
    @asynccontextmanager
    async def connection(self):
        yield _Conn()


@pytest.fixture
def w(monkeypatch):
    w = SimpleNamespace(
        existing=None, claimed=[], released=[], marked=[], created=[], links=[], sent=[],
        recorded=[], create_error=None, link_error=None, owner=True, window=True,
        delivery="ok", test_mode=None, stale=False, kept=True, slow=False, details=dict(DETAILS), no_details=False)
    monkeypatch.setattr(settings, "autentique_api_key", "k")
    monkeypatch.setattr(settings, "autentique_sandbox", False)
    monkeypatch.setattr(settings, "contract_signer_name", "Paulo Nobre")
    monkeypatch.setattr(settings, "contract_signer_email", "Paulo@Converge.me")

    async def get(conn, briefing_id):
        return ProposalBriefing(id=briefing_id, contact_id=9, status="proposal_sent",
                                answers={}, created_at=NOW, updated_at=NOW)

    async def get_by_id(conn, contact_id):
        return Contact(id=9, chat_id="wa:5591999990000", name="Ana", channel="whatsapp",
                       test_mode=w.test_mode)

    async def lock_as_owner(conn, contact, user, *, past_window=False):
        if not w.owner:
            raise InboxError(409, "Ana assumiu esta conversa.")
        if not past_window and not w.window:
            raise InboxError(409, "janela fechada")
        return 5

    async def require_live_session(conn, user, token_hash):
        return None

    async def generate(conn, briefing_id):
        if w.no_details:
            raise contract_pdf.NoDetails(briefing_id)
        return b"%PDF-contrato", "Contrato Converge - Clínica Bem.pdf", TERMS

    async def details(conn, briefing_id):
        return w.details

    async def existing(conn, briefing_id):
        return w.existing

    async def claim(conn, briefing_id, user_id, *, sandbox, terms=None):
        assert terms == TERMS
        w.claimed.append((briefing_id, user_id, sandbox))
        return 70

    async def release(conn, attempt):
        w.released.append(attempt)

    async def mark_sent(conn, attempt, document_id, customer, converge):
        w.marked.append((attempt, document_id, customer, converge))
        return w.kept

    async def create_document(name, pdf, filename, signers, *, message="", sandbox):
        w.created.append((name, filename, signers, sandbox))
        if w.slow:
            import asyncio
            await asyncio.sleep(1)
        if w.create_error:
            raise w.create_error
        return autentique.Created("doc1", {"ana@cliente.com": "sig-ana",
                                           "paulo@converge.me": "sig-paulo"})

    async def signing_link(public_id):
        w.links.append(public_id)
        if w.link_error:
            raise w.link_error
        return "https://assina.ae/A9"

    async def send(channel, chat_id, text):
        w.sent.append((chat_id, text))
        return w.delivery

    async def record_message(conn, contact_id, chat_id, role, message, intent=None,
                             author_user_id=None):
        w.recorded.append((message, intent, author_user_id))

    monkeypatch.setattr(repo, "get", get)
    monkeypatch.setattr(contacts_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(contacts_repo, "record_message", record_message)
    monkeypatch.setattr(inbox, "lock_as_owner", lock_as_owner)
    monkeypatch.setattr(inbox, "send", send)
    monkeypatch.setattr(panel_uploads, "require_live_session", require_live_session)
    monkeypatch.setattr(contract_pdf, "generate_with_terms", generate)
    monkeypatch.setattr(details_repo, "get", details)
    monkeypatch.setattr(signatures_repo, "get", existing)
    monkeypatch.setattr(signatures_repo, "claim", claim)
    monkeypatch.setattr(signatures_repo, "release", release)
    monkeypatch.setattr(signatures_repo, "mark_sent", mark_sent)
    monkeypatch.setattr(autentique, "create_document", create_document)
    monkeypatch.setattr(autentique, "signing_link", signing_link)
    return w


async def _send():
    return await contract_sign.send(_Pool(), PAULO, "hash", 7)


async def test_sent_with_both_signers_and_the_link_in_the_conversation(w):
    result = await _send()

    name, filename, signers, sandbox = w.created[0]
    assert filename == "Contrato Converge - Clínica Bem.pdf"
    assert name == "Contrato Converge - Clínica Bem"
    assert signers == [autentique.Signer("Ana Lima Souza", "ana@cliente.com", "52998224725"),
                       autentique.Signer("Paulo Nobre", "paulo@converge.me")]
    assert sandbox is False
    assert w.claimed == [(7, 1, False)]
    assert w.marked == [(70, "doc1", "sig-ana", "sig-paulo")]
    assert w.links == ["sig-ana"]
    text = w.sent[0][1]
    assert text.startswith("Ana, ") and "https://assina.ae/A9" in text and "ana@cliente.com" in text
    assert w.recorded == [(text, "contract_sign", 1)]
    assert result == {"sent": True, "sandbox": False,
                      "notice": content.text("contract_sign.sent")}


@pytest.mark.parametrize(("setting", "test_mode"), [(True, None), (False, "novo")])
async def test_sandbox_by_setting_or_for_a_test_contact(w, monkeypatch, setting, test_mode):
    monkeypatch.setattr(settings, "autentique_sandbox", setting)
    w.test_mode = test_mode

    assert (await _send())["sandbox"] is True
    assert w.created[0][3] is True and w.claimed[0][2] is True


async def test_sent_once(w):
    w.existing = signatures_repo.Sent(7, "sent", False, "doc1", "sig-ana", 1, NOW)

    with pytest.raises(InboxError) as raised:
        await _send()

    assert raised.value.status == 409
    assert w.created == [] and w.claimed == []


async def test_only_whoever_holds_the_conversation(w):
    w.owner = False
    with pytest.raises(InboxError) as raised:
        await _send()
    assert raised.value.status == 409 and w.created == []


async def test_not_before_the_contract_data(w):
    w.no_details = True
    with pytest.raises(InboxError) as raised:
        await _send()
    assert raised.value.detail == content.text("contract_pdf.sem_dados")
    assert w.claimed == []


async def test_surely_refused_is_released(w):
    w.create_error = autentique.AutentiqueError("createDocument", 422)

    with pytest.raises(InboxError) as raised:
        await _send()

    assert raised.value.status == 502
    assert raised.value.detail == content.text("contract_sign.failed")
    assert w.released == [70] and w.marked == []


async def test_unconfirmed_stays_claimed_so_nothing_is_sent_twice(w):
    w.create_error = autentique.AutentiqueError("createDocument", None, uncertain=True)

    with pytest.raises(InboxError) as raised:
        await _send()

    assert raised.value.detail == content.text("contract_sign.uncertain")
    assert w.released == [] and w.marked == []


async def test_release_only_a_stale_send(w, monkeypatch):
    calls = []

    async def release_stale(conn, briefing_id, minutes):
        calls.append((briefing_id, minutes))
        return w.stale

    monkeypatch.setattr(signatures_repo, "release_stale", release_stale)
    w.stale = True
    assert (await contract_sign.release(_Pool(), PAULO, "hash", 7))["released"] is True
    w.stale = False
    with pytest.raises(InboxError) as raised:
        await contract_sign.release(_Pool(), PAULO, "hash", 7)
    assert raised.value.status == 409
    assert calls == [(7, contract_sign.STALE_MINUTES)] * 2


@pytest.mark.parametrize(("status", "age", "shown"), [
    ("sent", 0, "enviado"), ("sending", 0, "enviando"), ("sending", 5, "nao_confirmado"),
    ("signed", 5, "assinado")])
async def test_the_card_tells_a_send_under_way_from_an_unconfirmed_one(w, status, age, shown):
    from datetime import UTC, timedelta
    created = datetime.now(UTC) - timedelta(minutes=age)
    w.existing = signatures_repo.Sent(7, status, False, None, None, 1, None, created)

    found = await contract_sign.card(None, 7)
    assert found is not None and found["status"] == shown


async def test_window_closed_the_email_carries_it(w):
    w.window = False

    result = await _send()

    assert w.marked and w.sent == [] and w.recorded == []
    assert result["notice"] == content.text("contract_sign.sent_email")


async def test_link_failing_still_counts_as_sent(w):
    w.link_error = autentique.AutentiqueError("createLinkToSignature", 500)

    result = await _send()

    assert w.marked and w.sent == []
    assert result["notice"] == content.text("contract_sign.sent_email")


async def test_channel_refusing_is_not_recorded(w):
    w.delivery = "failed"

    result = await _send()

    assert w.recorded == []
    assert result["notice"] == content.text("contract_sign.sent_email")


@pytest.mark.parametrize(("key", "name", "email", "ready"), [
    ("k", "Paulo", "p@c.me", True), ("", "Paulo", "p@c.me", False),
    ("k", " ", "p@c.me", False), ("k", "Paulo", "", False)])
async def test_ready_needs_the_key_and_the_converge_signer(w, monkeypatch, key, name, email,
                                                          ready):
    monkeypatch.setattr(settings, "autentique_api_key", key)
    monkeypatch.setattr(settings, "contract_signer_name", name)
    monkeypatch.setattr(settings, "contract_signer_email", email)
    assert contract_sign.ready() is ready
    if not ready:
        with pytest.raises(InboxError) as raised:
            await _send()
        assert raised.value.detail == content.text("contract_sign.off")


async def test_too_slow_counts_as_unconfirmed(w, monkeypatch):
    monkeypatch.setattr(contract_sign, "_CREATE_SECONDS", 0.01)
    w.slow = True

    with pytest.raises(InboxError) as raised:
        await _send()

    assert raised.value.detail == content.text("contract_sign.uncertain")
    assert w.released == [] and w.marked == []
    assert contract_sign._CREATE_SECONDS < contract_sign.STALE_MINUTES * 60


async def test_created_after_being_freed_is_reported_not_written_over(w):
    w.kept = False

    with pytest.raises(InboxError) as raised:
        await _send()

    assert raised.value.status == 409
    assert raised.value.detail == content.text("contract_sign.duplicate")
    assert w.sent == [], "no link for a document the request no longer points to"


async def test_only_whoever_holds_the_conversation_frees_it(w, monkeypatch):
    called = []

    async def release_stale(conn, briefing_id, minutes):
        called.append(briefing_id)
        return True

    monkeypatch.setattr(signatures_repo, "release_stale", release_stale)
    w.owner = False

    with pytest.raises(InboxError):
        await contract_sign.release(_Pool(), PAULO, "hash", 7)
    assert called == []
