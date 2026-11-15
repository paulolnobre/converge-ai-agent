"""A photo or file with no text is a request to the team, opened once and
announced once; a conversation someone holds just pings its owner."""
# ruff: noqa: F811
import pytest

from core import content
from core.integrations import telegram
from core.models import Contact
from core.repositories import handoffs as handoffs_repo
from core.services import handoff, inbox
from tests.test_inbox import CONN, PAULO, TEAM, _value, world  # noqa: F401


@pytest.fixture
def touched(monkeypatch):
    calls: list[int] = []

    async def touch(conn, session_id):
        calls.append(session_id)

    monkeypatch.setattr(handoffs_repo, "touch", touch)
    return calls


async def test_open_for_media_opens_once_and_notifies_once(world, touched):
    assert await handoff.open_for_media(CONN, world.contact, "42", "image") == "opened"
    assert world.session is not None and world.session["reason"] == "midia"
    (chat, text, _), = world.posted
    assert chat == TEAM and "enviou uma foto" in text
    assert await handoff.open_for_media(CONN, world.contact, "42", "image") == "existing"
    assert len(world.posted) == 1 and touched == [5]


async def test_a_failed_notice_keeps_the_request(world, touched, monkeypatch):
    async def down(chat_id, text, reply_markup=None, **kwargs):
        raise RuntimeError("Telegram is down")

    monkeypatch.setattr(telegram, "post_message", down)
    assert await handoff.open_for_media(CONN, world.contact, "42", "document") == "opened"
    assert world.session is not None and world.session["status"] == "open"


async def test_a_claimed_conversation_is_existing_without_notice(world, touched):
    world.held_by(PAULO)
    assert await handoff.open_for_media(CONN, world.contact, "42", "video") == "existing"
    assert world.posted == [] and touched == [5]


async def test_held_media_pings_the_owner_without_recording(world, touched, monkeypatch):
    pinged = []

    async def ping_owner(conn, session, contact):
        pinged.append(session.id)

    monkeypatch.setattr(inbox, "ping_owner", ping_owner)
    world.held_by(PAULO)
    assert await handoff.held_media(CONN, world.contact, "42", notify=True) is True
    assert pinged == [5] and touched == [5] and world.recorded == []
    assert await handoff.held_media(CONN, world.contact, "42", notify=False) is True
    assert pinged == [5]


async def test_held_media_is_false_when_nobody_holds_it(world, touched):
    assert await handoff.held_media(CONN, world.contact, "42", notify=True) is False
    world.waiting()
    assert await handoff.held_media(CONN, world.contact, "42", notify=True) is False


async def test_a_test_contacts_media_request_is_not_announced_in_the_group(world, touched):
    world.contact = Contact(id=7, chat_id="42", name="Ana", test_mode="novo")
    assert await handoff.open_for_media(CONN, world.contact, "42", "audio") == "opened"
    assert world.posted == []


async def test_the_new_conversation_notice_is_sent_on_its_own(world):
    assert await inbox.send_new_conversation_notice(world.contact) is True
    (chat, text, _), = world.posted
    assert chat == TEAM and text == content.render(
        "inbox.new_conversation", nome="Ana Souza", canal=inbox.channel_label("telegram"))


def test_media_texts_and_placeholders_validate():
    content.validate()
    assert content.render("inbox.reason_media", tipo=content.text("media.kind_image")) == (
        "enviou uma foto")
    for status in ("expired", "too_large", "failed", "no_space", "rate_limited", "late",
                   "missing"):
        assert content.text(f"media.{status}")
    assert content.text("media.received") == (
        "Recebi o que você enviou. A equipe vai dar uma olhada e te responde por aqui.")
    assert content.text("media.not_received") == (
        "Não consegui receber esse arquivo. Pode tentar enviar de novo? "
        "Se for grande, vale mandar em partes menores.")

