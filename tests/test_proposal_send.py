"""The proposal PDF from the panel: one message within the window, the template
past it, sent once, and the three buttons' answers."""
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from core import content
from core.config import settings
from core.integrations import telegram, whatsapp
from core.models import Contact, ProposalBriefing
from core.repositories import contacts as contacts_repo
from core.repositories import funnel as funnel_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.repositories import proposal_briefings as repo
from core.services import inbox, panel_uploads, proposal_send
from core.services.inbox import InboxError
from core.services.upload_types import Upload

PDF = Upload(b"%PDF-1.4 proposta", "document", "application/pdf", "proposta.pdf")
PAULO = {"user_id": 1, "name": "Paulo"}
NOW = datetime(2026, 11, 3, 12, 0)


class _Conn:
    async def execute(self, sql, params=None):
        return None


TX: list[str] = []  # each block's writes, then "commit" or "rollback"


class _Pool:
    """Like the real pool: a block commits when it ends, and rolls back when an
    error leaves it."""
    @asynccontextmanager
    async def connection(self):
        try:
            yield _Conn()
        except BaseException:
            TX.append("rollback")
            raise
        TX.append("commit")


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch):
    w = SimpleNamespace(
        status="sent", window=True, channel="whatsapp", buttons=[], templates=[], marked=[],
        recorded=[], delivery="ok", tg=[], unmarked=[], moved=[], live=True, record_fails=False,
        mark_wins=True, tg_delivery="ok", owner=True, touched=[], owner_checks=0,
        lose_owner_after=None,
        answers={"quem": "Izabel Ataide · Clínica Bem Saúde · Clínica · Sócia"})
    for name in ("whatsapp_app_secret", "whatsapp_phone_number_id", "whatsapp_access_token",
                 "whatsapp_webhook_verify_token"):
        monkeypatch.setattr(settings, name, "x")
    monkeypatch.setattr(settings, "whatsapp_proposal_template", "proposta_converge")

    async def get(conn, briefing_id):
        return ProposalBriefing(id=briefing_id, contact_id=9, status=w.status, answers=w.answers,
                                created_at=NOW, updated_at=NOW)

    async def get_by_id(conn, contact_id):
        chat = "wa:5591999990000" if w.channel == "whatsapp" else "42"
        return Contact(id=9, chat_id=chat, name="Izabel Ataide", channel=w.channel)

    async def window_open(conn, contact_id):
        return w.window

    async def upload_media(data, mime, filename):
        return "MEDIA1"

    async def send_document_buttons(to, media_id, filename, body, buttons):
        w.buttons.append((to, media_id, filename, body, [k for _, k in buttons]))
        return w.delivery

    async def send_document_template(to, name, language, media_id, filename, params, payloads):
        w.templates.append((name, media_id, params, payloads))
        return "ok"

    async def record_message(conn, contact_id, chat_id, role, message, intent=None,
                             author_user_id=None):
        if w.record_fails:
            raise RuntimeError("database gone")
        w.recorded.append((message, intent, author_user_id))

    async def mark_proposal_sent(conn, briefing_id, user_id):
        if not w.mark_wins:
            return False
        w.marked.append(briefing_id)
        return True

    async def unmark_proposal_sent(conn, briefing_id, status):
        w.unmarked.append((briefing_id, status))
        TX.append("unmark")

    async def move_stage(conn, contact_id, stage, from_stages):
        w.moved.append((contact_id, stage))

    async def lock_as_owner(conn, contact, user, *, past_window=False):
        assert past_window, "past the window the template goes"
        w.owner_checks += 1
        if not w.owner or (w.lose_owner_after is not None
                           and w.owner_checks > w.lose_owner_after):
            raise InboxError(409, "Ana assumiu esta conversa.")
        return 5

    async def touch(conn, session_id):
        w.touched.append(session_id)

    async def require_live_session(conn, user, token_hash):
        if not w.live:
            raise panel_uploads.UploadError(401, "Entre novamente.")

    async def tg_attachment(chat_id, data, kind, mime, filename, caption=""):
        w.tg.append(("doc", filename))
        return w.tg_delivery

    async def tg_message(chat_id, text, reply_markup=None, **kw):
        keyboard = (reply_markup or {}).get("inline_keyboard", [])
        w.tg.append(("text", [b["callback_data"] for row in keyboard for b in row]))
        return True

    monkeypatch.setattr(repo, "get", get)
    monkeypatch.setattr(contacts_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(contacts_repo, "record_message", record_message)
    monkeypatch.setattr(inbox_repo, "window_open", window_open)
    monkeypatch.setattr(whatsapp, "upload_media", upload_media)
    monkeypatch.setattr(whatsapp, "send_document_buttons", send_document_buttons)
    monkeypatch.setattr(whatsapp, "send_document_template", send_document_template)
    monkeypatch.setattr(repo, "mark_proposal_sent", mark_proposal_sent)
    monkeypatch.setattr(repo, "unmark_proposal_sent", unmark_proposal_sent)
    monkeypatch.setattr(funnel_repo, "move_stage", move_stage)
    monkeypatch.setattr(panel_uploads, "require_live_session", require_live_session)
    monkeypatch.setattr(telegram, "send_attachment", tg_attachment)
    monkeypatch.setattr(telegram, "send_message", tg_message)
    monkeypatch.setattr(inbox, "lock_as_owner", lock_as_owner)
    monkeypatch.setattr(handoffs_repo, "touch", touch)
    TX.clear()
    return w


TEXT = content.render("proposal_doc.text", nome="Izabel", empresa="Clínica Bem Saúde")


async def test_within_the_window_one_message_with_the_pdf_and_the_buttons(world):
    result = await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert result["sent"] is True
    assert world.buttons == [("5591999990000", "MEDIA1", "proposta.pdf", TEXT,
                              proposal_send.PAYLOADS)]
    assert world.templates == [] and world.marked == [3] and world.unmarked == []
    assert world.moved == [(9, "proposta")] and world.touched == [5]
    assert world.recorded == [(content.render("proposal_doc.history", arquivo="proposta.pdf",
                                              texto=TEXT), "proposal_doc", 1)]


async def test_past_the_window_the_template_with_the_pdf_as_header(world):
    world.window = False

    await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert world.buttons == []
    assert world.templates == [("proposta_converge", "MEDIA1", ["Izabel", "Clínica Bem Saúde"],
                                proposal_send.PAYLOADS)]
    assert world.marked == [3]


async def test_a_window_closed_by_meta_falls_back_to_the_template(world):
    world.delivery = "window_closed"

    await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert len(world.buttons) == 1 and len(world.templates) == 1 and world.marked == [3]


async def test_past_the_window_without_the_template_nothing_is_sent(world, monkeypatch):
    world.window = False
    monkeypatch.setattr(settings, "whatsapp_proposal_template", None)

    with pytest.raises(InboxError) as raised:
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert raised.value.detail == content.text("proposal_doc.no_template")
    assert world.templates == [] and world.marked == [] and world.recorded == []


async def test_a_refused_send_waits_again(world):
    world.delivery = "failed"

    with pytest.raises(InboxError):
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert world.marked == [3] and world.unmarked == [(3, "sent")]
    assert world.recorded == [] and world.moved == []
    # The undo is committed, not rolled back by the error that follows it.
    assert TX[TX.index("unmark") + 1] == "commit"


async def test_a_failed_write_after_sending_never_unmarks(world):
    """Sent, then the database failed: a second click must not send again."""
    world.record_fails = True

    with pytest.raises(RuntimeError):
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert len(world.buttons) == 1 and world.marked == [3] and world.unmarked == []


async def test_an_uncertain_send_stays_marked(world, monkeypatch):
    async def lost(*args, **kwargs):
        raise TimeoutError
    monkeypatch.setattr(whatsapp, "send_document_buttons", lost)

    with pytest.raises(InboxError) as raised:
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert raised.value.detail == content.text("proposal_doc.uncertain")
    assert world.marked == [3] and world.unmarked == []


async def test_a_session_revoked_during_the_upload_sends_nothing(world):
    world.live = False

    with pytest.raises(InboxError) as raised:
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert raised.value.status == 401
    assert world.buttons == [] and world.templates == [] and world.marked == []


async def test_a_proposal_already_sent_is_not_sent_again(world):
    world.status = "proposal_sent"

    with pytest.raises(InboxError) as raised:
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert raised.value.status == 409 and world.buttons == [] and world.templates == []


@pytest.mark.parametrize("upload", [
    Upload(b"%PDF-1.4", "document", "application/msword", "p.doc"),
    Upload(b"not a pdf", "document", "application/pdf", "p.pdf"),
])
async def test_only_a_pdf_goes(world, upload):
    with pytest.raises(InboxError) as raised:
        await proposal_send.send(_Pool(), PAULO, "tok", 3, upload)

    assert raised.value.status == 422 and world.buttons == []


async def test_without_a_company_the_text_still_reads(world):
    world.answers = {"quem": "Izabel"}

    await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert world.buttons[0][3] == content.render(
        "proposal_doc.text", nome="Izabel", empresa=content.text("proposal_doc.no_company"))


async def test_on_telegram_the_document_then_the_buttons(world):
    world.channel = "telegram"

    await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert world.tg == [("doc", "proposta.pdf"), ("text", proposal_send.PAYLOADS)]
    assert world.marked == [3]


def test_the_template_carries_the_document_header():
    payload = whatsapp._template("559100", "proposta_converge", "pt_BR", ["A", "B"],
                                 proposal_send.PAYLOADS)
    assert payload["template"]["components"][0]["type"] == "body"


async def test_send_document_template_puts_the_header_first(monkeypatch):
    sent = []

    async def post(payload):
        sent.append(payload)
        return "ok"
    monkeypatch.setattr(whatsapp, "_post_once", post)

    await whatsapp.send_document_template("559100", "proposta_converge", "pt_BR", "M1",
                                          "p.pdf", ["A", "B"], proposal_send.PAYLOADS)

    components = sent[0]["template"]["components"]
    assert components[0] == {"type": "header", "parameters": [
        {"type": "document", "document": {"id": "M1", "filename": "p.pdf"}}]}
    assert [c["type"] for c in components] == ["header", "body", "button", "button", "button"]


@pytest.mark.parametrize("data, key", [("propdoc:accept", "accept"),
                                       ("propdoc:decline", "decline")])
async def test_accepting_or_declining_tells_the_team(monkeypatch, data, key):
    told = []

    async def notify_team(text, contact_id):
        told.append(text)
        return True
    monkeypatch.setattr(inbox, "notify_team", notify_team)
    monkeypatch.setattr(inbox, "push_to_team", lambda text, contact: None)
    contact = Contact(id=9, chat_id="wa:1", name="Izabel Ataide", channel="whatsapp")

    chose, text = await proposal_send.decision(contact, data)

    assert chose == content.text(f"proposal_doc.button_{key}")
    assert text == content.text(f"proposal_doc.{key}_reply")
    assert told == [content.render(f"proposal_doc.team_{key}", nome="Izabel Ataide")]


async def test_marked_by_hand_meanwhile_sends_nothing(world):
    """"Marcar como enviada" won the race after the request was read."""
    world.mark_wins = False

    with pytest.raises(InboxError) as raised:
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert raised.value.status == 409 and world.buttons == [] and world.templates == []


async def test_a_lost_telegram_answer_stays_marked(world):
    world.channel = "telegram"
    world.tg_delivery = "uncertain"

    with pytest.raises(InboxError) as raised:
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert raised.value.detail == content.text("proposal_doc.uncertain")
    assert world.marked == [3] and world.unmarked == []


async def test_a_lost_whatsapp_answer_is_uncertain(world):
    world.delivery = "uncertain"

    with pytest.raises(InboxError) as raised:
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert raised.value.detail == content.text("proposal_doc.uncertain")
    assert world.marked == [3] and world.unmarked == [] and world.templates == []


async def test_a_transport_error_on_the_document_send_is_uncertain(monkeypatch):
    import httpx

    class Client:
        async def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("lost")
    monkeypatch.setattr(whatsapp, "_http", lambda: Client())

    assert await whatsapp.send_document_buttons("559100", "M1", "p.pdf", "t",
                                                [("A", "a")]) == "uncertain"
    assert await whatsapp.send_document_template("559100", "proposta_converge", "pt_BR",
                                                 "M1", "p.pdf", ["A", "B"],
                                                 ["a"]) == "uncertain"


async def test_only_whoever_holds_the_conversation_sends(world):
    world.owner = False

    with pytest.raises(InboxError) as raised:
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert raised.value.detail == "Ana assumiu esta conversa."
    assert world.buttons == [] and world.templates == [] and world.marked == []


async def test_a_server_error_on_the_document_send_is_uncertain(monkeypatch):
    import httpx

    class Client:
        async def post(self, *args, **kwargs):
            return httpx.Response(503, json={})
    monkeypatch.setattr(whatsapp, "_http", lambda: Client())

    assert await whatsapp.send_document_buttons("559100", "M1", "p.pdf", "t",
                                                [("A", "a")]) == "uncertain"


async def test_an_owner_who_let_go_during_the_upload_sends_nothing(world):
    """Checked again, and held, right before the send: let go meanwhile, and
    the request waits again, unsent."""
    world.lose_owner_after = 1

    with pytest.raises(InboxError) as raised:
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert raised.value.detail == "Ana assumiu esta conversa."
    assert world.buttons == [] and world.templates == []
    assert world.marked == [3] and world.unmarked == [(3, "sent")]
    assert TX[TX.index("unmark") + 1] == "commit"


async def test_a_session_revoked_during_the_upload_to_meta_sends_nothing(world, monkeypatch):
    checks = []

    async def require_live_session(conn, user, token_hash):
        checks.append(1)
        if len(checks) > 1:
            raise panel_uploads.UploadError(401, "Entre novamente.")
    monkeypatch.setattr(panel_uploads, "require_live_session", require_live_session)

    with pytest.raises(InboxError) as raised:
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert raised.value.status == 401 and world.buttons == [] and world.unmarked == [(3, "sent")]


async def test_a_slow_upload_to_meta_leaves_the_request_waiting(world, monkeypatch):
    """Nothing reached the customer: the request is not stuck as sent."""
    async def slow(data, mime, filename):
        raise TimeoutError
    monkeypatch.setattr(whatsapp, "upload_media", slow)

    with pytest.raises(InboxError) as raised:
        await proposal_send.send(_Pool(), PAULO, "tok", 3, PDF)

    assert raised.value.detail == content.text("proposal_doc.failed")
    assert world.buttons == [] and world.unmarked == [(3, "sent")]
