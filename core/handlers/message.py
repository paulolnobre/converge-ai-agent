"""One Telegram message to the front desk, start to finish.

Unlike the site assistant, this bot talks to strangers: anyone who finds the
handle can write to it. So the limits come first, nothing is stored about a
person before they have said something, and every scheduling action goes through
the calendar rather than through anything this process remembers.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from core import channels, content
from core.config import settings
from core.integrations import cache, telegram, workspace
from core.integrations.workspace.models import TimeSlot
from core.repositories import appointments as appointments_repo
from core.repositories import automations as automations_repo
from core.repositories import contacts as contacts_repo
from core.repositories import handoffs as handoffs_repo
from core.repositories import inbox as inbox_repo
from core.repositories import offers as offers_repo
from core.repositories import proposal_briefings as proposal_repo
from core.security import RateLimiter
from core.services import appointment_taps as taps
from core.services import (
    attendance,
    audio,
    automations,
    booking_lists,
    contact_details,
    form_lead,
    forms,
    funnel,
    handoff,
    inbox,
    knowledge,
    media,
    proposal_contract,
    proposal_info,
    proposal_send,
    scheduling,
    secretarial,
    slot_choice,
    test_marks,
    test_mode,
)
from core.services import proposal_briefing as proposal
from core.services.graph import greet_in_conversation, route
from core.services.media import Captured, SameAttachment
from core.services.media_types import attachment_of

logger = logging.getLogger(__name__)

# Fallback for when Redis is down: per-replica instead of shared, which is the
# safe direction — a flood costs more replicas, never an open door.
_limiter = RateLimiter(settings.rate_limit_max, settings.rate_limit_window)

# Only the two the organization has no opinion about: a message this service
# could not decode, and a flood. Everything a patient reads about the clinic
# itself comes from content/messages.toml.
_CANNOT_READ = "Nao consegui ler essa mensagem. Pode reenviar?"
_TOO_MANY = "Muitas mensagens seguidas. Aguarde um instante."
async def _note_inbound(conn, chat_id, name: str | None, item: dict) -> None:
    """Every WhatsApp message moves the 24h window, whatever happens to it next
    — rate-limited, /id, unanswered. Telegram has no window to keep."""
    if channels.is_whatsapp(chat_id):
        await contacts_repo.get_or_create(conn, chat_id, name, inbound_at=channels.sent_at(item))


async def note_inbound(conn, update: dict) -> None:
    """When the customer wrote, kept apart from handling the update: the worker
    commits it on its own, so a turn that fails and rolls back does not take
    the reopened WhatsApp window with it."""
    item = (update.get("message") or update.get("edited_message")
            or update.get("callback_query") or {})
    chat = item.get("chat") or (item.get("message") or {}).get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is not None and chat.get("type") in (None, "private"):
        await _note_inbound(conn, chat_id, _display_name(item), item)


async def _record_expired(conn, chat_id, name: str | None, item: dict, body: str | None) -> str:
    """`body` None: a media message whose own line was already recorded."""
    contact = await contacts_repo.get_or_create(conn, chat_id, name, inbound_at=channels.sent_at(item))
    if contact.id is not None and body is not None:
        await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "expired")
    logger.warning("A WhatsApp message was processed more than 24h after it was sent; "
                   "no free-form answer is possible")
    return "expired"


async def _allowed(chat_id) -> tuple[bool, bool]:
    """(allowed, should_notify), from the shared limiter when Redis is up."""
    if cache.available():
        return await cache.rate_limit_hit(
            f"chat:{chat_id}", settings.rate_limit_max, settings.rate_limit_window
        )
    return _limiter.check(chat_id)


_NOT_CAPTURED = object()  # _text_of downloads the voice note itself, as before capture


async def _text_of(message: dict, *, skip_media: bool = False,
                   audio_bytes: object = _NOT_CAPTURED) -> str | None:
    """The message as text. A voice note becomes its transcript.

    `skip_media`: only the caption (an edit of a caption, the file already on
    record). `audio_bytes`: what the capture downloaded (bytes, or None when
    nothing came) — transcribed as is, never downloaded again."""
    if message.get("text"):
        return message["text"][: settings.max_message_chars]
    caption = (message.get("caption") or "")[: settings.max_message_chars] or None
    if skip_media:
        return caption

    voice = message.get("voice") or message.get("audio")
    if voice:
        captured = audio_bytes is not _NOT_CAPTURED
        # Without capture, exactly as before it: no transcript is no text.
        fallback = caption if captured else None
        if voice.get("duration", 0) > settings.max_voice_seconds:
            return fallback
        if captured:
            data = audio_bytes if isinstance(audio_bytes, bytes) else None
        else:
            data = await channels.download_voice(voice["file_id"])
        if not data:
            return fallback
        return await audio.transcribe(data, voice.get("mime_type")) or fallback

    return caption


async def _media_without_text(conn, chat_id, message: dict, captured: Captured,
                              noticed: bool) -> str:
    """A photo, file or audio with nothing to read (spec §4). One event, one
    notice: a request that opens is its own notice; otherwise the new
    conversation is announced, if this was its first message."""
    contact = await contacts_repo.get_or_create(
        conn, chat_id, _display_name(message), inbound_at=channels.sent_at(message))
    if contact.id is not None:
        await automations_repo.mark_replied(conn, contact.id)
    if captured.kind == "sticker":
        # A reaction, not a delivery: no request, and the bot stays quiet.
        if not await handoff.held_media(conn, contact, chat_id, notify=False) and noticed:
            await inbox.send_new_conversation_notice(contact)
        return "media"
    if await handoff.held_media(conn, contact, chat_id, notify=True):
        return "handoff"
    if captured.status == "stored":
        if await handoff.open_for_media(conn, contact, chat_id, captured.kind) == "opened":
            await _say(conn, contact, chat_id, content.text("media.received"))
        elif noticed:
            await inbox.send_new_conversation_notice(contact)
        return "media"
    if noticed:
        await inbox.send_new_conversation_notice(contact)
    await _say(conn, contact, chat_id, content.text("media.not_received"))
    return "media"


async def _say(conn, contact, chat_id, text: str) -> None:
    if await channels.send(chat_id, text) and contact.id is not None:
        await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, "media")


async def _held(conn, chat_id) -> bool:
    """A person holds this conversation (taken in the panel's Inbox), and the
    bot must not speak in it."""
    session = await handoffs_repo.active_session(conn, chat_id)
    return session is not None and session.status == "claimed"


def _tapped_label(callback: dict, data: str) -> str:
    """The words on the button that was tapped, read from the message it sits
    on — what the person holding the conversation needs to see."""
    keyboard = ((callback.get("message") or {}).get("reply_markup") or {}).get("inline_keyboard") or []
    for row in keyboard:
        for button in row:
            if button.get("callback_data") == data:
                return button.get("text") or data
    return "um botão"


def _display_name(message: dict) -> str | None:
    sender = message.get("from") or {}
    parts = [sender.get("first_name"), sender.get("last_name")]
    return " ".join(part for part in parts if part) or None


def _title(contact) -> str:
    title = f"Atendimento: {contact.name}" if contact.name else "Atendimento"
    return test_marks.event_title(title, contact)


def _period_keyboard(preferred_day: date | None, replaces_id: int | None = None) -> dict:
    """Manhã / Tarde / Sem preferência — a button per choice, code reads the tap.

    Whatever the booking already knew before the question was asked travels in
    the callback data, because nothing else remembers it between this message
    and the tap that answers it.
    """
    day_token = preferred_day.isoformat() if preferred_day else "none"
    replaces_token = str(replaces_id) if replaces_id else "none"

    def cb(period: str) -> str:
        return f"period:{period}:{day_token}:{replaces_token}"

    return telegram.inline_keyboard([[
        (content.text("scheduling.period_morning"), cb("morning")),
        (content.text("scheduling.period_afternoon"), cb("afternoon")),
        (content.text("scheduling.period_any"), cb("any")),
    ]])


def _slots_keyboard(offer_id: int, slots: list[TimeSlot]) -> dict:
    """One button per offered time. A tap sends back which offer and which
    index, so a stale button from a superseded offer is caught rather than
    silently booking the wrong slot."""
    return telegram.inline_keyboard([
        [(scheduling.short_when(slot), f"slot:{offer_id}:{i}")]
        for i, slot in enumerate(slots)
    ])


def _limit_reply(held: list) -> tuple[str, dict]:
    """At the limit: the appointment held (or the list of them) and a way to
    move one, or to keep things as they are."""
    keep = (content.text("booking.limit_keep"), "limit:keep")
    if len(held) == 1:
        text = content.render("booking.limit_one", quando=scheduling.when_appointment(held[0]))
        return text, telegram.inline_keyboard([
            [(content.text("booking.limit_reschedule"), taps.tap("move", held[0]))], [keep]])
    text = content.render("booking.limit_many", n=len(held), lista=_listing(held))
    rows = _choose_keyboard(held, "move")["inline_keyboard"]
    return text, {"inline_keyboard": [*rows, [{"text": keep[0], "callback_data": keep[1]}]]}


def _choose_day() -> bool:
    return settings.booking_mode == "choose_day"


def _today() -> date:
    return datetime.now(ZoneInfo(settings.office_timezone)).date()


def _only_period(period: str | None) -> str | None:
    """morning or afternoon; "any", None or anything else is no filter."""
    return period if period in ("morning", "afternoon") else None


def _wait_for_day(period: str | None, replaces_id: int | None) -> str:
    """pickday:<p>:<r>:<minute it began> — a wait older than an offer's
    lifetime is over: days later, "hoje não vou conseguir ir" is not an
    answer to the list of days."""
    code = {"morning": "m", "afternoon": "t"}.get(period or "", "-")
    return f"pickday:{code}:{replaces_id or '-'}:{_minute_now()}"


def _minute_now() -> int:
    return int(datetime.now(ZoneInfo(settings.office_timezone)).timestamp() // 60)


def _day_description(entry: booking_lists.DayEntry, period: str | None) -> str:
    if entry.only and period is None:
        return content.text(f"booking.days_only_{entry.only}")
    if entry.count == 1:
        return content.text("booking.days_one")
    return content.render("booking.days_count", n=entry.count)


def _with_prefix(prefix: str | None, text: str) -> str:
    return f"{prefix}\n\n{text}" if prefix else text


async def _offer_days(conn, contact, chat_id, *, period: str | None, replaces_id: int | None,
                      first: date | None = None, only_days: list[date] | None = None,
                      prefix: str | None = None) -> tuple[str, dict | None]:
    """The list of days with a free time (BOOKING_MODE=choose_day). Ends any
    list of times on the table, and waits for a typed day (pickday)."""
    period = _only_period(period)
    # Before anything that can fail: a list of times left on the table would
    # still book after a failed change of day.
    await offers_repo.expire(conn, chat_id)
    try:
        starts = await scheduling.available_starts(period=period)
    except workspace.WorkspaceError as exc:
        logger.error("Could not read the calendar for the days: %s", exc)
        return content.text("scheduling.could_not_book"), None
    zone = ZoneInfo(settings.office_timezone)
    if only_days:
        named = [s for s in starts if s.start.astimezone(zone).date() in only_days]
        free = sorted({s.start.astimezone(zone).date() for s in named})
        if len(free) == 1:
            return await _offer_day_times(conn, contact, chat_id, day=free[0], period=period,
                                          replaces_id=replaces_id)
        if free:
            starts = named
        else:
            only_days = None
    if first is not None and first < _today():
        first = None
    entries, following = booking_lists.days_page(starts, first=first, period=period)
    if not entries:
        return _with_prefix(prefix, content.text("scheduling.no_slots")), None
    if contact.id is not None:
        await contacts_repo.set_waiting(conn, contact.id, _wait_for_day(period, replaces_id))
    rows = [[telegram.Choice(
        booking_lists.day_title(entry.day),
        booking_lists.callback(booking_lists.Nav("day", entry.day, period, replaces_id, None)),
        _day_description(entry, period),
    )] for entry in entries]
    if following:
        rows.append([telegram.Choice(
            content.text("booking.days_next"),
            booking_lists.callback(booking_lists.Nav("days", following, period, replaces_id,
                                                     None)),
        )])
    text = content.text("booking.days_which" if only_days else "booking.days_prompt")
    return _with_prefix(prefix, text), telegram.menu(
        rows, list_button=content.text("booking.days_button"))


async def _offer_day_times(conn, contact, chat_id, *, day: date, period: str | None,
                           replaces_id: int | None, after: time | None = None,
                           typed: bool = False) -> tuple[str, dict | None]:
    """The free times of one day, as an offer: a tap or a typed time books
    through the path that already exists (slot:<offer>:<index>)."""
    period = _only_period(period)
    if not _today() <= day <= _today() + timedelta(days=settings.booking_horizon_days):
        return await _offer_days(conn, contact, chat_id, period=period, replaces_id=replaces_id)
    await offers_repo.expire(conn, chat_id)
    try:
        starts = await scheduling.available_starts(period=period)
    except workspace.WorkspaceError as exc:
        logger.error("Could not read the calendar for a day: %s", exc)
        return content.text("scheduling.could_not_book"), None
    page, next_after = booking_lists.times_page(starts, day, after=after, period=period)
    if not page:
        weekday, _, when = booking_lists.day_title(day).partition(", ")
        said = (content.render("scheduling.day_unavailable", dia_semana=weekday.lower(),
                               data=when) if typed else content.text("booking.day_full"))
        return await _offer_days(conn, contact, chat_id, period=period,
                                 replaces_id=replaces_id, prefix=said)
    offer = await scheduling.remember_offer(conn, contact=contact, chat_id=chat_id, slots=page,
                                            period=period, replaces_id=replaces_id)
    if contact.id is not None and (contact.pending_action or "").startswith("pickday:"):
        await contacts_repo.set_waiting(conn, contact.id, None)
    rows = [[telegram.Choice(booking_lists.time_title(slot), f"slot:{offer.id}:{index}")]
            for index, slot in enumerate(page)]
    if next_after:
        rows.append([telegram.Choice(
            content.text("booking.times_more"),
            booking_lists.callback(booking_lists.Nav("more", day, period, replaces_id,
                                                     next_after)),
        )])
    rows.append([telegram.Choice(
        content.text("booking.times_other_day"),
        booking_lists.callback(booking_lists.Nav("days", _today(), period, replaces_id, None)),
    )])
    weekday, _, when = booking_lists.day_title(day).partition(", ")
    text = content.render("booking.times_prompt", dia_semana=weekday.lower(), data=when)
    return text, telegram.menu(rows, list_button=content.text("booking.times_button"))


async def _handle_pickday(conn, contact, chat_id, body: str) -> tuple[str, dict | None] | None:
    """A typed answer while the list of days is open. Only what is clear is
    acted on; a mixed answer asks for a tap; anything else ends the wait
    (None) and the message is handled on its own terms."""
    _, code, replaces_token, began = ((contact.pending_action or "") + ":::").split(":")[:4]
    if not began.isdigit() or _minute_now() - int(began) > settings.offer_ttl_minutes:
        if contact.id is not None:
            await contacts_repo.set_waiting(conn, contact.id, None)
        return None
    period = {"m": "morning", "t": "afternoon"}.get(code)
    replaces_id = int(replaces_token) if replaces_token.isdigit() else None
    reply = slot_choice.read_day_reply(body)
    if reply.kind == "clear":
        chosen = period if reply.period is None else _only_period(reply.period)
        ahead = [day for day in reply.days if day >= _today()]
        if len(ahead) == 1:
            return await _offer_day_times(conn, contact, chat_id, day=ahead[0], period=chosen,
                                          replaces_id=replaces_id, typed=True)
        if ahead:
            return await _offer_days(conn, contact, chat_id, period=chosen,
                                     replaces_id=replaces_id, only_days=ahead)
        return await _offer_days(conn, contact, chat_id, period=chosen, replaces_id=replaces_id,
                                 prefix=content.text("scheduling.day_past"))
    if reply.kind == "mixed":
        return await _offer_days(conn, contact, chat_id, period=period, replaces_id=replaces_id,
                                 prefix=content.text("booking.days_tap"))
    if reply.kind == "period_only":
        return await _offer_days(conn, contact, chat_id, period=_only_period(reply.period),
                                 replaces_id=replaces_id)
    if contact.id is not None:
        await contacts_repo.set_waiting(conn, contact.id, None)
    return None


# The WhatsApp Flow that books (content/forms/agendamento.json): the free times
# go inside it, and its answer comes back as `flow:agendar:<offer id>`.
BOOKING_FLOW = "agendamento"
BOOKING_TOKEN = "agendar:"
# A Dropdown takes 200 options; a month of half hours is more than anyone reads.
_FLOW_SLOTS_MAX = 100
# A WhatsApp list shows ten rows: the fallback when the Flow is refused.
_FALLBACK_SLOTS = 10
_OFFER_ID_DIGITS = 18
_INDEX_DIGITS = 4
_SUBJECT_MAX = 80
_NAME_MAX = 100
_EMAIL_MAX = 254


def _booking_flow(chat_id):
    """The booking Flow, when this chat is on WhatsApp and the Flow is published."""
    if not channels.is_whatsapp(chat_id):
        return None
    try:
        form = forms.load(BOOKING_FLOW)
    except forms.FormError as exc:
        logger.error("The booking Flow is broken: %s", exc)
        return None
    return form if form is not None and form.flow_id else None


def _slot_title(slot: TimeSlot) -> str:
    """"Quinta, 13/11 às 9h": fits a Dropdown option (30) and a list row (24)."""
    day = slot.start.astimezone(ZoneInfo(settings.office_timezone)).date()
    return f"{booking_lists.day_title(day)} às {booking_lists.time_title(slot)}"


async def _offer_by_flow(conn, contact, chat_id, form, *, preferred_day=None,
                         preferred_period: str | None = None, replaces_id: int | None = None,
                         only_days: list[date] | None = None) -> tuple[str, dict | None]:
    """Every free time, in one Flow, as one offer: the answer books through the
    path a tap on a time takes. The days the person named come first, when
    they have room; otherwise every day."""
    period = _only_period(preferred_period)
    await offers_repo.expire(conn, chat_id)
    try:
        starts = await scheduling.available_starts(period=period)
    except workspace.WorkspaceError as exc:
        logger.error("Could not read the calendar for the Flow: %s", exc)
        return content.text("scheduling.could_not_book"), None
    zone = ZoneInfo(settings.office_timezone)
    asked = set(only_days or []) | ({preferred_day} if preferred_day else set())
    named = [s for s in starts if s.start.astimezone(zone).date() in asked]
    starts = (named or starts)[:_FLOW_SLOTS_MAX]
    if not starts:
        return content.text("scheduling.no_slots"), None
    offer = await scheduling.remember_offer(conn, contact=contact, chat_id=chat_id, slots=starts,
                                            period=period, replaces_id=replaces_id)
    known = {"nome": contact.name or "", "email": contact.email or "",
             "horarios": [{"id": str(i), "title": _slot_title(slot)}
                          for i, slot in enumerate(starts)]}
    keyboard = channels.flow_keyboard(
        flow_id=form.flow_id, token=f"{BOOKING_TOKEN}{offer.id}",
        cta=content.text("booking.flow_cta"), screen=form.screen,
        data={key: known.get(key, "") for key in form.prefill})
    rows = [[telegram.Choice(_slot_title(slot), f"slot:{offer.id}:{i}")]
            for i, slot in enumerate(starts[:_FALLBACK_SLOTS])]
    keyboard["_fallback"] = (content.text("booking.flow_fallback"),
                             telegram.menu(rows, list_button=content.text("booking.times_button")))
    return content.text("booking.flow_intro"), keyboard


def _whole_email(typed: object) -> str | None:
    """The e-mail typed in the Flow, when the whole field is one address."""
    if not isinstance(typed, str) or len(typed) > _EMAIL_MAX:
        return None
    found = contact_details.extract(typed).email
    return found if found and found == typed.strip().lower() else None


async def _book_from_flow(conn, contact, chat_id, token: str,
                          answers: dict) -> tuple[str, dict | None]:
    """The booking Flow, answered: the time picked out of the offer it was
    sent with. An answer for an offer that is no longer on the table, or a
    time that is not in it, books nothing."""
    offer = await offers_repo.pending_for_chat(conn, chat_id)
    index = answers.get("horario")
    plain = token.isascii() and token.isdigit() and len(token) <= _OFFER_ID_DIGITS
    valid = (offer is not None and plain and offer.id == int(token)
             and isinstance(index, str) and index.isascii() and index.isdigit()
             and len(index) <= _INDEX_DIGITS and int(index) < len(offer.slots))
    if not valid or offer is None or not isinstance(index, str):
        return content.text("scheduling.slot_taken"), None
    email = _whole_email(answers.get("email"))
    if email is None and not contact.email:
        # Nowhere to send the confirmation: the Flow again, the times still free.
        text, keyboard = await _offer(conn, contact, chat_id, preferred_period=offer.period,
                                      replaces_id=offer.replaces_id)
        return f"{content.text('booking.flow_bad_email')}\n\n{text}", keyboard
    changes: dict[str, str] = {}
    if email and email != contact.email:
        changes["email"] = email
    name = answers.get("nome")
    name = " ".join(name.split())[:_NAME_MAX] if isinstance(name, str) else ""
    if name and name != contact.name:
        changes["name"] = name
    if changes and contact.id is not None:
        await contacts_repo.update_details(conn, contact.id, **changes)
    contact = contact.model_copy(update=changes)
    subject = answers.get("assunto")
    subject = " ".join(subject.split())[:_SUBJECT_MAX] if isinstance(subject, str) else ""
    return await _book_picked_slot(conn, offer, contact, chat_id, offer.slots[int(index)],
                                   subject=subject)


async def _offer(conn, contact, chat_id, *, preferred_day=None,
                 preferred_period: str | None = None,
                 replaces_id: int | None = None,
                 only_days: list[date] | None = None) -> tuple[str, dict | None]:
    """Put times in front of someone and remember which ones.

    The times are offered, not taken: the person picks one in the next turn.
    Booking on a first mention is how a bot fills someone's diary with
    appointments nobody confirmed.

    With BOOKING_MODE=choose_day: the list of days (or, for a named day, its
    times) instead of three spread-out times. A new booking (not a reschedule)
    at the contact's limit offers to move what they hold instead.
    """
    if replaces_id is None:
        held = await scheduling.limit_reached(conn, contact)
        if held:
            return _limit_reply(held)
    form = _booking_flow(chat_id)
    if form is not None:
        return await _offer_by_flow(conn, contact, chat_id, form, preferred_day=preferred_day,
                                    preferred_period=preferred_period, replaces_id=replaces_id,
                                    only_days=only_days)
    if _choose_day():
        if preferred_day:
            return await _offer_day_times(conn, contact, chat_id, day=preferred_day,
                                          period=preferred_period, replaces_id=replaces_id,
                                          typed=True)
        return await _offer_days(conn, contact, chat_id, period=preferred_period,
                                 replaces_id=replaces_id, only_days=only_days)
    slots, offer = await scheduling.offer_and_remember(
        conn, contact=contact, chat_id=chat_id,
        preferred_day=preferred_day, preferred_period=preferred_period,
        replaces_id=replaces_id,
    )
    if not slots or offer is None or offer.id is None:
        return content.text("scheduling.no_slots"), None
    text = content.render("scheduling.offer", slots=scheduling.phrase(slots))
    zone = ZoneInfo(settings.office_timezone)
    if preferred_day and slots[0].start.astimezone(zone).date() != preferred_day:
        # The day asked for is full (or closed): say so, rather than offering
        # other days as if they were the answer.
        noon = datetime.combine(preferred_day, time(12), tzinfo=zone)
        asked = scheduling.when_parts(TimeSlot(start=noon, end=noon + timedelta(minutes=30)))
        text = (f"{content.render('scheduling.day_unavailable', dia_semana=asked['dia_semana'], data=asked['data'])} "
                f"{text}")
    return text, _slots_keyboard(offer.id, slots)


async def _handle_day_reply(conn, contact, chat_id, body: str
                            ) -> tuple[str, dict | None] | None:
    """The answer to "Qual dia fica melhor para você?".

    The day is read in code. Not a day: ask once more with an example, then
    let the message be handled on its own terms. None means the wait is over
    and nothing was said here.
    """
    kind, _, replaces_token = (contact.pending_action or "").partition(":")
    replaces_id = int(replaces_token) if replaces_token.isdigit() else None
    day = slot_choice.requested_day(body)
    if day is None:
        if kind == "day":
            await contacts_repo.set_waiting(conn, contact.id, f"day2:{replaces_token}")
            return content.text("scheduling.which_day_again"), None
        await contacts_repo.set_waiting(conn, contact.id, None)
        return None
    if day < datetime.now(ZoneInfo(settings.office_timezone)).date():
        return content.text("scheduling.day_past"), None
    await contacts_repo.set_waiting(conn, contact.id, None)
    return content.text("scheduling.period_question"), _period_keyboard(day, replaces_id)


def _intake_needed(contact) -> bool:
    """Details are asked for once, before a booking or a handoff — never again
    for someone who was asked and would rather not give them."""
    return bool(contact_details.missing(contact.email, contact.phone)) and (
        contact.details_requested_at is None
    )


async def _ask_details(conn, contact, action: str) -> str:
    """Ask for what is missing and remember what the person wanted, so their
    next message can pick up from there instead of asking again."""
    missing = contact_details.missing(contact.email, contact.phone)
    need = "contact.need_both" if len(missing) > 1 else "contact.need_" + "".join(missing)
    await contacts_repo.set_pending_action(conn, contact.id, action)
    key = ("contact.ask_before_booking" if action.startswith("booking")
           else "contact.ask_before_proposal" if action.startswith("proposal")
           else "contact.ask_before_handoff")
    return content.render(key, faltando=content.text(need))


# Asked for in the middle of the questionnaire, these pause it.
_PAUSES_PROPOSAL = frozenset({"booking", "reschedule", "cancel", "escalation"})

_PENDING_ACTION_MAX = 255  # contacts.pending_action, migration 0010


def _booking_action(triage, only_days: list[date] | None = None) -> str:
    """booking:<day>:<period>[:<d1>,<d2>,...] — what to resume once the
    details arrive, with every day the person named. contacts.pending_action
    is 255 characters: twenty-one days, more than anyone names."""
    day = triage.preferred_day.isoformat() if triage.preferred_day else "none"
    action = f"booking:{day}:{triage.preferred_period or 'none'}"
    if only_days:
        fits = (_PENDING_ACTION_MAX - len(action)) // 11
        action += ":" + ",".join(d.isoformat() for d in only_days[:fits])
    return action


async def _resume(conn, contact, chat_id, action: str, body: str
                  ) -> tuple[str, dict | None, str]:
    """Carry on with what was asked for before the details were requested:
    the times (or the period question first), or the handoff to the team."""
    kind, _, rest = action.partition(":")
    if kind == "booking":
        day_token, _, after_day = rest.partition(":")
        period, _, days_token = after_day.partition(":")
        preferred_day = date.fromisoformat(day_token) if day_token not in ("", "none") else None
        only_days = None
        try:
            only_days = [date.fromisoformat(d) for d in days_token.split(",") if d] or None
        except ValueError:
            only_days = None
        if period in ("morning", "afternoon", "any") or _choose_day():
            text, keyboard = await _offer(
                conn, contact, chat_id, preferred_day=preferred_day, preferred_period=period,
                only_days=only_days,
            )
        else:
            text = content.text("scheduling.period_question")
            keyboard = _period_keyboard(preferred_day)
        return text, keyboard, "booking"
    if kind == "proposal":
        text, keyboard = await _send_proposal(conn, contact)
        return text, keyboard, "proposal"
    told = await handoff.start(conn, contact, chat_id, rest or "pedido")
    return content.text("reply.escalation" if told else "reply.escalation_failed"), None, "escalation"


async def _history_text(conn, chat_id) -> str:
    history = await contacts_repo.recent_history(conn, chat_id)
    return "\n".join(f"{role}: {text}" for role, text in history)


async def _start_booking(conn, contact, chat_id) -> tuple[str, dict | None]:
    """The times, from scratch: "Remarcar", "Conversar 30 min"."""
    if _choose_day() or _booking_flow(chat_id) is not None:
        return await _offer(conn, contact, chat_id)
    return content.text("scheduling.period_question"), _period_keyboard(None)


def _waiting_for_email(b) -> tuple[str, dict]:
    """No e-mail, so nothing goes to the team: the answers wait, and the
    confirmation buttons stay at hand for when the e-mail comes."""
    _, keyboard = proposal.confirmation(b)
    return content.text("proposal.waiting_email"), keyboard


async def _proposal_waiting(conn, contact) -> tuple[str, dict | None] | None:
    b = await proposal_repo.open_for(conn, contact.id) if contact.id is not None else None
    return _waiting_for_email(b) if b is not None else None


async def _send_proposal(conn, contact) -> tuple[str, dict | None]:
    """The questionnaire confirmed: to the team, if there is an e-mail to send
    the proposal to. The phone is never required."""
    b = await proposal_repo.open_for(conn, contact.id) if contact.id is not None else None
    if b is None:
        # Sent or ended meanwhile (a second tap): nothing left to send.
        return content.text("contact.thanks"), None
    step = b.step or proposal.next_step(b.answers)
    if step is not None:
        return proposal.question(step, b.answers)
    if contact.email:
        return await proposal.submit(conn, contact, b), None
    return _waiting_for_email(b)


async def _proposal_ok(conn, contact, b) -> tuple[str | None, dict | None]:
    """The "Está certo" tap: sent, or the e-mail asked for first — the whole
    intake once, then the e-mail alone ("proposal:email", the last ask)."""
    if contact.email or (b.step or proposal.next_step(b.answers)) is not None:
        return await _send_proposal(conn, contact)
    if contact.details_requested_at is None:
        return await _ask_details(conn, contact, "proposal"), None
    await contacts_repo.set_pending_action(conn, contact.id, "proposal:email")
    return content.render("contact.ask_before_proposal",
                          faltando=content.text("contact.need_email")), None


async def _ask_contract_data(conn, contact, chat_id) -> None:
    """After "Aceitar proposta": the Flow with what the contract needs, when it
    can go (WhatsApp, published); otherwise the team takes it from here."""
    flow = await proposal_contract.after_accept(conn, contact)
    if flow is None:
        return
    text, keyboard = flow
    sent = await _send_contract(conn, contact, chat_id, text, keyboard)
    await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", sent, "contract")


async def _send_contract(conn, contact, chat_id, text: str, keyboard: dict | None) -> str:
    """A reply about the contract's data: when its Flow was refused, the person
    got the fallback and the team is told to ask by hand."""
    sent = await _send_reply(conn, contact, chat_id, text, keyboard)
    if (keyboard or {}).get("_flow") and sent != text:
        await proposal_contract.tell_team_no_flow(contact)
    return sent


async def _pause_proposal(conn, contact) -> None:
    """A booking or a person asked for by a tap or a fixed phrase: the
    questionnaire waits, its answers kept, so what is typed next is not taken
    for an answer. Asking for the proposal again picks it up (proposal.start)."""
    if contact.id is None:
        return
    b = await proposal_repo.open_for(conn, contact.id)
    if b is not None and b.step is not None:
        await proposal_repo.save(conn, b.id, answers=b.answers, step=None)


async def _start_proposal(conn, contact, chat_id, history_text: str
                          ) -> tuple[str, dict | None]:
    """The questionnaire chosen (or picked up): the times on the table, if any,
    are dropped, so what is typed next is an answer, not a pick."""
    await offers_repo.expire(conn, chat_id)
    return await proposal.start(conn, contact, history_text,
                                whatsapp=channels.is_whatsapp(chat_id))


async def _send_reply(conn, contact, chat_id, text: str, keyboard: dict | None) -> str:
    """The reply, sent. A WhatsApp Flow that WhatsApp refuses (not published,
    an old app) falls back to the same request asked one question at a time.
    What was finally sent, for the history."""
    if await channels.send(chat_id, text, keyboard) or not (keyboard or {}).get("_flow"):
        return text
    if keyboard and "_fallback" in keyboard:
        text, fallback_keyboard = keyboard["_fallback"]
        await channels.send(chat_id, text, fallback_keyboard)
        return text
    fallback = await proposal.fall_back(conn, contact)
    if fallback is None:
        return text
    text, keyboard = fallback
    await channels.send(chat_id, text, keyboard)
    return text


async def _call_the_team(conn, contact, chat_id, reason: str) -> str:
    """The handoff, as the "Falar com a equipe" button starts it: the details
    first when they are missing and were never asked for."""
    if handoff.configured() and _intake_needed(contact):
        return await _ask_details(conn, contact, f"escalation:{reason}")
    told = await handoff.start(conn, contact, chat_id, reason)
    return content.text("reply.escalation" if told else "reply.escalation_failed")


async def _proposal_tap(conn, contact, chat_id, data: str) -> tuple[str | None, dict | None]:
    """A prop:* button. A tap from a questionnaire that is no longer active
    offers both paths again, never an error."""
    if data == "prop:meet":
        await _pause_proposal(conn, contact)
        return await _start_booking(conn, contact, chat_id)
    if data == "prop:team":
        await _pause_proposal(conn, contact)
        return await _call_the_team(conn, contact, chat_id, "botao"), None
    if data == "prop:start":
        return await _start_proposal(conn, contact, chat_id, await _history_text(conn, chat_id))
    b = await proposal_repo.open_for(conn, contact.id) if contact.id is not None else None
    if data == "prop:stop":
        if b is not None:
            await proposal.stop(conn, b)
        text, keyboard = await _start_booking(conn, contact, chat_id)
        return f"{content.text('proposal.stopped')}\n\n{text}", keyboard
    if b is None:
        # "Está certo" tapped twice: the first one already sent it.
        return (None, None) if data == "prop:ok" else await proposal.offer()
    if data == "prop:skip":
        return await proposal.skip(conn, contact, b)
    if data.startswith("prop:ramo:"):
        return await proposal.set_ramo(conn, contact, b, key=data.removeprefix("prop:ramo:"))
    if data.startswith("prop:pick:"):
        step, _, key = data.removeprefix("prop:pick:").partition(":")
        return await proposal.pick(conn, b, step, key)
    if data.startswith("prop:done:"):
        return await proposal.pick_done(conn, b, data.removeprefix("prop:done:"))
    # Taps on messages sent before the buttons changed.
    if data.startswith("prop:area:"):
        return await proposal.pick_area(conn, b, data.removeprefix("prop:area:"))
    if data == "prop:areas:done":
        return await proposal.areas_done(conn, b)
    if data == "prop:ok":
        return await _proposal_ok(conn, contact, b)
    step = data.removeprefix("prop:fix:")
    if data.startswith("prop:fix:") and step in proposal.steps_for(b.answers):
        return await proposal.correct(conn, b, step)
    return proposal.fix_menu(b)


async def _handle_booking(conn, contact, chat_id, triage,
                          only_days: list[date] | None = None) -> tuple[str, dict | None]:
    if not triage.preferred_period and not _choose_day():
        return content.text("scheduling.period_question"), _period_keyboard(
            triage.preferred_day
        )
    return await _offer(
        conn, contact, chat_id,
        preferred_day=triage.preferred_day, preferred_period=triage.preferred_period,
        only_days=only_days,
    )


async def _handle_reschedule(conn, contact, chat_id, triage,
                             only_days: list[date] | None = None) -> tuple[str, dict | None]:
    """Offer new times for an appointment the person already has.

    Nothing is cancelled here. The old appointment is only released once the new
    one exists, so a person who never answers keeps the time they had.
    """
    upcoming = await appointments_repo.upcoming_for_contact(conn, contact.id or 0)
    if not upcoming:
        return await _handle_booking(conn, contact, chat_id, triage, only_days)
    if len(upcoming) > 1:
        # Asked with buttons: a typed answer had nowhere to go, and the desk
        # asked the same question forever.
        return content.render(
            "scheduling.which_to_move", lista=_listing(upcoming)
        ), _choose_keyboard(upcoming, "move")
    if not triage.preferred_period and not _choose_day():
        return content.text("scheduling.period_question"), _period_keyboard(
            triage.preferred_day, upcoming[0].id
        )
    return await _offer(
        conn, contact, chat_id,
        preferred_day=triage.preferred_day, preferred_period=triage.preferred_period,
        replaces_id=upcoming[0].id, only_days=only_days,
    )


async def _book_picked_slot(conn, offer, contact, chat_id, slot: TimeSlot, *,
                            subject: str = "") -> tuple[str, dict | None]:
    """Book the slot picked out of a live offer — by text or by a tap, the
    booking rules (claim first, re-offer if it went meanwhile) are the same."""
    result = await scheduling.take_offer(
        conn, offer=offer, contact=contact, slot=slot,
        title=f"{_title(contact)} · {subject}" if subject else _title(contact),
    )
    if result.outcome == "booked" and result.appointment:
        await funnel.advance(conn, contact.id, "booked")
        await attendance.notify_team(
            result.appointment, contact,
            "rescheduled_notice" if offer.replaces_id else "booked_notice",
        )
        text = content.render(
            "scheduling.confirmation",
            endereco=content.fact("endereco"),
            **scheduling.when_parts(slot),
        )
        return scheduling.with_meeting_link(text, result.appointment), None
    if result.outcome == "limit":
        held = await appointments_repo.upcoming_for_contact(conn, contact.id or 0)
        return _limit_reply(held) if held else (content.text("scheduling.could_not_book"), None)
    if result.outcome == "stale_reschedule":
        return content.text("booking.stale_reschedule"), None
    if result.outcome == "taken":
        # The slot went while the offer sat unanswered. Say so and offer
        # again, rather than leaving the person to guess what happened.
        # A reschedule stays a reschedule: the new offer still replaces the old time.
        taken = content.text("scheduling.slot_taken")
        text, keyboard = await _offer(conn, contact, chat_id, preferred_period=offer.period,
                                      replaces_id=offer.replaces_id)
        return f"{taken} {text}", keyboard
    return content.text("scheduling.could_not_book"), None


async def _handle_pending_offer(conn, offer, contact, chat_id, body, triage
                                ) -> tuple[str, dict | None] | None:
    """Read a reply against the times already on the table.

    Returns None when the message was not about the offer at all — someone who
    asks the price mid-booking gets an answer, and the times stay on the table
    rather than being thrown away for changing the subject.
    """
    choice = slot_choice.interpret(body, offer.slots)

    if (_choose_day() and offer.period is not None
            and slot_choice.read_day_reply(body).period == "any"
            and not slot_choice.names_a_time(body)):
        # "qualquer horário" with a morning or afternoon page open lifts the
        # filter: read like a new period, below, not as a pick among these —
        # even when the page holds a single time. A time named with it wins.
        choice = slot_choice.Choice(kind="declined", candidates=[])

    if choice.kind == "picked" and choice.slot is not None:
        return await _book_picked_slot(conn, offer, contact, chat_id, choice.slot)

    if choice.kind == "ambiguous":
        # Still pending: they narrowed it down, they just have not landed on one.
        return content.render(
            "scheduling.offer", slots=scheduling.phrase(choice.candidates)
        ), None

    if choice.kind == "declined" and _choose_day():
        # Not one of the times on the page. What was written instead decides:
        # "sexta" is Friday's times, "de tarde" this day's afternoon, "sexta ou
        # segunda à tarde" those two days; anything else, the list of days.
        said = slot_choice.read_day_reply(body)
        # A period written replaces the one on the page; "qualquer horário" lifts it.
        period = offer.period if said.period is None else _only_period(said.period)
        zone = ZoneInfo(settings.office_timezone)
        on_page = sorted({slot.start.astimezone(zone).date() for slot in offer.slots})
        named = [day for day in said.days if day >= _today()]
        if said.kind == "period_only" and len(on_page) == 1:
            named = on_page
        if said.kind in ("clear", "period_only") and len(named) == 1:
            return await _offer_day_times(conn, contact, chat_id, day=named[0],
                                          period=period, replaces_id=offer.replaces_id,
                                          typed=True)
        if said.kind == "clear" and named:
            return await _offer_days(conn, contact, chat_id, period=period,
                                     replaces_id=offer.replaces_id, only_days=named)
        return await _offer_days(conn, contact, chat_id, period=offer.period,
                                 replaces_id=offer.replaces_id)

    if choice.kind == "declined":
        await offers_repo.expire(conn, chat_id)
        # "eu falei na quinta" turns the offer down and names the day in one go:
        # go to that day. A day that was on offer is the one being refused
        # ("nao consigo dia 28/09"), so it never counts.
        named = slot_choice.requested_day(body)
        zone = ZoneInfo(settings.office_timezone)
        offered_days = {slot.start.astimezone(zone).date() for slot in offer.slots}
        if named and named >= datetime.now(zone).date() and named not in offered_days:
            return content.text("scheduling.period_question"), _period_keyboard(
                named, offer.replaces_id
            )
        # Otherwise the next message is the day. What was being moved travels with the
        # wait: without it, a reschedule answered with "quinta" became a second
        # booking, and the old appointment stayed.
        if contact.id is not None:
            await contacts_repo.set_waiting(conn, contact.id, f"day:{offer.replaces_id or 'none'}")
        return content.text("scheduling.which_day"), None

    # Unclear. If they were plainly talking about the booking, ask again;
    # otherwise let the message be handled on its own terms.
    if triage.intent in ("booking", "reschedule", "other") and _choose_day():
        return await _offer_days(conn, contact, chat_id, period=offer.period,
                                 replaces_id=offer.replaces_id)
    if triage.intent in ("booking", "reschedule", "other"):
        keyboard = _slots_keyboard(offer.id, offer.slots) if offer.id else None
        return content.render(
            "scheduling.offer", slots=scheduling.phrase(offer.slots)
        ), keyboard
    return None


async def _handle_cancel(conn, contact, chat_id, has_offer: bool) -> tuple[str, dict | None]:
    upcoming = await appointments_repo.upcoming_for_contact(conn, contact.id or 0)
    if not upcoming:
        if has_offer:
            # "Cancel" with times on the table and nothing booked means the
            # offer, not an appointment they never made.
            await offers_repo.expire(conn, chat_id)
            return content.text("scheduling.offer_dropped"), None
        return content.text("scheduling.nothing_to_cancel"), None
    if len(upcoming) > 1:
        return content.render(
            "scheduling.which_to_cancel", lista=_listing(upcoming)
        ), _choose_keyboard(upcoming, "cancel")
    # Asked, never done on a word: a misread "cancela" — or one said in the
    # middle of a reschedule, meaning only that — must not cancel a meeting.
    # The tap is handled by attendance.handle_tap, like the reminder's button.
    appointment = upcoming[0]
    return content.render(
        "scheduling.cancel_check",
        **scheduling.when_parts(TimeSlot(start=appointment.starts_at, end=appointment.ends_at)),
    ), telegram.inline_keyboard([[
        (content.text("scheduling.cancel_yes"), taps.tap("cancel_yes", appointment)),
        (content.text("scheduling.cancel_no"), taps.tap("keep", appointment)),
    ]])


def _listing(appointments) -> str:
    moments = [scheduling.when_appointment(item) for item in appointments]
    return moments[0] if len(moments) == 1 else f"{', '.join(moments[:-1])} e {moments[-1]}"


def _choose_keyboard(appointments, action: str) -> dict:
    """One button per appointment. The taps are the reminder's own ("appt:move",
    "appt:cancel"), so choosing leads into flows that already exist — cancelling
    still asks once more."""
    return telegram.inline_keyboard([
        [(scheduling.short_when(TimeSlot(start=item.starts_at, end=item.ends_at)),
          taps.tap(action, item))]
        for item in appointments
    ])


async def _list_appointments(conn, contact) -> str:
    """What this person has booked, read from the database — never guessed."""
    upcoming = await appointments_repo.upcoming_for_contact(conn, contact.id or 0)
    if not upcoming:
        return content.text("scheduling.none_upcoming")
    moments = [scheduling.when_appointment(item) for item in upcoming]
    if len(moments) == 1:
        return scheduling.with_meeting_link(
            content.render("scheduling.upcoming_one", quando=moments[0]), upcoming[0]
        )
    lista = f"{', '.join(moments[:-1])} e {moments[-1]}"
    return content.render("scheduling.upcoming_many", lista=lista)


# Hello, and nothing else: "oi", "olá!", "bom dia, tudo bem?", "e aí". Anything
# more ("oi, quanto custa?") goes to triage like any other message.
_GREETING = re.compile(
    r"^(oi+e?|ola+|opa+|e ?ai|ei|hey|hello|hi|salve|"
    r"bom dia|boa tarde|boa noite)"
    r"( (tudo bem|td bem|tudo bom|como vai|pessoal|converge|"
    r"bom dia|boa tarde|boa noite))*$"
)


# A thank-you: only these words (and thumbs/hands emoji), at most five.
_THANKS = frozenset({
    "ok", "okay", "okey", "oks", "okk", "blz", "beleza", "combinado", "certo", "perfeito",
    "show", "otimo", "obrigado", "obrigada", "obg", "obgd", "brigado", "brigada", "valeu",
    "vlw", "ta", "bom", "fechado", "top", "entendi", "ciente", "agradeco", "muito", "ate",
    "la", "joia", "tudo", "bem", "certinho", "maravilha",
})
_THANKS_EMOJI = frozenset("👍🙏👌😊🙂☺😀😄❤🤝✅👏")


def _is_thanks(body: str) -> bool:
    folded = unicodedata.normalize("NFKD", body.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    words = re.sub(r"[^a-z ]", " ", folded).split()
    rest = re.sub(r"[a-z\s.,!;:]", "", folded)
    emoji_only = all(ch in _THANKS_EMOJI or ord(ch) in (0xFE0F, 0x200D) or 0x1F3FB <= ord(ch) <= 0x1F3FF
                     for ch in rest)
    if not emoji_only or len(words) > 5:
        return False
    return bool(words or rest) and all(word in _THANKS for word in words)


# A conversation, as the team's "new conversation" notice counts it
# (inbox_repo.claim_conversation_notice): messages less than 24h apart.
CONVERSATION = timedelta(hours=24)
_NOT_READ = object()


async def _greet_in_conversation(conn, contact, chat_id, body: str) -> str:
    """A hello while a conversation is going on: greeted back, and the point
    where it stopped comes back. Mid-questionnaire, the pending question."""
    keyboard = None
    briefing = (await proposal_repo.open_for(conn, contact.id)
                if contact.id is not None else None)
    if briefing is not None and briefing.step is not None:
        asked, keyboard = proposal.question(briefing.step, briefing.answers)
        text = content.render("reply.hello_resume",
                              retomada=content.render("proposal.resume", pergunta=asked))
    else:
        text = (await greet_in_conversation(body, await _history_text(conn, chat_id))
                or content.text("reply.hello_ongoing"))
    await channels.send(chat_id, text, keyboard)
    await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "greeting")
    await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, "greeting")
    await funnel.advance(conn, contact.id, "replied")
    logger.info("Answered a greeting in the middle of a conversation")
    return "greeting"


def _is_greeting(body: str) -> bool:
    folded = unicodedata.normalize("NFKD", body.lower())
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = " ".join(re.sub(r"[^a-z ]", " ", folded).split())
    return 0 < len(folded) <= 40 and _GREETING.match(folded) is not None


def _panel_address() -> str:
    base = (settings.public_base_url or "").rstrip("/")
    return content.render("reply.panel_link", link=f"{base}/painel")


def _entry_keyboard() -> dict:
    """The three ways in: learn more, ask for a proposal, or book the call. Every
    conversation is meant to end in one of the last two."""
    return telegram.inline_keyboard([
        [(content.text("reply.button_info"), "info:start")],
        [(content.text("proposal.button_email"), "prop:start")],
        [(content.text("proposal.button_meet"), "prop:meet")],
    ])


_FORM_HOW = {"clinic": "info:form", "legal": "info:legal",
             "construction": "info:construction"}


def _form_keyboard(product: str = "clinic") -> dict | None:
    """After the lead form: how it works, the call, or the proposal. The
    construction tool is still in development: no proposal. Unclear: no buttons."""
    if product not in _FORM_HOW:
        return None
    rows = [[(content.text("form.button_how"), _FORM_HOW[product])],
            [(content.text("proposal.button_meet"), "prop:meet")]]
    if product != "construction":
        rows.append([(content.text("proposal.button_email"), "prop:start")])
    return telegram.inline_keyboard(rows)


def _handoff_keyboard() -> dict:
    return telegram.inline_keyboard([[(content.text("handoff.button"), "handoff")]])


async def _handle_group(conn, message: dict) -> str | None:
    """A group is never a customer. In the team group, "/resumo" posts the
    morning briefing on demand and "/painel" the panel's address; anywhere,
    "/id" tells a group its own id, which is how TEAM_CHAT_ID gets filled in.
    Everything else is ignored — the bot does not talk in groups, and
    answering customers happens in the panel's Inbox."""
    chat_id = (message.get("chat") or {}).get("id")
    command = (message.get("text") or "").strip().split("@")[0].split(" ")[0]
    if command == "/id":
        await telegram.post_message(chat_id, f"ID deste grupo: {chat_id}")
        return "group_id"
    team = settings.team_chat_id.strip()
    if not team or str(chat_id) != team:
        return None
    if command == "/resumo":
        await telegram.post_message(chat_id, await secretarial.daily_briefing(conn))
        return "briefing"
    if command == "/painel":
        await telegram.post_message(chat_id, _panel_address())
        return "panel_link"
    return None


def _tap_as_message(callback: dict, text: str) -> dict:
    """A button tap, as the message the person would have typed."""
    chat = (callback.get("message") or {}).get("chat") or {}
    return {"update_id": callback.get("id"), "message": {
        "message_id": callback.get("id"), "chat": chat, "from": callback.get("from") or {},
        "date": callback.get("date"), "text": text,
    }}


async def _record_blocked(conn, chat_id, message: dict, attachment) -> None:
    said = message.get("text") or message.get("caption") or message.get("_wa_event")
    if attachment is not None:
        line = content.text(f"media.line_{attachment.kind}")
        said = f"{line}\n{said}" if said else line
    contact = await contacts_repo.get_or_create(conn, chat_id, _display_name(message))
    if contact.id is not None:
        text = (said or content.text("inbox.unreadable"))[: settings.max_message_chars]
        await contacts_repo.record_message(conn, contact.id, chat_id, "user", text, "blocked")
    logger.info("A blocked contact wrote: recorded, not answered")


async def process_message(conn, update: dict) -> str | None:
    """Handle one update. Returns the intent it was handled as, if any."""
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    if chat.get("type") not in (None, "private"):
        return await _handle_group(conn, message)

    # Locked before anything is decided — the rate-limit notice included,
    # since whether the bot may speak here is part of what is decided.
    await handoffs_repo.lock_chat(conn, chat_id)
    # A media update already handled (its turn committed, its ACK was lost),
    # or too old to trust: nothing below runs, not even the rate limit.
    ended = await media.guard_event(conn, update, message)
    if ended:
        return ended
    message = media.normalized(message)
    attachment = attachment_of(message) if settings.media_enabled else None
    edited = "edited_message" in update and "message" not in update
    # Blocked in the panel: before everything else, nothing answered, nobody
    # told, nothing downloaded. On record only, under "Bloqueadas".
    if await contacts_repo.is_blocked(conn, chat_id):
        if not edited:
            await _record_blocked(conn, chat_id, message, attachment)
        return "blocked"
    # Before the limit: messages written over a day ago must not spend it.
    if channels.expired(chat_id, message):
        said = message.get("text") or message.get("caption") or message.get("_wa_event")
        if attachment is not None:
            contact = await contacts_repo.get_or_create(
                conn, chat_id, _display_name(message), inbound_at=channels.sent_at(message))
            await media.record_without_download(conn, contact, chat_id, attachment, "late")
        elif not said:
            said = content.text("inbox.unreadable")
        return await _record_expired(
            conn, chat_id, _display_name(message), message,
            said[: settings.max_message_chars] if said else None,
        )
    await _note_inbound(conn, chat_id, _display_name(message), message)
    # /teste, /teste antigo, /sair: read before the limit, the opt-out and
    # triage — the team's way in and out of a test must always work.
    intent = await test_mode.handle_command(
        conn, chat_id, message.get("text"), channels.sent_at(message))
    if intent:
        return intent
    allowed, should_notify = await _allowed(chat_id)
    if not allowed:
        if await _held(conn, chat_id):
            # Keep the customer's words visible to the person answering, even
            # during a flood. Never download/transcribe media on this path.
            said = message.get("text") or message.get("caption") or message.get("_wa_event")
            if not said and attachment is None:
                said = content.text("inbox.unreadable")
            contact = await contacts_repo.get_or_create(
                conn, chat_id, _display_name(message), inbound_at=channels.sent_at(message))
            if contact.id is not None:
                await automations_repo.mark_replied(conn, contact.id)
            if attachment is not None:
                await media.record_without_download(conn, contact, chat_id, attachment,
                                                    "rate_limited")
            if said:
                await handoff.handle_customer_message(
                    conn, contact, chat_id, said[: settings.max_message_chars])
            else:
                await handoff.held_media(conn, contact, chat_id, notify=True)
            return "handoff"
        if should_notify:
            await channels.send(chat_id, _TOO_MANY)
        return None

    # Until this turn commits, nobody can take the conversation in the panel:
    # whether a person holds it is decided once, here, and stays true.

    event = message.get('_wa_event') if channels.is_whatsapp(chat_id) else None
    if isinstance(event, str) and event:
        # Reactions, locations and shared cards are visible to staff as data.
        # They never trigger triage or an automatic "could not read" reply.
        contact = await contacts_repo.get_or_create(
            conn, chat_id, _display_name(message), inbound_at=channels.sent_at(message))
        assert contact.id is not None
        if await _held(conn, chat_id):
            await handoff.handle_customer_message(conn, contact, chat_id, event[:3900])
        else:
            noticed = await inbox_repo.claim_conversation_notice(conn, contact.id)
            await contacts_repo.record_message(conn, contact.id, chat_id, 'user', event[:3900], 'wa_event')
            if noticed:
                await inbox.send_new_conversation_notice(contact)
        return 'wa_event'

    captured: Captured | SameAttachment | None = None
    noticed = False
    # The last message before this one: read before an attachment is recorded,
    # which would make a voice "oi" after days look like a conversation going on.
    last_before: datetime | None | object = _NOT_READ
    if attachment is not None:
        contact = await contacts_repo.get_or_create(
            conn, chat_id, _display_name(message), inbound_at=channels.sent_at(message))
        last_before = await contacts_repo.last_message_at(conn, chat_id)
        # Taken before the media's line is recorded — afterwards this would no
        # longer be the first message — and spent on one notice below.
        if contact.id is not None:
            noticed = await inbox_repo.claim_conversation_notice(conn, contact.id)
        captured = await media.capture(conn, contact, chat_id, message, edited=edited)
        if noticed and not isinstance(captured, Captured):
            # The claim is spent: the paths below would not announce the
            # conversation (a caption edit, a capture undone by an SQL error).
            await inbox.send_new_conversation_notice(contact)

    if isinstance(captured, SameAttachment):
        body = await _text_of(message, skip_media=True)
        if not body:
            return None  # an edit that only removed the caption
    elif isinstance(captured, Captured):
        body = await _text_of(message, audio_bytes=captured.audio)
        if not body:
            return await _media_without_text(conn, chat_id, message, captured, noticed)
        if noticed:
            await inbox.send_new_conversation_notice(await contacts_repo.get_or_create(
                conn, chat_id, _display_name(message), inbound_at=channels.sent_at(message)))
    else:
        body = await _text_of(message)
    if not body:
        # A photo, a file, a voice note that could not be transcribed. With a
        # person holding the conversation, they hear of it in the panel
        # instead of the bot answering "Não consegui ler".
        if await _held(conn, chat_id):
            contact = await contacts_repo.get_or_create(
                conn, chat_id, _display_name(message), inbound_at=channels.sent_at(message))
            await handoff.handle_customer_message(conn, contact, chat_id, content.text("inbox.unreadable"))
            return "handoff"
        # Still the customer writing: on record for the Inbox, and it keeps
        # WhatsApp's 24h window open as Meta does.
        contact = await contacts_repo.get_or_create(
            conn, chat_id, _display_name(message), inbound_at=channels.sent_at(message))
        # Announced first: once this is on record, it is no longer the first
        # message of the conversation and nothing later would announce it.
        await inbox.notify_new_conversation(conn, contact)
        if contact.id is not None:
            await contacts_repo.record_message(
                conn, contact.id, chat_id, "user", content.text("inbox.unreadable"), "unreadable")
        if await channels.send(chat_id, _CANNOT_READ) and contact.id is not None:
            await contacts_repo.record_message(
                conn, contact.id, chat_id, "assistant", _CANNOT_READ, "unreadable")
        return None

    # The panel is a web page, not a chat command: a team member asking for it
    # in private gets its address. Anyone else's "/painel" is an ordinary
    # message — its address is no secret, but it is not the customer's business.
    if body.strip().split("@")[0].lower() == "/painel" and await contacts_repo.find_staff_by_chat(
        conn, chat_id
    ):
        if channels.is_whatsapp(chat_id):
            await channels.send(chat_id, _panel_address(), parse_mode=None)
        else:
            await telegram.send_message(chat_id, _panel_address(), parse_mode=None)
        return "panel_link"

    # A utility, like /id in a group: how a team member finds the chat_id that
    # goes into staff.json. Never triaged — to a model it reads as gibberish,
    # and gibberish gets handed to a person.
    if body.strip().split("@")[0].lower() == "/id":
        await channels.send(chat_id, f"Seu ID: {chat_id}")
        return "own_id"

    contact = await contacts_repo.get_or_create(
        conn, chat_id, _display_name(message), inbound_at=channels.sent_at(message))
    if contact.id is not None:
        await automations_repo.mark_replied(conn, contact.id)

    # A new conversation — the first message, or the first in 24h — is one
    # line in the team group with a button to the Inbox. Checked before this
    # message is recorded, which is what makes it the first.
    await inbox.notify_new_conversation(conn, contact)

    # A person holds this conversation (taken in the panel's Inbox): the
    # message is recorded for them and they are told privately. No triage, no
    # FAQ, no booking — a person is answering, and the bot has no business
    # guessing alongside them. A request nobody has taken yet leaves the bot
    # answering below.
    relayed = await handoff.handle_customer_message(conn, contact, chat_id, body)
    if relayed == "claimed":
        return "handoff"
    await channels.typing(chat_id, message.get("_wa_message_id"))

    # SAIR: no more automated messages, read by code — the way out every
    # automated message offers must work every time, with no model to misread it.
    if automations.is_opt_out(body):
        text = await automations.opt_out(conn, contact)
        await channels.send(chat_id, text)
        await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "opt_out")
        await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, "opt_out")
        logger.info("A contact opted out of automated messages")
        return "opt_out"

    # A greeting needs no model: "ola" read as a question once got "não tenho
    # essa informação", and a desk that cannot say hello cannot be trusted with
    # anything else. /start always introduces the desk; a plain hello from
    # someone it already talked to is greeted by name instead.
    command = body.strip().lower()
    # "/start site": a link that says where the person came from. The first
    # origin known is kept; anything a link says that is not an origin is
    # ignored. It is still a /start, and the desk introduces itself.
    words = command.split()
    if len(words) > 1 and words[0].split("@")[0] == "/start":
        origin = inbox.origin_from_start(body)
        if origin and contact.id is not None:
            await inbox_repo.set_origin_if_empty(conn, contact.id, origin)
        command = "/start"
    if command in ("/start", "/help") or _is_greeting(body):
        last = None
        if command not in ("/start", "/help"):
            last = (await contacts_repo.last_message_at(conn, chat_id)
                    if last_before is _NOT_READ else last_before)
        talked = isinstance(last, datetime)
        # "Oi, boa noite" a minute after the desk answered (often after the
        # message an ad writes for the person) is manners, not a new contact:
        # no introduction, no "de novo". The conversation picks up where it was.
        ended = contact.ended_at is not None and isinstance(last, datetime) \
            and contact.ended_at >= last
        if isinstance(last, datetime) and datetime.now(UTC) - last < CONVERSATION and not ended:
            return await _greet_in_conversation(conn, contact, chat_id, body)
        first_name = (contact.name or "").split()[:1]
        returning = bool(talked and first_name)
        text = (content.render("reply.welcome_back", nome=first_name[0])
                if returning else content.text("reply.welcome"))
        await channels.send(chat_id, text, _entry_keyboard())
        await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "greeting")
        await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, "greeting")
        await funnel.advance(conn, contact.id, "replied")
        logger.info("Handled a greeting message")
        return "greeting"

    # An e-mail or phone the desk does not have yet is read by pattern, before
    # any model sees the message.
    found = contact_details.extract(body)
    gaps = contact_details.missing(contact.email, contact.phone)
    new = {
        field: value
        for field, value in (("email", found.email), ("phone", found.phone))
        if value and field in gaps
    }
    if new and contact.id is not None:
        await contacts_repo.update_details(conn, contact.id, **new)
        contact = contact.model_copy(update=new)

    # The lead form's message: they already said who they are and what hurts.
    # Answered in code, tied to that, with the three ways forward.
    form = form_lead.read(body)
    if form is not None and contact.id is not None:
        text = form_lead.opening(form, contact.name)
        await channels.send(chat_id, text, _form_keyboard(form.product))
        await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "form")
        await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, "form")
        await funnel.advance(conn, contact.id, "replied")
        logger.info("Answered a lead form message")
        return "form"

    if (contact.pending_action or "").startswith("pickday:") and contact.id is not None:
        answered = await _handle_pickday(conn, contact, chat_id, body)
        if answered is not None:
            text, keyboard = answered
            await channels.send(chat_id, text, keyboard)
            await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "booking")
            await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, "booking")
            await funnel.advance(conn, contact.id, "replied")
            logger.info("Handled a typed day while the days were open")
            return "booking"
        contact = contact.model_copy(update={"pending_action": None})

    if (contact.pending_action or "").startswith(("day:", "day2:")) and contact.id is not None:
        answered = await _handle_day_reply(conn, contact, chat_id, body)
        if answered is not None:
            text, keyboard = answered
            await channels.send(chat_id, text, keyboard)
            await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "booking")
            await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, "booking")
            await funnel.advance(conn, contact.id, "replied")
            logger.info("Handled the reply to 'which day'")
            return "booking"
        contact = contact.model_copy(update={"pending_action": None})

    # Sent once the reply has gone out: the questionnaire's pending question,
    # or the note that it waits for an e-mail.
    after: tuple[str, dict | None] | None = None
    resuming = False
    briefing = (await proposal_repo.open_for(conn, contact.id)
                if contact.id is not None else None)
    if briefing is not None and briefing.step is not None and contact.id is not None:
        if (body.strip().endswith("?")
                or await offers_repo.pending_for_chat(conn, chat_id) is not None):
            # A question, not the answer — or times on the table, which the
            # message may be picking from: answered the normal way, and the
            # pending question comes back after it. Nothing is stored.
            asked, asked_keyboard = proposal.question(briefing.step, briefing.answers)
            after = (content.render("proposal.resume", pergunta=asked), asked_keyboard)
            resuming = True
        elif proposal.wants_a_person(body):
            # "quero falar com um atendente": a fixed phrase, read by code. The
            # questionnaire pauses and the team is called, as the button does.
            await _pause_proposal(conn, contact)
            text = await _call_the_team(conn, contact, chat_id, "pedido")
            await channels.send(chat_id, text)
            await contacts_repo.record_message(conn, contact.id, chat_id, "user", body,
                                               "escalation")
            await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text,
                                               "escalation")
            await funnel.advance(conn, contact.id, "replied")
            logger.info("A fixed phrase asked for a person mid-questionnaire")
            return "escalation"
        else:
            text, keyboard = await proposal.answer(conn, contact, briefing, body)
            text = await _send_reply(conn, contact, chat_id, text, keyboard)
            await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "proposal")
            await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text,
                                               "proposal")
            await funnel.advance(conn, contact.id, "replied")
            logger.info("Recorded an answer to the proposal questionnaire")
            return "proposal"

    if contact.pending_action and contact.id is not None:
        # The desk asked for the details before a booking or a handoff. Cleared
        # either way: with details it picks up where it left off; without, the
        # person changed the subject or would rather not say, and the message
        # is handled on its own terms below — never asked for them again.
        action = contact.pending_action
        contact_id = contact.id
        await contacts_repo.set_pending_action(conn, contact_id, None)
        contact = contact.model_copy(update={"pending_action": None})
        if action.startswith("proposal") and not contact.email:
            # A proposal goes by e-mail. Only a phone came: the e-mail is
            # asked for once more, alone. Otherwise the answers wait, and the
            # message is handled on its own terms.
            if new and action == "proposal":
                await contacts_repo.set_pending_action(conn, contact_id, "proposal:email")
                text = content.text("contact.ask_email")
                await channels.send(chat_id, text)
                await contacts_repo.record_message(conn, contact.id, chat_id, "user", body,
                                                   "details")
                await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text,
                                                   "proposal")
                await funnel.advance(conn, contact.id, "replied")
                logger.info("Recorded a phone and asked for the e-mail for a proposal")
                return "proposal"
            after = await _proposal_waiting(conn, contact)
        elif new:
            text, keyboard, intent = await _resume(conn, contact, chat_id, action, body)
            text = f"{content.text('contact.noted')}\n\n{text}"
            await channels.send(chat_id, text, keyboard)
            await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "details")
            await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, intent)
            await funnel.advance(conn, contact.id, "replied")
            logger.info("Recorded contact details and resumed %s", intent)
            return intent
    elif new and found.only_details and contact.id is not None:
        # Details sent unprompted: a thank-you, no model call.
        still = contact_details.missing(contact.email, contact.phone)
        key = ("contact.thanks" if not still
               else "contact.ask_email" if still == ("email",) else "contact.ask_phone")
        text = content.text(key)
        await channels.send(chat_id, text)
        await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "details")
        await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, "details")
        await funnel.advance(conn, contact.id, "replied")
        logger.info("Recorded contact details (%s)", ", ".join(sorted(new)))
        return "details"

    # A thank-you right after booking ("ok", "obrigada", 👍) is answered in
    # code: read by the model with the booking just above it, an "Ok" once
    # came back as a new request and the times were offered again.
    if contact.id is not None and _is_thanks(body):
        booked = await appointments_repo.just_booked(conn, contact.id)
        if booked is not None:
            text = content.render("booking.ack", quando=scheduling.when_appointment(booked))
            await channels.send(chat_id, text)
            await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "thanks")
            await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, "thanks")
            await funnel.advance(conn, contact.id, "replied")
            logger.info("Answered a thank-you after a booking")
            return "thanks"

    # "Quero ser atendido por você": the person picks the desk. Read in code, so
    # it is never taken for a request for someone from the team.
    if contact.id is not None and proposal.chose_the_ai(body):
        text = content.text("reply.stay_with_ai")
        await channels.send(chat_id, text, telegram.inline_keyboard(
            [[(content.text("reply.button_start_proposal"), "prop:start")]]))
        await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, "stay_with_ai")
        await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, "stay_with_ai")
        await funnel.advance(conn, contact.id, "replied")
        logger.info("The person chose to be served by the desk")
        return "stay_with_ai"

    history_text = await _history_text(conn, chat_id)

    triage, reply = await route(
        body, str(chat_id), history_text,
        # The desk answers only from the organization's own FAQ. Retrieval needs
        # a connection, so it is handed in rather than reached for inside the graph.
        retrieve=lambda question: knowledge.retrieve(conn, question),
    )
    only_days: list[date] | None = None
    if triage.intent in ("booking", "reschedule"):
        # The day is read in code: the model does not know what today is. Only
        # a clearly chosen day counts — "quinta não posso" never makes Thursday
        # the preferred day.
        said = slot_choice.read_day_reply(body)
        ahead = [day for day in said.days if day >= _today()]
        if said.kind == "clear" and len(ahead) == 1:
            triage = triage.model_copy(update={"preferred_day": ahead[0]})
        elif said.kind == "clear" and len(ahead) > 1 and _choose_day():
            triage = triage.model_copy(update={"preferred_day": None})
            only_days = ahead
        elif said.kind == "clear" and len(ahead) > 1:
            triage = triage.model_copy(update={"preferred_day": ahead[0]})
        elif said.kind == "mixed":
            triage = triage.model_copy(update={"preferred_day": None})

    # An offer already on the table is read before anything the model said.
    # "a terca" means the Tuesday this desk just offered, and triage has no
    # reason to classify those two words as a booking — left to it, the desk
    # answers them as a new conversation and offers the same three times again.
    pending = await offers_repo.pending_for_chat(conn, chat_id)
    # What the model decided, for explaining an answer later — never the words
    # nor the model's summary of them (docs/OBSERVABILIDADE.md).
    logger.info(
        "Triage for contact %s: intent=%s confidence=%.2f day=%s period=%s pending_offer=%s",
        contact.id, triage.intent, triage.confidence,
        triage.preferred_day.isoformat() if triage.preferred_day else "-",
        triage.preferred_period or "-", "yes" if pending else "no",
    )
    text: str | None = None
    keyboard: dict | None = None
    offer_answered = False
    if pending and triage.intent != "cancel":
        result = await _handle_pending_offer(conn, pending, contact, chat_id, body, triage)
        if result is not None:
            text, keyboard = result
            offer_answered = True

    if (resuming and briefing is not None
            and (offer_answered or triage.intent in _PAUSES_PROPOSAL)):
        # A booking or a person asked for mid-questionnaire: it pauses, its
        # answers kept, and what follows is not taken for an answer. Asking
        # for the proposal again picks it up (proposal.start).
        await proposal_repo.save(conn, briefing.id, answers=briefing.answers, step=None)
        briefing = briefing.model_copy(update={"step": None})
        after = None

    if text is None and triage.intent == "proposal":
        # Both paths, the call or the questionnaire. One already running is
        # not started over: picked up where it stopped (or its summary), or,
        # with a question pending, the normal answer and then the question.
        if briefing is None and proposal.chose_the_proposal(body):
            # Already chose it: the questions, not the same two buttons again.
            text, keyboard = await _start_proposal(conn, contact, chat_id, history_text)
        elif briefing is None:
            text, keyboard = await proposal.offer()
        elif briefing.step is None:
            text, keyboard = await _start_proposal(conn, contact, chat_id, "")

    # Nothing to do with the offer: whatever the desk was going to say stands.
    text = text or reply.text
    if not text:
        if triage.intent == "booking":
            if _intake_needed(contact):
                text = await _ask_details(conn, contact, _booking_action(triage, only_days))
            else:
                text, keyboard = await _handle_booking(conn, contact, chat_id, triage,
                                                       only_days)
        elif triage.intent == "reschedule":
            text, keyboard = await _handle_reschedule(conn, contact, chat_id, triage,
                                                      only_days)
        elif triage.intent == "cancel":
            text, keyboard = await _handle_cancel(conn, contact, chat_id, pending is not None)
        elif triage.intent == "appointments":
            text = await _list_appointments(conn, contact)
    text = text or "Certo, ja verifico isso e retorno."

    if reply.needs_human and text == reply.text:
        if triage.intent == "escalation" and handoff.configured() and _intake_needed(contact):
            text = await _ask_details(conn, contact, "escalation:pedido")
        elif triage.intent == "escalation":
            # A person was asked for: tell the team, and promise only what
            # actually happened.
            told = await handoff.start(conn, contact, chat_id, "pedido")
            text = content.text("reply.escalation" if told else "reply.escalation_failed")
        elif handoff.configured():
            # "Não tenho essa informação" — one tap hands it to the team.
            keyboard = _handoff_keyboard()

    text = await _send_reply(conn, contact, chat_id, text, keyboard)
    # Recorded together, and only once a reply actually went out: a booking
    # attempt that crashes mid-handling (a bad calendar credential, say) gets
    # retried from scratch, and a message recorded before that point would be
    # written again on every attempt — duplicated in its own history, and
    # confusing the very triage call that reads it back.
    await contacts_repo.record_message(conn, contact.id, chat_id, "user", body, triage.intent)
    await contacts_repo.record_message(
        conn, contact.id, chat_id, "assistant", text, triage.intent
    )
    if after is not None:
        await channels.send(chat_id, after[0], after[1])
        await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", after[0],
                                           "proposal")
    await funnel.advance(conn, contact.id, "replied")
    logger.info("Handled a %s message", triage.intent)
    return triage.intent


async def process_callback(conn, update: dict) -> str | None:
    """One button tap: manhã/tarde, or a time slot. Never goes through triage —
    a tap already says exactly what it means, which is the whole point of
    reading it with code instead of a model."""
    callback = update.get("callback_query") or {}
    callback_id = callback.get("id")
    data = callback.get("data") or ""
    chat_id = ((callback.get("message") or {}).get("chat") or {}).get("id")
    if chat_id is None or callback_id is None:
        return None

    # Ack first: Telegram leaves the tap's loading spinner on screen until this
    # is called, whatever the tap turns out to mean. The webhook normally did
    # it already, the moment the tap arrived.
    if not callback.get("_acked"):
        await telegram.answer_callback_query(callback_id)

    # A tap in a group is the team's, never a customer's.
    chat_type = ((callback.get("message") or {}).get("chat") or {}).get("type")
    if chat_type in ("group", "supergroup"):
        if data.startswith(taps.OUTCOME_PREFIXES) and await attendance.handle_team_tap(conn, callback):
            return "outcome"
        return None

    # "Quero continuar" is the person writing back: it goes the way a typed
    # message does — from before the limit and the locks, which the message
    # path takes once — so the agent picks the conversation up from its history.
    if data.startswith("resume:yes:"):
        return await process_message(conn, _tap_as_message(
            callback, content.text("automation.resume_yes")))
    # The button under a template the company started past the 24h window
    # (a charge, or the team asking to reopen): same, the person wrote back.
    if data == "lead:yes":
        return await process_message(conn, _tap_as_message(callback, content.text("lead.yes")))
    if data == "propdoc:doubt":
        return await process_message(conn, _tap_as_message(
            callback, content.text("proposal_doc.doubt")))
    if data.startswith("reopen:"):
        return await process_message(conn, _tap_as_message(callback, content.text(
            "billing.charge_doubt" if data == "reopen:charge" else "inbox.reopen_tap")))

    await handoffs_repo.lock_chat(conn, chat_id)  # see process_message
    # A blocked person's taps do nothing. Read under the lock, so a block
    # being saved right now is seen.
    if await contacts_repo.is_blocked(conn, chat_id):
        logger.info("A blocked contact tapped a button: ignored")
        return "blocked"
    if channels.expired(chat_id, callback):
        return await _record_expired(conn, chat_id, _display_name(callback), callback,
                                     f"(tocou no botão) {_tapped_label(callback, data)}")
    await _note_inbound(conn, chat_id, _display_name(callback), callback)
    allowed, _ = await _allowed(chat_id)
    if not allowed:
        if not await _held(conn, chat_id):
            return None
        contact = await contacts_repo.get_or_create(
            conn, chat_id, _display_name(callback), inbound_at=channels.sent_at(callback))
        await handoff.handle_customer_message(
            conn, contact, chat_id, f"(tocou no botão) {_tapped_label(callback, data)}"
        )
        return "handoff"
    contact = await contacts_repo.get_or_create(
            conn, chat_id, _display_name(callback), inbound_at=channels.sent_at(callback))

    # A person holds this conversation: a button from an earlier message must
    # not book, cancel or answer behind their back. The tap goes to them.
    # Accepting or declining the proposal they sent is the exception: the
    # customer gets the fixed answer and the team its notice, as well.
    if data.startswith(f"flow:{proposal_contract.TOKEN}") and await _held(conn, chat_id):
        # The contract's data, sent while the person who sent the proposal holds
        # the conversation: kept, answered, and the person hears it.
        chose = _tapped_label(callback, data)
        text, keyboard = await proposal_contract.apply(
            conn, contact, data.removeprefix("flow:"), callback.get("_flow") or {})
        await handoff.handle_customer_message(conn, contact, chat_id, f"(tocou no botão) {chose}")
        text = await _send_contract(conn, contact, chat_id, text, keyboard)
        await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text,
                                           "contract")
        return "handoff"
    if data in ("propdoc:accept", "propdoc:decline") and await _held(conn, chat_id):
        chose, text = await proposal_send.decision(contact, data)
        await handoff.handle_customer_message(conn, contact, chat_id, f"(tocou no botão) {chose}")
        if await channels.send(chat_id, text):
            await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text,
                                               "proposal_doc")
        if data == "propdoc:accept":
            await _ask_contract_data(conn, contact, chat_id)
        return "handoff"
    info = proposal_info.tap(data)
    info_label: str | None = None
    if info is not None:
        # The buttons of "Pedir mais informações", sent by the person who holds
        # the conversation. "Agendar conversa" of a request still waiting hands
        # it back to the bot, which books; "Conversar por aqui", or a button of
        # an old request, stays with whoever holds it now.
        choice, briefing_id = info
        info_label = _tapped_label(callback, data)
        held = await _held(conn, chat_id)
        if held and choice == "meet" and await proposal_info.waiting(conn, contact, briefing_id):
            # Whoever asked hears it (privately); the tap itself is recorded
            # once, below, with the booking it starts.
            session = await handoffs_repo.active_session(conn, chat_id)
            if session is not None and session.id is not None:
                await inbox.ping_owner(conn, session, contact)
                await handoffs_repo.close(conn, session.id)
            held = False
        if not held:
            data = f"prop:{choice}"
    if await _held(conn, chat_id):
        await handoff.handle_customer_message(
            conn, contact, chat_id, f"(tocou no botão) {_tapped_label(callback, data)}"
        )
        return "handoff"

    follow_up = False  # after "Aceitar proposta": the contract's data next
    text: str | None = None
    keyboard: dict | None = None
    # What the tap meant, in words: this goes into the history the next triage
    # call reads, and "slot:1:0" tells a model nothing.
    chose = data

    if data.startswith("period:"):
        _, period, day_token, replaces_token = data.split(":", 3)
        chose = content.text(f"scheduling.period_{period}")
        preferred_day = date.fromisoformat(day_token) if day_token != "none" else None
        replaces_id = int(replaces_token) if replaces_token != "none" else None
        text, keyboard = await _offer(
            conn, contact, chat_id,
            preferred_day=preferred_day, preferred_period=period,
            replaces_id=replaces_id,
        )
    elif data == "handoff":
        chose = content.text("handoff.button")
        await _pause_proposal(conn, contact)
        text = await _call_the_team(conn, contact, chat_id, "botao")
    elif data.startswith("why:"):
        chose, text = await automations.handle_reason_tap(conn, contact, data)
    elif data.startswith("resume:no:"):
        chose, text = await automations.handle_resume_no(conn, contact, data)
    elif data == "lead:no":
        chose, text = content.text("lead.no"), content.text("lead.no_reply")
    elif data in ("propdoc:accept", "propdoc:decline"):
        chose, text = await proposal_send.decision(contact, data)
        if data == "propdoc:accept":
            follow_up = True
    elif data.startswith(("day:", "days:", "more:")):
        nav = booking_lists.parse(data)
        if nav is None:
            # An old or broken button: the days of today, never an error.
            text, keyboard = await _offer_days(conn, contact, chat_id, period=None,
                                               replaces_id=None)
        elif nav.kind == "days":
            chose = content.text("booking.days_next")
            text, keyboard = await _offer_days(conn, contact, chat_id, period=nav.period,
                                               replaces_id=nav.replaces, first=nav.day)
        else:
            chose = booking_lists.day_title(nav.day)
            text, keyboard = await _offer_day_times(conn, contact, chat_id, day=nav.day,
                                                    period=nav.period, replaces_id=nav.replaces,
                                                    after=nav.after)
    elif data == "limit:keep":
        chose = content.text("booking.limit_keep")
        text = content.text("booking.limit_kept")
    elif data == "info:start":
        chose = content.text("reply.button_info")
        text = content.text("reply.info_topics")
    elif data == "info:form":
        chose = content.text("form.button_how")
        text = content.text("form.how_it_works")
    elif data == "info:legal":
        chose = content.text("form.button_how")
        text = content.text("form.legal_how_it_works")
    elif data == "info:construction":
        chose = content.text("form.button_how")
        text = content.text("form.construction_how_it_works")
    elif data == "book:start":
        chose = content.text("automation.rebook")
        await _pause_proposal(conn, contact)
        text, keyboard = await _start_booking(conn, contact, chat_id)
    elif data.startswith("prop:"):
        chose = info_label or _tapped_label(callback, data)
        text, keyboard = await _proposal_tap(conn, contact, chat_id, data)
    elif data.startswith(f"flow:{proposal_contract.TOKEN}"):
        # The contract's data, answered (after "Aceitar proposta").
        chose = _tapped_label(callback, data)
        text, keyboard = await proposal_contract.apply(
            conn, contact, data.removeprefix("flow:"), callback.get("_flow") or {})
    elif data.startswith(f"flow:{proposal.FLOW_TOKEN}"):
        # The proposal's WhatsApp Flow, answered (whatsapp_updates): the
        # answers ride on the tap.
        chose = _tapped_label(callback, data)
        text, keyboard = await proposal.apply_flow(
            conn, contact, data.removeprefix("flow:"), callback.get("_flow") or {})
    elif data.startswith(("appt:move:", "appt2:move:")):
        chose = content.text("scheduling.reminder_reschedule")
        _, appointment_id, tapped_stamp = taps.parse(data)
        appointment = await attendance.own_upcoming(conn, contact, appointment_id)
        if appointment is None or not taps.still_valid(appointment, tapped_stamp):
            text = content.text("scheduling.reminder_stale")
        else:
            # Same path as a typed "quero remarcar": the old time is released
            # only once the new one is booked.
            if _choose_day():
                text, keyboard = await _offer(conn, contact, chat_id,
                                              replaces_id=appointment.id)
            else:
                text = content.text("scheduling.period_question")
                keyboard = _period_keyboard(None, appointment.id)
    elif data.startswith(taps.APPOINTMENT_PREFIXES):
        chose, text, keyboard = await attendance.handle_tap(conn, contact, data)
    elif data.startswith(f"flow:{BOOKING_TOKEN}"):
        chose = content.text("forms.sent_label")
        text, keyboard = await _book_from_flow(
            conn, contact, chat_id, data.removeprefix(f"flow:{BOOKING_TOKEN}"),
            callback.get("_flow") or {})
    elif data.startswith("slot:"):
        _, offer_id_token, index_token = data.split(":", 2)
        offer = await offers_repo.pending_for_chat(conn, chat_id)
        # The offer this button belonged to may have been superseded by a
        # newer one since it was sent — a stale tap must not book blind
        # against whatever offer happens to be live now.
        if offer and offer.id == int(offer_id_token) and int(index_token) < len(offer.slots):
            picked = offer.slots[int(index_token)]
            chose = scheduling.when(picked)
            text, keyboard = await _book_picked_slot(conn, offer, contact, chat_id, picked)
        else:
            text = content.text("scheduling.slot_taken")

    if not text:
        return None

    if data.startswith(f"flow:{proposal_contract.TOKEN}"):
        text = await _send_contract(conn, contact, chat_id, text, keyboard)
    else:
        text = await _send_reply(conn, contact, chat_id, text, keyboard)
    label = data.split(":", 1)[0]
    intent = ("proposal" if (label == "prop" and data not in ("prop:meet", "prop:team"))
              or data.startswith(f"flow:{proposal.FLOW_TOKEN}") else None)
    await contacts_repo.record_message(
        conn, contact.id, chat_id, "user", f"(tocou no botão) {chose}", intent
    )
    await contacts_repo.record_message(conn, contact.id, chat_id, "assistant", text, intent)
    await funnel.advance(conn, contact.id, "replied")
    if follow_up:
        await _ask_contract_data(conn, contact, chat_id)
    logger.info("Handled a %s callback", label)
    return label
