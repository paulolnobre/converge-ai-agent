"""A queued Autentique event applied: each party once, the document finished
once, and only then the team hears."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from core.repositories import contacts as contacts_repo
from core.repositories import contract_signatures as repo
from core.repositories import proposal_briefings as briefings_repo
from core.services import contract_charge, contract_signed, proposal_briefing

DOC = repo.Document(briefing_id=4, contact_id=9, status="sent",
                    customer_signature="sig-ana", converge_signature="sig-paulo")


class _Conn:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def w(monkeypatch):
    w = SimpleNamespace(doc=DOC, parties=[], signed=[], told=[], first=True, tell_error=None,
                        sending=False, charged=[])

    async def by_document(conn, document_id):
        return w.doc if document_id == "doc1" else None

    async def mark_party_signed(conn, briefing_id, party, at):
        w.parties.append((briefing_id, party, at))
        return w.first

    async def mark_signed(conn, briefing_id, signed_file, at):
        w.signed.append((briefing_id, signed_file, at))
        return w.first

    async def sending_any(conn):
        return w.sending

    async def get_by_id(conn, contact_id):
        return SimpleNamespace(id=contact_id)

    async def get(conn, briefing_id):
        return SimpleNamespace(id=briefing_id)

    async def tell_team(template, contact, briefing):
        if w.tell_error:
            raise w.tell_error
        w.told.append((template, contact.id, briefing.id))
        return True

    async def charge_signed(conn, briefing_id, contact_id):
        w.charged.append((briefing_id, contact_id))
        return "sent"

    monkeypatch.setattr(contract_charge, "charge_signed", charge_signed)
    monkeypatch.setattr(repo, "by_document", by_document)
    monkeypatch.setattr(repo, "mark_party_signed", mark_party_signed)
    monkeypatch.setattr(repo, "mark_signed", mark_signed)
    monkeypatch.setattr(repo, "sending_any", sending_any)
    monkeypatch.setattr(contacts_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(briefings_repo, "get", get)
    monkeypatch.setattr(proposal_briefing, "tell_team", tell_team)
    return w


AT = "2025-01-31T12:22:30.000000Z"
AT_TIME = datetime(2025, 1, 31, 12, 22, 30, tzinfo=UTC)


def _signed(signature):
    return {"id": "e", "type": "signature.accepted", "document": "doc1", "signature": signature,
            "at": AT}


FINISHED = {"id": "e", "type": "document.finished", "document": "doc1",
            "file": "https://painel.autentique.com.br/documentos/doc1/assinado.pdf", "at": AT}


@pytest.mark.parametrize(("signature", "party"), [("sig-ana", "customer"),
                                                  ("sig-paulo", "converge")])
async def test_each_party_is_marked_by_its_signature(w, signature, party):
    conn = _Conn()
    assert await contract_signed.handle_event(conn, _signed(signature)) == f"signed:{party}"
    assert w.parties == [(4, party, AT_TIME)] and conn.commits == 1 and w.told == []


async def test_a_repeated_signature_is_a_duplicate(w):
    w.first = False
    assert await contract_signed.handle_event(_Conn(), _signed("sig-ana")) == "duplicate"


async def test_a_signature_of_neither_party_changes_nothing(w):
    assert await contract_signed.handle_event(_Conn(), _signed("sig-other")) == "unknown"
    assert w.parties == []


async def test_a_document_this_does_not_know_changes_nothing(w):
    event = {**FINISHED, "document": "doc2"}
    assert await contract_signed.handle_event(_Conn(), event) == "unknown"
    assert w.signed == [] and w.told == []


async def test_the_finished_document_is_signed_and_the_team_hears(w):
    assert await contract_signed.handle_event(_Conn(), FINISHED) == "finished:sent"
    assert w.signed == [(4, FINISHED["file"], AT_TIME)]
    assert w.told == [("contract_sign.team_signed", 9, 4)]


async def test_the_finished_document_charges_the_setup(w):
    await contract_signed.handle_event(_Conn(), FINISHED)
    assert w.charged == [(4, 9)]


async def test_the_team_hears_once_and_a_halfway_charge_is_picked_up(w):
    w.first = False
    assert await contract_signed.handle_event(_Conn(), FINISHED) == "duplicate:sent"
    assert w.told == [] and w.charged == [(4, 9)]


async def test_a_failed_notice_keeps_the_contract_signed(w):
    w.tell_error = RuntimeError("telegram down")
    assert await contract_signed.handle_event(_Conn(), FINISHED) == "finished:sent"
    assert w.signed == [(4, FINISHED["file"], AT_TIME)]
    assert w.charged == [(4, 9)]


async def test_without_a_time_ours_is_used(w):
    await contract_signed.handle_event(_Conn(), {**_signed("sig-ana"), "at": None})
    assert w.parties == [(4, "customer", None)]


@pytest.mark.parametrize(("known", "sending", "waits"), [
    (True, True, False), (False, False, False), (False, True, True)])
async def test_only_an_unknown_document_during_a_send_waits(w, known, sending, waits):
    w.sending = sending
    assert await contract_signed.must_wait(None, "doc1" if known else "doc2") is waits
