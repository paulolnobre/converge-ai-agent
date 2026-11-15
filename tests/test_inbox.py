"""The Inbox: one owner per conversation, only the owner sends, nothing is
recorded that did not reach the customer, and the team group hears one line."""
import pytest

from core import content
from core.config import settings
from core.integrations import telegram
from core.models import Contact, HandoffSession
from core.repositories import contacts as contacts_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.services import handoff, inbox, push, test_marks

TEAM = "-1001234567890"
CONN = object()
PAULO = {"user_id": 1, "email": "paulo@converge.test", "name": "Paulo Nobre"}
VICTOR = {"user_id": 2, "email": "victor@converge.test", "name": "Victor Lobato"}


async def _value(value):
    return value


class World:
    """The repositories and Telegram, in memory."""

    def __init__(self):
        self.contact = Contact(id=7, chat_id="42", name="Ana Souza")
        self.session: dict | None = None
        self.sent: list[tuple[str, str]] = []        # (chat_id, text) to customers
        self.posted: list[tuple[str, str, dict | None]] = []  # (chat_id, text, markup)
        self.recorded: list[tuple] = []
        self.deliver = True
        self.ping_ok = True
        self.owner_chats = {1: "111", 2: "222"}
        self.push_subs: dict[int, list[dict]] = {}   # user_id -> subscriptions
        self.pushed: list[tuple[list[dict], str, str, str]] = []  # (subs, title, url, tag)
        self.team_pushed: list[tuple[str, str, str]] = []  # (title, url, tag) to everyone
        self.team_push_chats: list[str | None] = []
        self.events: list[str] = []

    def waiting(self):
        self.session = {"id": 5, "contact_id": 7, "chat_id": "42", "team_chat_id": TEAM,
                        "reason": "pedido", "status": "open", "claimed_by_id": None,
                        "claimed_by_name": None, "claimed_by_user_id": None, "relay_chat_id": TEAM}

    def held_by(self, user):
        self.waiting()
        assert self.session is not None
        self.session.update(status="claimed", claimed_by_user_id=user["user_id"],
                            claimed_by_name=inbox.first_name(user["name"]))


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    monkeypatch.setattr(settings, "team_chat_id", TEAM)

    async def get_by_id(conn, contact_id):
        return w.contact if contact_id == w.contact.id else None

    async def lock_chat(conn, chat_id):
        w.events.append("lock")

    monkeypatch.setattr(handoffs_repo, "lock_chat", lock_chat)

    async def active_session(conn, chat_id):
        w.events.append("read")
        if w.session and w.session["status"] in ("open", "claimed"):
            return HandoffSession(**w.session)
        return None

    async def open_session(conn, *, contact_id, chat_id, team_chat_id, reason):
        if w.session is None or w.session["status"] == "closed":
            w.waiting()
            assert w.session is not None
            w.session["reason"] = reason
        return HandoffSession(**w.session)

    async def claim_by_user(conn, session_id, user_id, name, expected_owner):
        s = w.session
        if not s or s["status"] == "closed" or s["claimed_by_user_id"] != expected_owner:
            return False
        s.update(status="claimed", claimed_by_user_id=user_id, claimed_by_name=name)
        return True

    async def lock_for_reply(conn, chat_id, user_id):
        s = w.session
        if s and s["status"] == "claimed" and s["claimed_by_user_id"] == user_id:
            return s["id"]
        return None

    async def close_by(conn, session_id, user_id):
        s = w.session
        if not s or s["status"] == "closed":
            return False
        if s["status"] == "claimed" and s["claimed_by_user_id"] != user_id:
            return False
        s["status"] = "closed"
        return True

    async def record_message(conn, contact_id, chat_id, role, message, intent=None,
                             author_user_id=None):
        w.recorded.append((role, message, intent, author_user_id))

    async def send_message(chat_id, text, parse_mode=None, reply_markup=None, **kwargs):
        if w.deliver:
            w.sent.append((str(chat_id), text))
        return w.deliver

    async def post_message(chat_id, text, reply_markup=None, **kwargs):
        w.posted.append((str(chat_id), text, reply_markup))
        return 1

    monkeypatch.setattr(contacts_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(contacts_repo, "record_message", record_message)
    monkeypatch.setattr(handoffs_repo, "active_session", active_session)
    monkeypatch.setattr(handoffs_repo, "open_session", open_session)
    monkeypatch.setattr(handoffs_repo, "claim_by_user", claim_by_user)
    monkeypatch.setattr(handoffs_repo, "lock_for_reply", lock_for_reply)
    monkeypatch.setattr(handoffs_repo, "close_by", close_by)
    monkeypatch.setattr(handoffs_repo, "touch", lambda conn, session_id: _value(None))
    monkeypatch.setattr(handoffs_repo, "set_reopen_pending", lambda conn, sid, reply: _value(None))
    monkeypatch.setattr(handoffs_repo, "take_reopen_pending", lambda conn, chat_id: _value(None))
    monkeypatch.setattr(inbox_repo, "claim_owner_ping", lambda conn, sid, minutes: _value(w.ping_ok))
    monkeypatch.setattr(inbox_repo, "owner_chat_id", lambda conn, uid: _value(w.owner_chats.get(uid)))
    monkeypatch.setattr(push, "recipients", lambda conn, uid: _value(w.push_subs.get(uid, [])))

    def schedule(subs, *, title, url, tag):
        w.pushed.append((subs, title, url, tag))

    monkeypatch.setattr(push, "schedule", schedule)

    def schedule_team(*, title, url, tag, chat_id=None):
        w.team_pushed.append((title, url, tag))
        w.team_push_chats.append(chat_id)

    monkeypatch.setattr(push, "schedule_team", schedule_team)
    monkeypatch.setattr(telegram, "send_message", send_message)
    monkeypatch.setattr(telegram, "post_message", post_message)
    return w


def _team_lines(world):
    return [text for chat, text, _ in world.posted if chat == TEAM]


# ── Taking a conversation ───────────────────────────────────────────────────

async def test_taking_a_conversation_nobody_asked_about_tells_the_group(world):
    await inbox.claim(CONN, 7, PAULO, seen_state="bot", seen_owner=None)

    assert world.session and world.session["status"] == "claimed"
    assert world.session["reason"] == "painel" and world.session["claimed_by_name"] == "Paulo"
    assert _team_lines(world) == [content.render("inbox.claimed", nome="Ana Souza", atendente="Paulo")]


async def test_taking_it_from_someone_else_is_refused_even_with_a_current_screen(world):
    world.held_by(VICTOR)

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.claim(CONN, 7, PAULO, seen_state="claimed", seen_owner=2)

    assert refused.value.status == 409
    assert world.session and world.session["claimed_by_user_id"] == 2
    assert world.posted == []


async def test_a_claim_on_a_lead_that_just_moved_to_the_other_form_is_refused(
    world, monkeypatch: pytest.MonkeyPatch
):
    async def lock_chat(conn, chat_id):
        # The first reply adopted the lead under Meta's form while we waited.
        world.contact = world.contact.model_copy(update={"chat_id": "wa:559180000002"})

    monkeypatch.setattr(handoffs_repo, "lock_chat", lock_chat)

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.claim(CONN, 7, PAULO, seen_state="bot", seen_owner=None)

    assert refused.value.status == 409 and world.session is None


async def test_taking_what_you_already_hold_changes_nothing(world):
    world.held_by(PAULO)

    await inbox.claim(CONN, 7, PAULO, seen_state="claimed", seen_owner=1)

    assert world.posted == []


@pytest.mark.parametrize(("seen_state", "seen_owner"), [("waiting", None), ("bot", None), ("claimed", 3)])
async def test_a_click_on_a_screen_that_is_out_of_date_takes_nothing(world, seen_state, seen_owner):
    """Paulo's screen said "Assumir" (or showed someone else); meanwhile
    Victor took it. The old click must not become "Assumir no lugar"."""
    world.held_by(VICTOR)

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.claim(CONN, 7, PAULO, seen_state=seen_state, seen_owner=seen_owner)

    assert refused.value.status == 409
    assert world.session and world.session["claimed_by_user_id"] == 2 and world.posted == []


async def test_taking_a_conversation_waits_for_the_bot_turn_in_progress(world):
    """The chat lock comes before reading who holds it: a bot turn holding
    that lock finishes (and its reply goes out) before the claim is decided."""
    await inbox.claim(CONN, 7, PAULO, seen_state="bot", seen_owner=None)

    assert world.events[0] == "lock", world.events


async def test_a_legacy_telegram_owner_is_also_preserved(world):
    """A legacy Telegram claim still belongs to its existing human owner."""
    world.held_by(VICTOR)
    assert world.session is not None
    world.session.update(claimed_by_user_id=None, claimed_by_id=777)

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.claim(CONN, 7, PAULO, seen_state="claimed", seen_owner=None)

    assert refused.value.status == 409
    assert world.session["claimed_by_user_id"] is None
    assert world.posted == []


async def test_a_claim_that_lost_the_race_is_refused(world, monkeypatch: pytest.MonkeyPatch):
    world.waiting()
    monkeypatch.setattr(handoffs_repo, "claim_by_user",
                        lambda conn, sid, uid, name, expected: _value(False))

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.claim(CONN, 7, PAULO, seen_state="waiting", seen_owner=None)

    assert refused.value.status == 409 and world.posted == []


async def test_an_unknown_conversation_is_404(world):
    with pytest.raises(inbox.InboxError) as refused:
        await inbox.claim(CONN, 99, PAULO, seen_state="bot", seen_owner=None)
    assert refused.value.status == 404


# ── Answering ───────────────────────────────────────────────────────────────

async def test_the_owner_answers_with_first_name_and_it_is_recorded_as_theirs(world):
    world.held_by(PAULO)

    await inbox.reply(CONN, 7, PAULO, "  Oi Ana, tudo bem?  ")

    assert world.sent == [("42", "Paulo:\nOi Ana, tudo bem?")]
    assert world.recorded == [("assistant", "Paulo:\nOi Ana, tudo bem?", "relay", 1)]


@pytest.mark.parametrize("holder", [None, "waiting", VICTOR])
async def test_nobody_but_the_owner_can_send(world, holder):
    if holder == "waiting":
        world.waiting()
    elif holder is not None:
        world.held_by(holder)

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reply(CONN, 7, PAULO, "Oi!")

    assert refused.value.status == 409
    assert world.sent == [] and world.recorded == []
    if holder == VICTOR:
        assert "Victor" in refused.value.detail


async def test_a_reply_goes_out_only_while_the_conversation_is_locked_as_yours(
    world, monkeypatch: pytest.MonkeyPatch
):
    """Victor took it between Paulo's screen and Paulo's send: the lock taken
    before sending says no, and nothing reaches Ana."""
    world.held_by(PAULO)
    monkeypatch.setattr(handoffs_repo, "lock_for_reply", lambda conn, chat_id, uid: _value(None))

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reply(CONN, 7, PAULO, "Oi!")

    assert refused.value.status == 409 and world.sent == [] and world.recorded == []


@pytest.mark.parametrize("text", ["", "   ", "x" * 4001])
async def test_an_empty_or_too_long_reply_is_refused(world, text):
    world.held_by(PAULO)
    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reply(CONN, 7, PAULO, text)
    assert refused.value.status == 422 and world.sent == []


async def test_the_signed_message_must_fit_the_channel(world):
    """4000 characters plus a long name before them would pass Telegram's
    4096: the limit is checked on what is actually sent."""
    long_name = {**PAULO, "name": "P" * 120}
    world.held_by(long_name)

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reply(CONN, 7, long_name, "x" * 4000)

    assert refused.value.status == 422 and world.sent == []


async def test_a_reply_the_channel_refused_is_not_recorded(world):
    world.held_by(PAULO)
    world.deliver = False

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reply(CONN, 7, PAULO, "Oi!")

    assert refused.value.status == 502
    assert refused.value.detail == content.render("inbox.send_failed", canal="Telegram")
    assert world.recorded == []


async def test_a_channel_without_integration_says_so(world):
    world.contact = world.contact.model_copy(update={"channel": "instagram"})
    world.held_by(PAULO)

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reply(CONN, 7, PAULO, "Oi!")

    assert refused.value.status == 501 and world.recorded == []


# ── Giving it back ──────────────────────────────────────────────────────────

async def test_giving_it_back_tells_the_customer_and_the_group(world):
    world.held_by(PAULO)

    await inbox.release(CONN, 7, PAULO)

    assert world.session and world.session["status"] == "closed"
    assert world.sent == [("42", content.text("handoff.back_to_bot"))]
    assert _team_lines(world) == [content.render("inbox.released", nome="Ana Souza", atendente="Paulo")]


async def test_giving_back_refused_by_channel_reports_error_without_team_notice(world):
    world.held_by(PAULO)
    world.deliver = False

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.release(CONN, 7, PAULO)

    assert refused.value.status == 502
    assert world.recorded == []
    assert world.posted == []


async def test_giving_back_what_the_bot_already_has_is_409(world):
    with pytest.raises(inbox.InboxError) as refused:
        await inbox.release(CONN, 7, PAULO)
    assert refused.value.status == 409


async def test_only_whoever_holds_it_can_give_it_back(world):
    """Giving back sends Ana a message, so only her current owner may do it."""
    world.held_by(VICTOR)

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.release(CONN, 7, PAULO)

    assert refused.value.status == 409
    assert world.session and world.session["status"] == "claimed"
    assert world.sent == [] and world.posted == []


async def test_an_unclaimed_request_must_be_assumed_before_returning_it(world):
    world.waiting()

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.release(CONN, 7, PAULO)

    assert refused.value.status == 409
    assert world.session and world.session["status"] == "open"
    assert world.sent == [] and world.posted == []


async def test_giving_back_waits_for_the_bot_turn_in_progress(world):
    """A turn that already read the conversation as held records the message
    for the person and does not answer; giving back in the middle of it would
    leave that message unanswered. The lock comes first."""
    world.held_by(PAULO)

    await inbox.release(CONN, 7, PAULO)

    assert world.events[0] == "lock", world.events


async def test_giving_back_loses_to_someone_who_just_took_it(world, monkeypatch: pytest.MonkeyPatch):
    world.held_by(PAULO)
    monkeypatch.setattr(handoffs_repo, "close_by", lambda conn, sid, uid: _value(False))

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.release(CONN, 7, PAULO)

    assert refused.value.status == 409 and world.sent == []


# ── Notices ─────────────────────────────────────────────────────────────────

async def test_the_notice_opens_the_conversation_in_the_panel(world, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example.com/")

    await inbox.notify_team("linha", 7)

    assert world.posted[0][2] == {"inline_keyboard": [[{
        "text": content.text("inbox.open_button"), "url": "https://bot.example.com/painel#inbox/7"}]]}


async def test_without_an_address_the_notice_goes_without_a_button(world):
    await inbox.notify_team("linha", 7)
    assert world.posted == [(TEAM, "linha", None)]


@pytest.mark.parametrize("base", ["http://localhost:8000", "http://bot.example.com", "localhost"])
async def test_an_address_telegram_would_refuse_gets_no_button(world, monkeypatch: pytest.MonkeyPatch, base):
    """Telegram refuses a button to a non-https address, and the whole notice
    with it. Posting once without the button, rather than retrying after a
    failure, is what keeps an ambiguous timeout from posting it twice."""
    monkeypatch.setattr(settings, "public_base_url", base)

    assert await inbox.notify_team("linha", 7) is True
    assert world.posted == [(TEAM, "linha", None)]


async def test_a_notice_telegram_did_not_take_is_posted_once(world, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example.com")
    calls = []

    async def down(chat_id, text, reply_markup=None, **kwargs):
        calls.append(text)
        return None

    monkeypatch.setattr(telegram, "post_message", down)

    assert await inbox.notify_team("linha", 7) is False
    assert calls == ["linha"], "no second attempt that could duplicate it"


def test_the_request_for_a_person_is_one_line():
    text = content.render("inbox.needs_person", nome="Ana", canal="Telegram",
                          motivo=content.text("inbox.reason_pedido"))
    assert "\n" not in text


async def test_without_a_team_group_nothing_is_posted(world, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "team_chat_id", "")
    assert await inbox.notify_team("linha", 7) is False and world.posted == []


async def test_the_owner_is_told_privately_without_the_words(world):
    world.held_by(VICTOR)
    session = HandoffSession(**world.session)  # type: ignore[arg-type]

    await inbox.ping_owner(CONN, session, world.contact)

    assert world.posted == [("222", content.render("inbox.owner_ping", nome="Ana Souza"), None)]


async def test_the_private_notice_waits_its_five_minutes(world):
    world.held_by(VICTOR)
    world.ping_ok = False
    await inbox.ping_owner(CONN, HandoffSession(**world.session), world.contact)  # type: ignore[arg-type]
    assert world.posted == []


async def test_someone_with_no_telegram_gets_no_private_notice(world):
    world.held_by(VICTOR)
    world.owner_chats = {}
    await inbox.ping_owner(CONN, HandoffSession(**world.session), world.contact)  # type: ignore[arg-type]
    assert world.posted == [] and world.pushed == []


DEVICE = {"id": 9, "endpoint": "https://fcm.googleapis.com/fcm/send/x", "p256dh": "k", "auth": "a"}


async def test_the_owner_also_gets_a_push_with_the_same_words(world):
    world.held_by(VICTOR)
    world.push_subs = {2: [DEVICE]}
    await inbox.ping_owner(CONN, HandoffSession(**world.session), world.contact)  # type: ignore[arg-type]
    words = content.render("inbox.owner_ping", nome="Ana Souza")
    assert world.pushed == [([DEVICE], words, "/painel/#inbox/7", "inbox-7")]
    assert world.posted == [("222", words, None)]


async def test_a_push_reaches_someone_with_no_telegram(world):
    world.held_by(VICTOR)
    world.owner_chats = {}
    world.push_subs = {2: [DEVICE]}
    await inbox.ping_owner(CONN, HandoffSession(**world.session), world.contact)  # type: ignore[arg-type]
    assert len(world.pushed) == 1 and world.posted == []


async def test_the_push_waits_the_same_five_minutes(world):
    world.held_by(VICTOR)
    world.push_subs = {2: [DEVICE]}
    world.ping_ok = False
    await inbox.ping_owner(CONN, HandoffSession(**world.session), world.contact)  # type: ignore[arg-type]
    assert world.pushed == [] and world.posted == []


async def test_a_failing_telegram_does_not_stop_the_push(world, monkeypatch: pytest.MonkeyPatch):
    world.held_by(VICTOR)
    world.push_subs = {2: [DEVICE]}

    async def broken(*args, **kwargs):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(telegram, "post_message", broken)
    await inbox.ping_owner(CONN, HandoffSession(**world.session), world.contact)  # type: ignore[arg-type]
    assert len(world.pushed) == 1


async def test_a_test_contact_is_marked_in_the_push_too(world):
    world.held_by(VICTOR)
    world.push_subs = {2: [DEVICE]}
    world.contact = world.contact.model_copy(update={"test_mode": True})
    await inbox.ping_owner(CONN, HandoffSession(**world.session), world.contact)  # type: ignore[arg-type]
    title = world.pushed[0][1]
    assert title == test_marks.team_text(content.render("inbox.owner_ping", nome="Ana Souza"),
                                         world.contact)
    assert title != content.render("inbox.owner_ping", nome="Ana Souza")


async def test_a_new_conversation_is_announced_with_its_channel(world, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(inbox_repo, "claim_conversation_notice", lambda conn, cid: _value(True))

    assert await inbox.notify_new_conversation(CONN, world.contact) is True

    assert _team_lines(world) == [content.render("inbox.new_conversation", nome="Ana Souza", canal="Telegram")]


async def test_a_conversation_already_announced_is_not_again(world, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(inbox_repo, "claim_conversation_notice", lambda conn, cid: _value(False))
    assert await inbox.notify_new_conversation(CONN, world.contact) is False
    assert world.posted == [] and world.team_pushed == []


async def test_a_new_conversation_reaches_the_whole_team_by_push(world, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(inbox_repo, "claim_conversation_notice", lambda conn, cid: _value(True))
    await inbox.notify_new_conversation(CONN, world.contact)
    words = content.render("inbox.new_conversation", nome="Ana Souza", canal="Telegram")
    assert world.team_pushed == [(words, "/painel/#inbox/7", "inbox-7")]
    assert world.team_push_chats == ["42"]  # so the team's own chat can be left out


async def test_the_new_conversation_push_does_not_need_the_team_group(world,
                                                                     monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "team_chat_id", "")
    monkeypatch.setattr(inbox_repo, "claim_conversation_notice", lambda conn, cid: _value(True))
    await inbox.notify_new_conversation(CONN, world.contact)
    assert len(world.team_pushed) == 1 and world.posted == []


async def test_a_request_for_a_person_reaches_the_whole_team_by_push(world):
    assert await handoff.start(CONN, world.contact, "42", "pedido") is True
    words = content.render("inbox.needs_person", nome="Ana Souza", canal="Telegram",
                           motivo=content.text("inbox.reason_pedido"))
    assert world.team_pushed == [(words, "/painel/#inbox/7", "inbox-7")]
    assert _team_lines(world) == [words]


async def test_a_file_that_opens_a_request_reaches_the_whole_team_by_push(world):
    assert await handoff.open_for_media(CONN, world.contact, "42", "image") == "opened"
    ((title, url, _),) = world.team_pushed
    assert title.startswith("🙋 Ana Souza") and url == "/painel/#inbox/7"


async def test_a_test_contact_is_marked_in_the_team_push(world, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(inbox_repo, "claim_conversation_notice", lambda conn, cid: _value(True))
    world.contact = world.contact.model_copy(update={"test_mode": True})
    await inbox.notify_new_conversation(CONN, world.contact)
    words = content.render("inbox.new_conversation", nome="Ana Souza", canal="Telegram")
    assert world.team_pushed[0][0] == test_marks.team_text(words, world.contact) != words


# ── Small readers ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(("body", "origin"), [
    ("/start site", "site"), ("/start google", "google"), ("/start INDICACAO", "indicacao"),
    ("/start anuncio_instagram", "anuncio_instagram"), ("/start@ai_converge_bot site", "site"),
    ("/start", None), ("/start hackeado", None), ("/start site extra", None), ("site", None),
])
def test_only_known_origins_are_read_from_a_start_link(body, origin):
    assert inbox.origin_from_start(body) == origin


def test_channel_labels_and_first_names():
    assert inbox.channel_label("messenger") == "Facebook"
    assert inbox.channel_label("telegram") == "Telegram"
    assert inbox.first_name("Paulo Nobre") == "Paulo"
    assert inbox.first_name("") == "Equipe"


# ── WhatsApp's 24-hour window ───────────────────────────────────────────────

WA_CHAT = "wa:5591999990000"


@pytest.fixture
def on_whatsapp(world, monkeypatch: pytest.MonkeyPatch):
    """Ana writing on WhatsApp: `world.window` says whether she wrote in the
    last 24h, `world.meta` what Meta answers to a send."""
    from core.integrations import whatsapp

    for name, value in {"whatsapp_app_secret": "s", "whatsapp_phone_number_id": "PN1",
                        "whatsapp_access_token": "t", "whatsapp_webhook_verify_token": "v"}.items():
        monkeypatch.setattr(settings, name, value)
    world.contact = world.contact.model_copy(update={"chat_id": WA_CHAT, "channel": "whatsapp"})
    world.window = True
    world.meta = "ok"

    async def send_text(to, text):
        if world.meta == "ok":
            world.sent.append((to, text))
        return world.meta

    async def window_open(conn, contact_id):
        return world.window

    monkeypatch.setattr(whatsapp, "send_text", send_text)
    monkeypatch.setattr(inbox_repo, "window_open", window_open)
    return world


async def test_a_whatsapp_reply_inside_the_window_goes_out(on_whatsapp):
    on_whatsapp.held_by(PAULO)

    await inbox.reply(CONN, 7, PAULO, "Oi!")

    assert on_whatsapp.sent == [("5591999990000", content.render(
        "handoff.relay", atendente="Paulo", mensagem="Oi!"))]


async def test_a_closed_whatsapp_window_refuses_the_reply(on_whatsapp):
    on_whatsapp.held_by(PAULO)
    on_whatsapp.window = False

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reply(CONN, 7, PAULO, "Oi!")

    assert refused.value.status == 409
    assert refused.value.detail == content.text("inbox.window_closed")
    assert on_whatsapp.sent == [] and on_whatsapp.recorded == []


async def test_meta_saying_window_closed_is_a_409(on_whatsapp):
    on_whatsapp.held_by(PAULO)
    on_whatsapp.meta = "window_closed"

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reply(CONN, 7, PAULO, "Oi!")

    assert refused.value.status == 409
    assert refused.value.detail == content.text("inbox.window_closed")
    assert on_whatsapp.recorded == []


async def test_a_failed_whatsapp_send_is_still_a_502(on_whatsapp):
    on_whatsapp.held_by(PAULO)
    on_whatsapp.meta = "failed"

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reply(CONN, 7, PAULO, "Oi!")

    assert refused.value.status == 502


async def test_giving_back_with_a_closed_window_sends_nothing_to_the_customer(on_whatsapp):
    on_whatsapp.held_by(PAULO)
    on_whatsapp.window = False

    await inbox.release(CONN, 7, PAULO)

    assert on_whatsapp.session and on_whatsapp.session["status"] == "closed"
    assert on_whatsapp.sent == [] and on_whatsapp.recorded == []
    assert _team_lines(on_whatsapp) == [
        content.render("inbox.released", nome="Ana Souza", atendente="Paulo")
    ]


async def test_telegram_has_no_window(world, monkeypatch: pytest.MonkeyPatch):
    async def never(conn, contact_id):
        raise AssertionError("Telegram has no 24h window")

    monkeypatch.setattr(inbox_repo, "window_open", never)
    world.held_by(PAULO)

    await inbox.reply(CONN, 7, PAULO, "Oi!")

    assert len(world.sent) == 1


async def test_giving_back_when_meta_says_the_window_closed(on_whatsapp):
    """Our count said open, Meta said closed (131047): it is still a giving
    back, just without the message to the customer."""
    on_whatsapp.held_by(PAULO)
    on_whatsapp.meta = "window_closed"

    await inbox.release(CONN, 7, PAULO)

    assert on_whatsapp.session and on_whatsapp.session["status"] == "closed"
    assert on_whatsapp.recorded == []
    assert _team_lines(on_whatsapp) == [
        content.render("inbox.released", nome="Ana Souza", atendente="Paulo")
    ]


def test_the_preview_is_cut_at_120():
    from core.repositories.inbox import preview

    assert preview("x" * 300) == "x" * 120
    assert preview("curto") == "curto" and preview(None) is None


async def test_reopen_sends_the_team_template_and_records_its_words(on_whatsapp, monkeypatch):
    from core.integrations import whatsapp

    sent: list = []

    async def send_template(to, name, language, params, payloads):
        sent.append((to, name, language, params, payloads))
        return "ok"

    monkeypatch.setattr(whatsapp, "send_template", send_template)
    monkeypatch.setattr(settings, "whatsapp_team_template", "resposta_da_equipe")
    on_whatsapp.held_by(PAULO)
    on_whatsapp.window = False

    await inbox.reopen(CONN, 7, PAULO)

    assert sent == [("5591999990000", "resposta_da_equipe", "pt_BR", ["Ana"], ["reopen:team"])]
    assert on_whatsapp.recorded[-1][1] == content.render("inbox.reopen_text", nome="Ana")


async def test_reopen_without_the_template_is_refused(on_whatsapp, monkeypatch):
    from core.services import wa_templates

    for t in wa_templates.CATALOG:
        monkeypatch.setattr(settings, t.setting, None)
    on_whatsapp.held_by(PAULO)
    on_whatsapp.window = False

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reopen(CONN, 7, PAULO)

    assert refused.value.status == 409 and on_whatsapp.recorded == []


async def test_reopen_sends_the_template_the_owner_chose(on_whatsapp, monkeypatch):
    """Past 24h the owner picks which approved template goes: each one keeps
    the buttons and the words of the flow it belongs to."""
    from core.integrations import whatsapp

    sent: list = []

    async def send_template(to, name, language, params, payloads):
        sent.append((to, name, language, params, payloads))
        return "ok"

    monkeypatch.setattr(whatsapp, "send_template", send_template)
    monkeypatch.setattr(settings, "whatsapp_team_template", "resposta_da_equipe")
    monkeypatch.setattr(settings, "whatsapp_resume_template", "retomada_de_conversa")
    monkeypatch.setattr(settings, "whatsapp_lead_general_template", "contato_interesse")
    on_whatsapp.held_by(PAULO)
    on_whatsapp.window = False

    await inbox.reopen(CONN, 7, PAULO, "resume")
    await inbox.reopen(CONN, 7, PAULO, "lead_general")

    assert sent == [
        ("5591999990000", "retomada_de_conversa", "pt_BR", ["Ana"],
         ["resume:yes:manual", "resume:no:manual"]),
        ("5591999990000", "contato_interesse", "pt_BR", ["Ana"], ["lead:yes", "lead:no"]),
    ]
    assert [line[1] for line in on_whatsapp.recorded[-2:]] == [
        content.render("automation.resume_whatsapp_text", nome="Ana"),
        content.render("lead.first_contact_general_text", nome="Ana"),
    ]


async def test_reopen_without_a_choice_uses_the_first_ready(on_whatsapp, monkeypatch):
    from core.integrations import whatsapp

    sent: list = []

    async def send_template(to, name, language, params, payloads):
        sent.append(name)
        return "ok"

    monkeypatch.setattr(whatsapp, "send_template", send_template)
    from core.services import wa_templates

    for t in wa_templates.CATALOG:
        monkeypatch.setattr(settings, t.setting, None)
    monkeypatch.setattr(settings, "whatsapp_lead_general_template", "contato_interesse")
    on_whatsapp.held_by(PAULO)
    on_whatsapp.window = False

    await inbox.reopen(CONN, 7, PAULO)

    assert sent == ["contato_interesse"]


@pytest.mark.parametrize("choice", ["charge", "proposal", "inventado", ""])
async def test_reopen_refuses_a_template_not_offered(on_whatsapp, monkeypatch, choice):
    monkeypatch.setattr(settings, "whatsapp_team_template", "resposta_da_equipe")
    monkeypatch.setattr(settings, "whatsapp_charge_template", "cobranca_enviada")
    monkeypatch.setattr(settings, "whatsapp_proposal_template", "proposta_converge")
    on_whatsapp.held_by(PAULO)
    on_whatsapp.window = False

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reopen(CONN, 7, PAULO, choice)

    assert refused.value.status == 409 and on_whatsapp.recorded == []


async def test_reopen_with_the_window_open_is_refused(on_whatsapp, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_team_template", "resposta_da_equipe")
    on_whatsapp.held_by(PAULO)

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reopen(CONN, 7, PAULO)

    assert refused.value.status == 409


# ── The reply left with a reopen, delivered on the customer's tap ───────────

@pytest.fixture
def reopening(on_whatsapp, monkeypatch):
    """A team template ready, and the reopen claim kept in memory."""
    from core.integrations import whatsapp

    on_whatsapp.pending = None

    async def send_template(to, name, language, params, payloads):
        return "ok"

    async def set_reopen_pending(conn, session_id, reply):
        on_whatsapp.pending = {"session_id": session_id, "reply": reply}

    async def take_reopen_pending(conn, chat_id):
        found, on_whatsapp.pending = on_whatsapp.pending, None
        if found is None or not on_whatsapp.session or on_whatsapp.session["status"] != "claimed":
            return None
        return handoffs_repo.ReopenPending(
            session_id=found["session_id"], owner_user_id=on_whatsapp.session["claimed_by_user_id"],
            owner_name=on_whatsapp.session["claimed_by_name"], reply=found["reply"])

    monkeypatch.setattr(whatsapp, "send_template", send_template)
    monkeypatch.setattr(handoffs_repo, "set_reopen_pending", set_reopen_pending)
    monkeypatch.setattr(handoffs_repo, "take_reopen_pending", take_reopen_pending)
    monkeypatch.setattr(settings, "whatsapp_team_template", "resposta_da_equipe")
    on_whatsapp.held_by(PAULO)
    on_whatsapp.window = False
    return on_whatsapp


async def test_the_reply_left_with_a_reopen_goes_on_the_tap(reopening):
    from core.services import handoff

    await inbox.reopen(CONN, 7, PAULO, "team", "Segue o valor do pacote.")
    assert reopening.sent == []          # nothing free-form past the window
    outgoing = content.render("handoff.relay", atendente="Paulo", mensagem="Segue o valor do pacote.")
    assert reopening.pending["reply"] == outgoing

    reopening.window = True
    await handoff.handle_customer_message(CONN, reopening.contact, WA_CHAT, "Ver resposta")

    assert reopening.sent == [("5591999990000", outgoing)]
    assert reopening.recorded[-2][:2] == ("user", "Ver resposta")
    assert reopening.recorded[-1] == ("assistant", outgoing, "relay", PAULO["user_id"])

    await handoff.handle_customer_message(CONN, reopening.contact, WA_CHAT, "Obrigado")
    assert len(reopening.sent) == 1      # taken once


async def test_without_a_reply_the_customer_hears_who_answers(reopening):
    from core.services import handoff

    await inbox.reopen(CONN, 7, PAULO, "team")
    assert reopening.pending["reply"] is None

    reopening.window = True
    await handoff.handle_customer_message(CONN, reopening.contact, WA_CHAT, "Ver resposta")

    notice = content.render("inbox.reopen_wait", nome="Ana", atendente="Paulo")
    assert reopening.sent == [("5591999990000", notice)]
    assert reopening.recorded[-1] == ("assistant", notice, "handoff", None)


async def test_a_reply_too_long_is_refused_before_the_template(reopening):
    with pytest.raises(inbox.InboxError) as refused:
        await inbox.reopen(CONN, 7, PAULO, "team", "x" * (inbox.MAX_REPLY + 1))

    assert refused.value.status == 422
    assert reopening.pending is None and reopening.recorded == []


async def test_a_reply_that_did_not_go_out_waits_for_the_next_message(reopening):
    from core.services import handoff

    await inbox.reopen(CONN, 7, PAULO, "team", "Segue o valor.")
    reopening.window = True
    reopening.meta = "failed"
    await handoff.handle_customer_message(CONN, reopening.contact, WA_CHAT, "Ver resposta")
    assert reopening.sent == [] and reopening.pending is not None

    reopening.meta = "ok"
    await handoff.handle_customer_message(CONN, reopening.contact, WA_CHAT, "Oi?")
    assert [text for _, text in reopening.sent] == [
        content.render("handoff.relay", atendente="Paulo", mensagem="Segue o valor.")]


async def test_a_photo_after_the_reopen_also_takes_the_reply(reopening):
    from core.services import handoff

    await inbox.reopen(CONN, 7, PAULO, "team", "Segue o valor.")
    reopening.window = True
    assert await handoff.held_media(CONN, reopening.contact, WA_CHAT, notify=True) is True

    assert [text for _, text in reopening.sent] == [
        content.render("handoff.relay", atendente="Paulo", mensagem="Segue o valor.")]


# ── Ending a conversation and blocking a contact ────────────────────────────

@pytest.fixture
def winding(world, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    from datetime import UTC, datetime

    from core.repositories import automations as automations_repo
    from core.repositories import offers as offers_repo
    from core.repositories import proposal_briefings as proposal_repo

    done: list[tuple] = []

    async def close(conn, session_id):
        assert world.session is not None
        world.session["status"] = "closed"
        done.append(("closed", session_id))
        return True

    async def set_blocked(conn, contact_id, user_id):
        done.append(("blocked", contact_id, user_id))
        world.contact = world.contact.model_copy(update={
            "blocked_at": datetime.now(UTC) if user_id else None})

    monkeypatch.setattr(handoffs_repo, "close", close)
    monkeypatch.setattr(proposal_repo, "open_for", lambda conn, contact_id: _value(None))
    monkeypatch.setattr(offers_repo, "expire",
                        lambda conn, chat_id: _value(done.append(("offers", chat_id))))
    monkeypatch.setattr(automations_repo, "discard_for_contact",
                        lambda conn, contact_id: _value(done.append(("automations", contact_id))))
    monkeypatch.setattr(contacts_repo, "set_ended",
                        lambda conn, contact_id: _value(done.append(("ended", contact_id))))
    monkeypatch.setattr(contacts_repo, "set_blocked", set_blocked)
    return done


async def test_ending_lets_go_drops_what_is_pending_and_says_nothing(world, winding):
    world.held_by(PAULO)

    await inbox.end(CONN, 7, PAULO)

    assert ("closed", 5) in winding and ("ended", 7) in winding
    assert ("offers", "42") in winding and ("automations", 7) in winding
    assert world.sent == [], "the customer is not told"


async def test_only_whoever_holds_it_can_end_it(world, winding):
    world.held_by(VICTOR)

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.end(CONN, 7, PAULO)

    assert refused.value.status == 409 and winding == []


async def test_anyone_can_block_even_while_someone_holds_it(world, winding):
    world.held_by(VICTOR)

    await inbox.block(CONN, 7, PAULO)

    assert ("closed", 5) in winding and ("blocked", 7, 1) in winding
    assert world.sent == [] and _team_lines(world) == []


async def test_a_blocked_contact_cannot_be_taken_or_answered(world, winding):
    await inbox.block(CONN, 7, PAULO)

    with pytest.raises(inbox.InboxError) as refused:
        await inbox.claim(CONN, 7, PAULO, seen_state="bot", seen_owner=None)
    assert refused.value.status == 409
    with pytest.raises(inbox.InboxError):
        await inbox.reply(CONN, 7, PAULO, "oi")

    await inbox.unblock(CONN, 7, PAULO)
    assert winding[-1] == ("blocked", 7, None)
