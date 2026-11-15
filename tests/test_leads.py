"""Leads added in the panel: one WhatsApp contact per number, its details and
origin, and the first-contact template only when there is one approved."""
# ruff: noqa: F811
from types import SimpleNamespace

import pytest

from core import content
from core.config import settings
from core.models import Contact
from core.repositories import contacts as contacts_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.services import inbox, leads
from tests.test_panel import accounts, client, clock, signed_in  # noqa: F401

PAULO = {"user_id": 1, "email": "paulo@converge.test", "name": "Paulo Nobre"}
CHAT = "wa:5591999990000"


async def _value(value):
    return value


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    w = SimpleNamespace(existing=None, details=[], origins=[], recorded=[], templates=[],
                        delivery="ok", meta_id=CHAT, looked_up=[], created=[], locked=[])
    for name in ("whatsapp_app_secret", "whatsapp_phone_number_id", "whatsapp_access_token",
                 "whatsapp_webhook_verify_token"):
        monkeypatch.setattr(settings, name, "x")
    monkeypatch.setattr(settings, "whatsapp_lead_template", "contato_por_email")
    monkeypatch.setattr(settings, "whatsapp_lead_general_template", "contato_interesse")

    async def get_or_create(conn, chat_id, name=None, inbound_at=None):
        w.created.append(chat_id)
        return Contact(id=9, chat_id=chat_id, name=name, channel="whatsapp")

    async def update_details(conn, contact_id, **fields):
        w.details.append(fields)

    async def set_origin(conn, contact_id, origin, detail, user_id):
        w.origins.append((origin, detail, user_id))
        return True

    async def record_message(conn, contact_id, chat_id, role, message, intent=None,
                             author_user_id=None):
        w.recorded.append((chat_id, message, intent, author_user_id))

    async def send_first_template(chat_id, name, params, payloads):
        w.templates.append((chat_id, name, params, payloads))
        return w.delivery, w.meta_id if w.delivery == "ok" else None

    async def find_by_chat_ids(conn, chat_ids):
        w.looked_up.append(chat_ids)
        return w.existing

    async def lock_chat(conn, chat_id):
        w.locked.append(chat_id)

    monkeypatch.setattr(contacts_repo, "find_by_chat_ids", find_by_chat_ids)
    monkeypatch.setattr(handoffs_repo, "lock_chat", lock_chat)
    monkeypatch.setattr(contacts_repo, "get_or_create", get_or_create)
    monkeypatch.setattr(contacts_repo, "update_details", update_details)
    monkeypatch.setattr(contacts_repo, "record_message", record_message)
    monkeypatch.setattr(inbox_repo, "set_origin", set_origin)
    monkeypatch.setattr(inbox, "send_first_template", send_first_template)
    return w


def _form(**overrides) -> leads.LeadForm:
    values: dict = dict(name="Ana Souza", phone="(91) 99999-0000", email="Ana@X.com")
    values.update(overrides)
    return leads.LeadForm(**values)


async def test_a_lead_from_e_mail_gets_the_first_contact_template(world):
    result = await leads.add(object(), PAULO, _form())

    assert result == {"contact_id": 9, "existing": False, "sent": True,
                      "notice": content.text("lead.sent")}
    assert world.details == [{"phone": "5591999990000", "email": "ana@x.com"}]
    assert world.origins == [("email", None, 1)]
    assert world.templates == [(CHAT, "contato_por_email", ["Ana"], ["lead:yes", "lead:no"])]
    assert world.recorded == [(CHAT, content.render("lead.first_contact_text", nome="Ana"),
                               "lead", 1)]


async def test_without_the_template_the_lead_is_saved_and_nothing_sent(world, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_lead_template", None)
    monkeypatch.setattr(settings, "whatsapp_lead_general_template", None)

    result = await leads.add(object(), PAULO, _form())

    assert result["sent"] is False and result["notice"] == content.text("lead.no_template")
    assert world.templates == [] and world.recorded == [] and world.origins


@pytest.mark.parametrize("origin", ["indicacao", "anuncio_instagram", "site", "outro", None])
async def test_any_other_origin_gets_the_general_template(world, origin):
    result = await leads.add(object(), PAULO, _form(origin=origin))

    assert result["sent"] is True
    assert world.templates == [(CHAT, "contato_interesse", ["Ana"], ["lead:yes", "lead:no"])]
    assert world.recorded == [(CHAT, content.render("lead.first_contact_general_text", nome="Ana"),
                               "lead", 1)]


async def test_an_e_mail_lead_falls_back_to_the_general_template(world, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_lead_template", None)

    await leads.add(object(), PAULO, _form())

    assert [name for _, name, _, _ in world.templates] == ["contato_interesse"]


async def test_the_e_mail_template_never_goes_to_another_origin(world, monkeypatch):
    """"Você nos deixou o seu contato por e-mail" would be false for them."""
    monkeypatch.setattr(settings, "whatsapp_lead_general_template", None)

    result = await leads.add(object(), PAULO, _form(origin="indicacao"))

    assert result["notice"] == content.text("lead.no_template") and world.templates == []


async def test_a_refused_template_is_not_recorded(world):
    world.delivery = "failed"

    result = await leads.add(object(), PAULO, _form())

    assert result["notice"] == content.text("lead.send_failed") and world.recorded == []


async def test_not_sending_only_saves(world):
    result = await leads.add(object(), PAULO, _form(send=False))

    assert result["notice"] == content.text("lead.added") and world.templates == []


async def test_both_forms_of_a_brazilian_mobile_are_looked_up(world):
    await leads.add(object(), PAULO, _form(phone="(91) 98000-0002"))

    assert world.looked_up == [["wa:5591980000002", "wa:559180000002"]]


async def test_both_forms_are_locked_before_looking_up(world):
    await leads.add(object(), PAULO, _form(phone="(91) 98000-0002"))

    assert world.locked == ["wa:559180000002", "wa:5591980000002"]


@pytest.mark.parametrize("typed, first", [
    ("+49 911 234567", "wa:49911234567"),
    ("(91) 8000-0002", "wa:559180000002"),
    ("55 91 98000-0002", "wa:5591980000002"),
    ("+55 (91) 98000-0002", "wa:5591980000002"),
])
async def test_the_country_code_is_kept_and_a_bare_number_is_brazilian(world, typed, first):
    await leads.add(object(), PAULO, _form(phone=typed))

    assert world.looked_up[0][0] == first


async def test_the_contact_is_kept_under_the_id_meta_answers_with(world):
    world.meta_id = "wa:559180000002"

    await leads.add(object(), PAULO, _form(phone="(91) 98000-0002"))

    assert world.created == ["wa:559180000002"]
    assert world.recorded[0][0] == "wa:559180000002"
    assert world.details[0]["phone"] == "5591980000002"


async def test_a_number_already_on_whatsapp_opens_that_conversation(world):
    world.existing = Contact(id=3, chat_id=CHAT, name="Ana", channel="whatsapp")

    result = await leads.add(object(), PAULO, _form())

    assert result["contact_id"] == 3 and result["existing"] is True
    assert world.details == [] and world.templates == []


@pytest.mark.parametrize("overrides", [{"phone": "123456789"}, {"email": "nao é email"},
                                       {"origin": "tv"}, {"name": "   "}])
async def test_bad_input_is_refused(world, overrides):
    with pytest.raises(inbox.InboxError) as refused:
        await leads.add(object(), PAULO, _form(**overrides))

    assert refused.value.status == 422 and world.templates == []


# ── The route ───────────────────────────────────────────────────────────────


def test_adding_a_lead_needs_a_session(client, monkeypatch):
    called = []
    monkeypatch.setattr(leads, "add", lambda *a: called.append(a))
    assert client.post("/painel/api/leads", json={"name": "Ana", "phone": "91999990000"}
                       ).status_code == 401
    assert called == []


def test_the_route_passes_the_signed_in_person_and_shows_refusals(signed_in, monkeypatch):
    seen = []

    async def add(conn, user, form):
        seen.append((user["user_id"], form.name, form.origin))
        if form.name == "Erro":
            raise inbox.InboxError(422, "Telefone inválido.")
        return {"contact_id": 9, "existing": False, "sent": True, "notice": "ok"}

    monkeypatch.setattr(leads, "add", add)
    ok = signed_in.post("/painel/api/leads", json={"name": "Ana", "phone": "91999990000"})
    bad = signed_in.post("/painel/api/leads", json={"name": "Erro", "phone": "91999990000"})

    assert ok.status_code == 200 and ok.json()["contact_id"] == 9
    assert bad.status_code == 422 and bad.json()["detail"] == "Telefone inválido."
    assert seen[0][1:] == ("Ana", "email")
