"""The Inbox: the team answering customers from /painel.

The team group only hears one-line notices, each with a button that opens the
conversation in the panel; the conversation itself is read and answered
there. One conversation, one owner: only the person who took it can send, and
while they hold it the bot stays quiet (core/services/handoff.py). Every
reply goes out through send(), the one place a second channel plugs into.
"""
from __future__ import annotations

import logging

from core import channels, content
from core.config import settings
from core.integrations import telegram, whatsapp
from core.integrations.whatsapp import Delivery
from core.models import Contact, HandoffSession
from core.repositories import automations as automations_repo
from core.repositories import contacts as contacts_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.repositories import offers as offers_repo
from core.repositories import proposal_briefings as proposal_repo
from core.services import push, test_marks, wa_templates

logger = logging.getLogger(__name__)

MAX_REPLY = 4000             # what the person may type
CHANNEL_LIMIT = 4096         # what Telegram takes, the name in front included
OWNER_PING_MINUTES = 5
CHANNEL_LABELS = {
    "telegram": "Telegram", "whatsapp": "WhatsApp", "instagram": "Instagram",
    "messenger": "Facebook", "email": "E-mail",
}
ORIGINS = ("anuncio_instagram", "anuncio_facebook", "site", "google", "indicacao", "email",
           "outro")
# What a t.me/<bot>?start=<origin> link may claim. Anything else is ignored.
_START_ORIGINS = ("site", "google", "indicacao", "anuncio_instagram", "anuncio_facebook")


class InboxError(Exception):
    """A refusal the panel shows as it is; `status` is the HTTP status."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def channel_label(channel: str | None) -> str:
    return CHANNEL_LABELS.get(channel or "", channel or "—")


def first_name(name: str | None) -> str:
    parts = (name or "").split()
    return parts[0] if parts else "Equipe"


def origin_from_start(body: str) -> str | None:
    """"/start site" → "site"."""
    parts = body.strip().split()
    if len(parts) != 2 or parts[0].split("@")[0].lower() != "/start":
        return None
    value = parts[1].lower()
    return value if value in _START_ORIGINS else None


def _team() -> str:
    return settings.team_chat_id.strip()


def open_keyboard(contact_id: int) -> dict | None:
    """The "Abrir no painel" button — only for an https address. Telegram
    refuses a button to anything else, and the whole message with it."""
    base = (settings.public_base_url or "").rstrip("/")
    if not base.startswith("https://") or "localhost" in base:
        return None
    return telegram.link_keyboard(
        content.text("inbox.open_button"), f"{base}/painel#inbox/{contact_id}"
    )


async def notify_team(text: str, contact_id: int | None) -> bool:
    """One line in the team group, with the button when there is an address.

    One attempt. A failure can be a timeout after Telegram already posted it,
    and a second try would post it twice; a notice lost now and then costs
    little, since the conversation is in the Inbox either way.
    """
    if not _team():
        return False
    keyboard = open_keyboard(contact_id) if contact_id is not None else None
    return await telegram.post_message(_team(), text, reply_markup=keyboard) is not None


async def send(channel: str, chat_id: str, text: str) -> Delivery:
    """A reply to the customer, on the channel they wrote from."""
    if channel in ("telegram", "whatsapp"):
        return await channels.deliver(chat_id, text, parse_mode=None)
    raise InboxError(501, "Canal ainda não conectado.")


TEMPLATE_LANGUAGE = "pt_BR"


def template_ready(name: str | None) -> bool:
    """An approved template named, and WhatsApp itself set up."""
    return bool(name) and settings.whatsapp_configured()


async def send_template(chat_id: str, name: str, params: list[str],
                        payloads: list[str] | None = None, *,
                        language: str = TEMPLATE_LANGUAGE) -> Delivery:
    """What the company starts past the 24h window: an approved template."""
    return await whatsapp.send_template(chat_id.removeprefix(channels.WA_PREFIX), name,
                                        language, params, payloads or [])


async def send_first_template(chat_id: str, name: str, params: list[str],
                              payloads: list[str]) -> tuple[Delivery, str | None]:
    """A template to someone who never wrote, and Meta's chat id for them."""
    delivery, wa_id = await whatsapp.send_first_template(
        chat_id.removeprefix(channels.WA_PREFIX), name, TEMPLATE_LANGUAGE, params, payloads)
    return delivery, (f"{channels.WA_PREFIX}{wa_id}" if wa_id else None)


async def _window_open(conn, contact: Contact) -> bool:
    """Only WhatsApp has the 24h rule."""
    if contact.channel != "whatsapp" or contact.id is None:
        return True
    return await inbox_repo.window_open(conn, contact.id)


def _push_target(contact: Contact) -> tuple[str, str]:
    """Where a notification about this contact opens, and its tag (a newer
    one about the same conversation replaces the older)."""
    where = f"inbox/{contact.id}" if contact.id is not None else "inbox"
    return f"/painel/#{where}", where.replace("/", "-")


def push_to_team(text: str, contact: Contact) -> None:
    """The same line, by push, to everyone on the team with notifications on.
    In the background; the Telegram group does not need to exist."""
    url, tag = _push_target(contact)
    push.schedule_team(title=text, url=url, tag=tag, chat_id=contact.chat_id)


async def notify_new_conversation(conn, contact: Contact) -> bool:
    """The first message ever, or the first in 24h: one line to the team."""
    if contact.id is None:
        return False
    if not await inbox_repo.claim_conversation_notice(conn, contact.id):
        return False
    return await send_new_conversation_notice(contact)


async def send_new_conversation_notice(contact: Contact) -> bool:
    """The line itself, for a caller that took the claim earlier (a media
    message takes it before its line is recorded, and decides later which one
    notice the event gets)."""
    text = test_marks.team_text(content.render(
        "inbox.new_conversation", nome=contact.name or "Sem nome",
        canal=channel_label(contact.channel),
    ), contact)
    push_to_team(text, contact)
    if not _team() or test_marks.quiet(contact):
        return False
    return await notify_team(text, contact.id)


async def _contact(conn, contact_id: int) -> Contact:
    contact = await contacts_repo.get_by_id(conn, contact_id)
    if contact is None or contact.id is None or not contact.chat_id:
        raise InboxError(404, "Conversa não encontrada.")
    return contact


def _state_of(session: HandoffSession | None) -> tuple[str, int | None]:
    """What the Inbox shows for a conversation: bot, waiting or claimed, and
    by whom (None for a claim made before the Inbox, in Telegram)."""
    if session is None:
        return "bot", None
    if session.status == "claimed":
        return "claimed", session.claimed_by_user_id
    return "waiting", None


async def claim(conn, contact_id: int, user: dict, *, seen_state: str,
                seen_owner: int | None) -> None:
    """Take an unclaimed conversation. An existing human owner is preserved.

    `seen_state` and `seen_owner` are what the screen showed when the person
    clicked. If the conversation changed since — someone else took it while
    the button still said "Assumir" — the click is refused rather than read
    as permission to take over."""
    contact = await _contact(conn, contact_id)
    assert contact.chat_id is not None
    _not_blocked(contact)
    # Waits for a bot turn in progress on this chat to finish, reply and all.
    await _lock_chat(conn, contact)
    session = await handoffs_repo.active_session(conn, contact.chat_id)
    if session is not None and session.claimed_by_user_id == user["user_id"]:
        return
    if _state_of(session) != (seen_state, seen_owner):
        raise InboxError(409, "Esta conversa mudou desde que você abriu. Veja com quem está agora.")
    if session is not None and session.status == "claimed":
        raise InboxError(409, "Esta conversa já está com outra pessoa.")
    if session is None:
        session = await handoffs_repo.open_session(
            conn, contact_id=contact.id, chat_id=contact.chat_id, team_chat_id=_team(),
            reason="painel",
        )
    if session.id is None:
        raise InboxError(409, "Não foi possível assumir agora. Tente de novo.")
    me = first_name(user["name"])
    if not await handoffs_repo.claim_by_user(
        conn, session.id, user["user_id"], me, session.claimed_by_user_id
    ):
        raise InboxError(409, "Esta conversa acabou de mudar de mãos. Atualize para ver com quem está.")
    nome = contact.name or "Sem nome"
    text = content.render("inbox.claimed", nome=nome, atendente=me)
    if not test_marks.quiet(contact):
        await notify_team(test_marks.team_text(text, contact), contact.id)


async def _lock_chat(conn, contact: Contact, *, blocked_ok: bool = False) -> None:
    """The chat's lock, taken before any row lock. A lead's first reply may
    have just moved them to the other form of the number
    (contacts._adopt_added_lead, under this same lock): read it again."""
    assert contact.chat_id is not None and contact.id is not None
    await handoffs_repo.lock_chat(conn, contact.chat_id)
    current = await contacts_repo.get_by_id(conn, contact.id)
    if current is None or current.chat_id != contact.chat_id:
        raise InboxError(409, "Esta conversa mudou desde que você abriu. Atualize a página.")
    if not blocked_ok:
        _not_blocked(current)  # read again under the lock: a block just saved counts


async def lock_as_owner(conn, contact: Contact, user: dict, *,
                        past_window: bool = False) -> int:
    """The session this person holds for this conversation, locked until the
    caller commits — or a refusal saying who holds it, or that the WhatsApp
    window is closed. Anything that writes to the customer from the panel
    goes through here first."""
    assert contact.chat_id is not None
    _not_blocked(contact)
    await _lock_chat(conn, contact)
    session_id = await handoffs_repo.lock_for_reply(conn, contact.chat_id, user["user_id"])
    if session_id is None:
        session = await handoffs_repo.active_session(conn, contact.chat_id)
        if session is not None and session.status == "claimed" and session.claimed_by_name:
            raise InboxError(409, f"{session.claimed_by_name} assumiu esta conversa.")
        raise InboxError(409, "Assuma a conversa para responder.")
    # past_window: the caller sends an approved template, which Meta takes
    # at any time.
    if not past_window and not await _window_open(conn, contact):
        raise InboxError(409, content.text("inbox.window_closed"))
    return session_id


async def reply(conn, contact_id: int, user: dict, text: str) -> None:
    """Send what the owner wrote.

    The conversation is locked as this person's before the send and stays
    locked until the caller commits, so nobody takes it in between. Recorded
    only once the channel took it: the history must never show a message the
    customer did not get.
    """
    outgoing = _relay(user, text)
    contact = await _contact(conn, contact_id)
    assert contact.chat_id is not None
    session_id = await lock_as_owner(conn, contact, user)
    delivery = await send(contact.channel, contact.chat_id, outgoing)
    if delivery == "window_closed":
        raise InboxError(409, content.text("inbox.window_closed"))
    if delivery != "ok":
        raise InboxError(502, content.render("inbox.send_failed", canal=channel_label(contact.channel)))
    await contacts_repo.record_message(
        conn, contact.id, contact.chat_id, "assistant", outgoing, "relay",
        author_user_id=user["user_id"],
    )
    await handoffs_repo.touch(conn, session_id)


def _relay(user: dict, text: str) -> str:
    """What the owner wrote, as the customer sees it; refused when too long."""
    body = text.strip()
    if not body:
        raise InboxError(422, "Escreva a mensagem.")
    if len(body) > MAX_REPLY:
        raise InboxError(422, f"A mensagem pode ter até {MAX_REPLY} caracteres.")
    outgoing = content.render("handoff.relay", atendente=first_name(user["name"]), mensagem=body)
    if len(outgoing) > CHANNEL_LIMIT:
        raise InboxError(422, "A mensagem ficou longa demais para o canal. Divida em duas.")
    return outgoing


async def reopen(conn, contact_id: int, user: dict, choice: str | None = None,
                 reply: str | None = None) -> None:
    """Past the 24h window, send an approved template the owner chose (the
    first one ready when none was named): the customer's tap or reply reopens
    the window and the owner can then write freely. Only the owner sends, like
    any other message to the customer. `reply`, when given, waits and goes out
    on the customer's first message after it (deliver_after_reopen)."""
    outgoing = _relay(user, reply) if reply and reply.strip() else None
    contact = await _contact(conn, contact_id)
    assert contact.chat_id is not None and contact.id is not None
    _not_blocked(contact)
    await _lock_chat(conn, contact)
    session_id = await handoffs_repo.lock_for_reply(conn, contact.chat_id, user["user_id"])
    if session_id is None:
        raise InboxError(409, "Assuma a conversa para responder.")
    if contact.channel != "whatsapp" or await _window_open(conn, contact):
        raise InboxError(409, "A conversa já está aberta para resposta.")
    offered = wa_templates.manual_ready()
    chose = choice is not None
    template = wa_templates.manual(choice) if chose else (offered[0] if offered else None)
    if template is None:
        raise InboxError(409, content.text(
            "inbox.reopen_unknown" if chose and offered else "inbox.window_closed"))
    name = first_name(contact.name) if contact.name else "tudo bem"
    delivery = await whatsapp.send_template(
        contact.chat_id.removeprefix(channels.WA_PREFIX), wa_templates.name(template) or "",
        wa_templates.language(template), [name], list(template.payloads))
    if delivery != "ok":
        raise InboxError(502, content.render("inbox.send_failed", canal=channel_label(contact.channel)))
    await contacts_repo.record_message(
        conn, contact.id, contact.chat_id, "assistant",
        content.render(template.history or "inbox.reopen_text", nome=name), "relay",
        author_user_id=user["user_id"])
    await handoffs_repo.set_reopen_pending(conn, session_id, outgoing)
    await handoffs_repo.touch(conn, session_id)


async def deliver_after_reopen(conn, contact: Contact, chat_id) -> None:
    """The customer wrote (or tapped) after a reopen: the reply the owner left
    goes now, or, without one, word that the owner is coming. Once per reopen."""
    pending = await handoffs_repo.take_reopen_pending(conn, chat_id)
    if pending is None or contact.id is None:
        return
    if pending.reply:
        text, intent, author = pending.reply, "relay", pending.owner_user_id
    else:
        owner = first_name(pending.owner_name) if pending.owner_name else "A equipe"
        text = content.render("inbox.reopen_wait", nome=first_name(contact.name)
                              if contact.name else "tudo bem", atendente=owner)
        intent, author = "handoff", None
    delivery = await send(contact.channel or "telegram", str(chat_id), text)
    if delivery != "ok":
        # Kept for the customer's next message; the owner was told they wrote.
        logger.warning("The reply left with a reopen did not go out (%s)", delivery)
        await handoffs_repo.set_reopen_pending(conn, pending.session_id, pending.reply)
        return
    await contacts_repo.record_message(conn, contact.id, str(chat_id), "assistant", text, intent,
                                       author_user_id=author)


async def release(conn, contact_id: int, user: dict) -> None:
    """Give the conversation back to the bot, and say so to both sides.

    Only its current owner can return it: giving back sends the customer a
    message, and only whoever holds the conversation sends to them."""
    contact = await _contact(conn, contact_id)
    assert contact.chat_id is not None
    await handoffs_repo.lock_chat(conn, contact.chat_id)
    session = await handoffs_repo.active_session(conn, contact.chat_id)
    if session is None or session.id is None:
        raise InboxError(409, "Esta conversa já está com o assistente.")
    if session.status != "claimed" or session.claimed_by_user_id != user["user_id"]:
        raise InboxError(409, "Somente quem assumiu a conversa pode devolvê-la ao bot.")
    if not await handoffs_repo.close_by(conn, session.id, user["user_id"]):
        raise InboxError(409, "Esta conversa acabou de mudar de mãos. Atualize para ver com quem está.")
    # Past WhatsApp's 24h window Meta would refuse the notice: the conversation
    # goes back to the bot without one, and the team still hears of it.
    if await _window_open(conn, contact):
        text = content.text("handoff.back_to_bot")
        delivery = await send(contact.channel, contact.chat_id, text)
        if delivery == "ok":
            await contacts_repo.record_message(
                conn, contact.id, contact.chat_id, "assistant", text, "handoff")
        elif delivery != "window_closed":
            # The panel route lets this error escape its connection context, so
            # close_by rolls back and the person keeps the conversation.
            raise InboxError(502, content.render(
                "inbox.send_failed", canal=channel_label(contact.channel)
            ))
        # "window_closed": Meta's clock says the 24h passed even if ours did
        # not — the same giving back as with the window closed, no notice.
    if not test_marks.quiet(contact):
        await notify_team(test_marks.team_text(content.render(
            "inbox.released", nome=contact.name or "Sem nome", atendente=first_name(user["name"])
        ), contact), contact.id)


def _not_blocked(contact: Contact) -> None:
    if contact.blocked_at is not None:
        raise InboxError(409, "Contato bloqueado. Desbloqueie para falar com ele.")


async def _wind_down(conn, contact: Contact) -> None:
    """What ending and blocking share: whoever held it lets go, nothing
    pending goes out (the questionnaire, offers and automations are dropped),
    and the conversation is archived until the person writes again."""
    assert contact.chat_id is not None and contact.id is not None
    session = await handoffs_repo.active_session(conn, contact.chat_id)
    if session is not None and session.id is not None:
        await handoffs_repo.close(conn, session.id)
    briefing = await proposal_repo.open_for(conn, contact.id)
    if briefing is not None:
        # Cancelled, not paused: an open one would later be reported to the
        # team as abandoned. Its answers stay on record for the next one.
        await proposal_repo.set_status(conn, briefing.id, "cancelled")
    await offers_repo.expire(conn, contact.chat_id)
    await automations_repo.discard_for_contact(conn, contact.id)
    await contacts_repo.set_ended(conn, contact.id)


async def end(conn, contact_id: int, user: dict) -> None:
    """End the conversation: no message to the customer. If they write again,
    it is a new conversation and the bot answers as usual. Someone else
    holding it has to let go first."""
    contact = await _contact(conn, contact_id)
    await _lock_chat(conn, contact, blocked_ok=True)
    assert contact.chat_id is not None
    session = await handoffs_repo.active_session(conn, contact.chat_id)
    if (session is not None and session.status == "claimed"
            and session.claimed_by_user_id != user["user_id"]):
        raise InboxError(409, "Somente quem assumiu a conversa pode encerrá-la.")
    await _wind_down(conn, contact)
    logger.info("Conversation %s ended in the panel", contact.id)


async def block(conn, contact_id: int, user: dict) -> None:
    """Block: the bot answers nothing and tells nobody from now on; what the
    person sends is kept under "Bloqueadas". Anyone in the panel can block,
    whoever holds the conversation: blocking is for abuse, and cannot wait."""
    contact = await _contact(conn, contact_id)
    await _lock_chat(conn, contact, blocked_ok=True)
    await _wind_down(conn, contact)
    assert contact.id is not None
    await contacts_repo.set_blocked(conn, contact.id, user["user_id"])
    logger.info("Contact %s blocked in the panel", contact.id)


async def unblock(conn, contact_id: int, user: dict) -> None:
    contact = await _contact(conn, contact_id)
    await _lock_chat(conn, contact, blocked_ok=True)
    assert contact.id is not None
    await contacts_repo.set_blocked(conn, contact.id, None)
    logger.info("Contact %s unblocked in the panel", contact.id)


async def ping_owner(conn, session: HandoffSession, contact: Contact) -> None:
    """Tell whoever holds the conversation that the customer wrote — never
    what they wrote, and at most once every OWNER_PING_MINUTES: in private on
    Telegram and by push to their devices (core/services/push.py)."""
    if session.id is None or session.claimed_by_user_id is None:
        return
    chat_id = await inbox_repo.owner_chat_id(conn, session.claimed_by_user_id)
    devices = await push.recipients(conn, session.claimed_by_user_id)
    if not chat_id and not devices:
        logger.info("Panel user %s has no Telegram chat nor push device; no private notice",
                    session.claimed_by_user_id)
        return
    if not await inbox_repo.claim_owner_ping(conn, session.id, OWNER_PING_MINUTES):
        return
    text = test_marks.team_text(
        content.render("inbox.owner_ping", nome=contact.name or "Sem nome"), contact)
    # The push first, in the background: Telegram failing never stops it.
    url, tag = _push_target(contact)
    push.schedule(devices, title=text, url=url, tag=tag)
    if not chat_id:
        return
    try:
        await telegram.post_message(
            chat_id, text,
            reply_markup=open_keyboard(contact.id) if contact.id is not None else None,
        )
    except Exception as exc:
        logger.warning("Private notice to panel user %s failed (%s)",
                       session.claimed_by_user_id, type(exc).__name__)
