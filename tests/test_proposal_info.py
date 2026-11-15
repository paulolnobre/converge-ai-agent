"""Asking for more information to build the proposal, from the panel: one
message with two buttons (book a talk, or talk to the team) within the window,
the approved template past it, and what each button leads to."""
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from core import channels, content
from core.config import settings
from core.models import Contact, ProposalBriefing
from core.repositories import contacts as contacts_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.repositories import proposal_briefings as repo
from core.services import inbox, panel_uploads, proposal_info
from core.services.inbox import InboxError

PAULO = {"user_id": 1, "name": "Paulo"}
NOW = datetime(2026, 11, 9, 12, 0)
BUTTONS = ["propinfo:team:4", "propinfo:meet:4"]


class _Conn:
    locks: list = []

    async def execute(self, sql, params=None):
        if "advisory" in sql:
            self.locks.append(params)


class _Pool:
    @asynccontextmanager
    async def connection(self):
        yield _Conn()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    w = SimpleNamespace(status="sent", window=True, channel="whatsapp", sent=[], templates=[],
                        recorded=[], touched=[], delivery="ok", owner=True, live=True,
                        template="pedir_informacoes")
    for name in ("whatsapp_app_secret", "whatsapp_phone_number_id", "whatsapp_access_token",
                 "whatsapp_webhook_verify_token"):
        monkeypatch.setattr(settings, name, "x")
    monkeypatch.setattr(settings, "whatsapp_info_template", w.template)

    async def get(conn, briefing_id):
        return ProposalBriefing(id=briefing_id, contact_id=9, status=w.status, answers={},
                                created_at=NOW, updated_at=NOW)

    async def get_by_id(conn, contact_id):
        chat = "wa:5591999990000" if w.channel == "whatsapp" else "42"
        return Contact(id=9, chat_id=chat, name="Izabel Ataide", channel=w.channel)

    async def window_open(conn, contact_id):
        return w.window

    async def deliver(chat_id, text, keyboard=None, parse_mode="Markdown"):
        rows = (keyboard or {})["inline_keyboard"]
        w.sent.append((chat_id, text, [b["callback_data"] for row in rows for b in row]))
        return w.delivery

    async def send_template(chat_id, name, params, payloads=None, *, language="pt_BR"):
        w.templates.append((name, params, payloads))
        return "ok"

    async def record_message(conn, contact_id, chat_id, role, message, intent=None,
                             author_user_id=None):
        w.recorded.append((message, intent, author_user_id))

    async def lock_as_owner(conn, contact, user, *, past_window=False):
        assert past_window
        if not w.owner:
            raise InboxError(409, "Ana assumiu esta conversa.")
        return 5

    async def touch(conn, session_id):
        w.touched.append(session_id)

    async def require_live_session(conn, user, token_hash):
        if not w.live:
            raise panel_uploads.UploadError(401, "Entre novamente.")

    monkeypatch.setattr(repo, "get", get)
    monkeypatch.setattr(contacts_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(contacts_repo, "record_message", record_message)
    monkeypatch.setattr(inbox_repo, "window_open", window_open)
    monkeypatch.setattr(channels, "deliver", deliver)
    monkeypatch.setattr(inbox, "send_template", send_template)
    monkeypatch.setattr(inbox, "lock_as_owner", lock_as_owner)
    monkeypatch.setattr(handoffs_repo, "touch", touch)
    monkeypatch.setattr(panel_uploads, "require_live_session", require_live_session)
    return w


async def _ask():
    return await proposal_info.ask(_Pool(), PAULO, "hash", 4)


async def test_within_the_window_it_goes_as_one_message_with_two_buttons(world):
    result = await _ask()

    (chat, text, buttons), = world.sent
    assert chat == "wa:5591999990000" and buttons == BUTTONS
    assert text == content.render("proposal_info.text", nome="Izabel")
    assert "Izabel" in text and world.templates == []
    assert result == {"sent": True, "notice": content.text("proposal_info.sent")}
    assert world.recorded == [(content.render("proposal_info.history", texto=text),
                               "proposal_info", 1)]
    assert world.touched == [5]


async def test_past_the_window_the_approved_template_goes(world):
    world.window = False

    await _ask()

    assert world.sent == []
    assert world.templates == [("pedir_informacoes", ["Izabel"], BUTTONS)]
    assert world.recorded and world.recorded[0][1] == "proposal_info"


async def test_past_the_window_without_a_template_it_says_so(world, monkeypatch):
    world.window = False
    monkeypatch.setattr(settings, "whatsapp_info_template", None)

    with pytest.raises(InboxError) as refused:
        await _ask()

    assert refused.value.status == 409
    assert refused.value.detail == content.text("proposal_info.no_template")
    assert world.sent == [] and world.templates == [] and world.recorded == []


async def test_a_window_that_closed_meanwhile_falls_to_the_template(world):
    world.delivery = "window_closed"

    await _ask()

    assert len(world.templates) == 1


async def test_on_telegram_it_goes_with_the_same_buttons(world):
    world.channel = "telegram"
    world.window = False

    await _ask()

    assert world.sent[0][2] == BUTTONS and world.templates == []


@pytest.mark.parametrize("status", ["proposal_sent", "cancelled"])
async def test_a_request_not_waiting_on_a_proposal_gets_nothing(world, status):
    world.status = status

    with pytest.raises(InboxError) as refused:
        await _ask()

    assert refused.value.status == 409 and world.sent == []


async def test_only_who_holds_the_conversation_writes(world):
    world.owner = False

    with pytest.raises(InboxError) as refused:
        await _ask()

    assert "assumiu" in refused.value.detail and world.sent == []


async def test_a_revoked_session_sends_nothing(world):
    world.live = False

    with pytest.raises(panel_uploads.UploadError):
        await _ask()

    assert world.sent == []


async def test_a_refusal_by_the_channel_is_an_error_and_nothing_is_recorded(world):
    world.delivery = "failed"

    with pytest.raises(InboxError) as refused:
        await _ask()

    assert refused.value.status == 502
    assert refused.value.detail == content.text("proposal_info.failed")
    assert world.recorded == []


async def test_an_unknown_request_is_404(world, monkeypatch):
    async def nothing(conn, briefing_id):
        return None

    monkeypatch.setattr(repo, "get", nothing)

    with pytest.raises(InboxError) as refused:
        await _ask()

    assert refused.value.status == 404


async def test_it_takes_the_lock_that_sending_the_pdf_takes(world):
    _Conn.locks.clear()

    await _ask()

    assert _Conn.locks == [("proposal-doc:4",)]


async def test_a_lost_answer_is_never_a_reason_to_try_again(world, monkeypatch):
    async def lost(chat_id, text, keyboard=None, parse_mode="Markdown"):
        raise TimeoutError

    monkeypatch.setattr(channels, "deliver", lost)

    with pytest.raises(InboxError) as refused:
        await _ask()

    assert refused.value.detail == content.text("proposal_info.failed")
    assert world.recorded == []


async def test_sent_but_not_recorded_still_counts_as_sent(world, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(contacts_repo, "record_message", broken)

    assert (await _ask())["sent"] is True


@pytest.mark.parametrize("data, read", [
    ("propinfo:meet:4", ("meet", 4)), ("propinfo:team:12", ("team", 12)),
    ("propinfo:meet", None), ("propinfo:other:4", None), ("propinfo:meet:" + "9" * 30, None)])
def test_a_tap_names_its_request(data, read):
    assert proposal_info.tap(data) == read


def test_the_buttons_say_talk_here_then_book():
    rows = proposal_info._keyboard(4)["inline_keyboard"]
    assert [(b["text"], b["callback_data"]) for row in rows for b in row] == [
        ("Conversar por aqui", "propinfo:team:4"), ("Agendar conversa", "propinfo:meet:4")]
