"""The one door every message to a customer goes through.

The chat says the channel: a "wa:" chat is WhatsApp, anything else Telegram.
Callers keep building Telegram keyboards; for WhatsApp every choice becomes a
list (up to 10 rows), however few the options, within Meta's limits. Messages to the
team — the group, a private notice to whoever holds a conversation — do not
come through here: the team stays on Telegram. The one exception is a team
member's own WhatsApp chat (the /painel answer), which can only go back the
way it came.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from core import content
from core.config import settings
from core.integrations import telegram, whatsapp
from core.integrations.media_download import Download, Failed
from core.integrations.whatsapp import Delivery

logger = logging.getLogger(__name__)

WA_PREFIX = "wa:"
_MEDIA_PREFIX = "wa-media:"

_TEXT_LIMIT = 4096
_INTERACTIVE_BODY_LIMIT = 1024
_MAX_ROWS = 10
_BUTTON_TITLE = 20
_ROW_TITLE = 24
_ROW_DESCRIPTION = 72

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_CODE = re.compile(r"`([^`]*)`")


# WhatsApp takes free-form messages only within 24h of the customer's last one.
WHATSAPP_WINDOW = timedelta(hours=24)


def sent_at(item: dict) -> datetime:
    """When the person wrote, by the channel's clock (Telegram's and Meta's
    "date"); now, when the channel did not say (a Telegram tap)."""
    stamp = item.get("date")
    if isinstance(stamp, int) and stamp > 0:
        return datetime.fromtimestamp(stamp, tz=UTC)
    return datetime.now(tz=UTC)


def expired(chat_id, item: dict) -> bool:
    """A WhatsApp message handled more than 24h after it was written — held
    for office hours, or behind an outage: Meta would refuse any free-form
    answer to it."""
    return is_whatsapp(chat_id) and datetime.now(tz=UTC) - sent_at(item) > WHATSAPP_WINDOW


def whatsapp_ids(number: str | None) -> list[str]:
    """Every chat id WhatsApp may use for one phone number.

    Brazilian mobiles exist with and without the ninth digit, and Meta sends
    whichever form the number was registered in, which may not be the one the
    person types. Both are kept, so the same person is the same chat. The 9 is
    added or dropped only in the old mobile range (6-9): below it, the shorter
    form is a landline, which may well be a customer's.

    A number that cannot be a WhatsApp id gives none: a Brazilian one must
    carry 55 and a DDD, and any other one a leading + with its country code.
    """
    raw = (number or "").strip()
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("55"):
        local = digits[2:]
        if len(local) == 10:
            forms = [digits]
            if local[2] in "6789":
                forms.append("55" + local[:2] + "9" + local[2:])
        elif len(local) == 11 and local[2] == "9":
            forms = [digits]
            if local[3] in "6789":
                forms.append("55" + local[:2] + local[3:])
        else:
            return []
    elif raw.startswith("+") and 8 <= len(digits) <= 15:
        forms = [digits]
    else:
        return []
    return [f"{WA_PREFIX}{form}" for form in forms]


def is_whatsapp(chat_id) -> bool:
    return str(chat_id).startswith(WA_PREFIX)


def channel_of(chat_id) -> str:
    return "whatsapp" if is_whatsapp(chat_id) else "telegram"


def to_whatsapp_text(text: str) -> str:
    """Telegram's Markdown in WhatsApp's: *bold* and _italic_ already match;
    a link has no markup there, so it is written out; code loses its ticks."""
    return _CODE.sub(r"\1", _LINK.sub(r"\1: \2", text))


def _buttons(keyboard: dict | None
             ) -> tuple[list[tuple[str, str, str | None]], list[tuple[str, str]]]:
    """(title, callback data, description) for the tap buttons, (label, url)
    for the links. A menu's title is its label alone (Telegram's text also
    carries the description)."""
    taps: list[tuple[str, str, str | None]] = []
    links: list[tuple[str, str]] = []
    for row in (keyboard or {}).get("inline_keyboard") or []:
        for button in row:
            if button.get("url"):
                links.append((button.get("text") or "", button["url"]))
            elif button.get("callback_data"):
                taps.append((button.get("_label") or button.get("text") or "",
                             button["callback_data"], button.get("_description")))
    return taps, links


def flow_keyboard(*, flow_id: str, token: str, cta: str, screen: str,
                  data: dict[str, Any]) -> dict:
    """Ask for a WhatsApp Flow instead of the questions. A "_" key, so Telegram
    never sees it: there the questions are asked one by one."""
    return {"_flow": {"flow_id": flow_id, "token": token, "cta": cta, "screen": screen,
                      "data": data}}


def telegram_markup(keyboard: dict | None) -> dict | None:
    """The keyboard as Telegram takes it: without a menu's "_" keys."""
    if not keyboard:
        return keyboard
    clean = {k: v for k, v in keyboard.items() if not k.startswith("_")}
    if "inline_keyboard" in clean:
        clean["inline_keyboard"] = [
            [{k: v for k, v in button.items() if not k.startswith("_")} for button in row]
            for row in clean["inline_keyboard"]
        ]
    return clean or None


async def _whatsapp(to: str, text: str, keyboard: dict | None) -> Delivery:
    flow = (keyboard or {}).get("_flow")
    if flow:
        return await whatsapp.send_flow(to, text[:_INTERACTIVE_BODY_LIMIT], **flow)
    taps, links = _buttons(keyboard)
    for label, url in links:
        text = f"{text}\n\n{label}: {url}"
    text = text[:_TEXT_LIMIT]
    if not taps:
        return await whatsapp.send_text(to, text)
    if len(text) > _INTERACTIVE_BODY_LIMIT:
        first = await whatsapp.send_text(to, text)
        if first != "ok":
            return first
        text = content.text("whatsapp.choose_prompt")
    list_button = (keyboard or {}).get("_list_button")
    if list_button:
        # A menu of days or times: always a list, with its descriptions.
        return await whatsapp.send_list(
            to, text, list_button[:_BUTTON_TITLE],
            [(title[:_ROW_TITLE], data, (description or "")[:_ROW_DESCRIPTION])
             if description else (title[:_ROW_TITLE], data)
             for title, data, description in taps[:_MAX_ROWS]],
        )
    # Every other choice is a list too, however few: one way to choose.
    if len(taps) > _MAX_ROWS:
        logger.warning("WhatsApp list takes %d rows; %d offered, the rest dropped",
                       _MAX_ROWS, len(taps))
    return await whatsapp.send_list(
        to, text, content.text("whatsapp.list_button"),
        [(title[:_ROW_TITLE], data) for title, data, _ in taps[:_MAX_ROWS]],
    )


async def deliver(chat_id, text: str, keyboard: dict | None = None,
                  parse_mode: str | None = "Markdown") -> Delivery:
    """Send to the customer on their channel, saying how it went."""
    if not is_whatsapp(chat_id):
        sent = await telegram.send_message(chat_id, text, parse_mode=parse_mode,
                                           reply_markup=telegram_markup(keyboard))
        return "ok" if sent else "failed"
    if not settings.whatsapp_configured():
        logger.error("A WhatsApp chat got a message while WhatsApp is not configured")
        return "failed"
    if parse_mode:
        text = to_whatsapp_text(text)
    return await _whatsapp(str(chat_id).removeprefix(WA_PREFIX), text, keyboard)


async def send(chat_id, text: str, keyboard: dict | None = None,
               parse_mode: str | None = "Markdown") -> bool:
    return await deliver(chat_id, text, keyboard, parse_mode) == "ok"


async def typing(chat_id, wa_message_id: str | None = None) -> None:
    """"digitando…": on WhatsApp it rides on marking the message just received
    as read, so without that message there is nothing to show it on."""
    if not is_whatsapp(chat_id):
        await telegram.send_typing(chat_id)
    elif wa_message_id and settings.whatsapp_configured():
        await whatsapp.mark_read_typing(wa_message_id)


async def fetch_attachment(file_id: str, *, declared: int | None = None) -> Download:
    """What a customer sent, on their channel, within MAX_MEDIA_BYTES and
    MEDIA_DOWNLOAD_SECONDS. Cancelling the turn is not swallowed: only this
    download's own deadline becomes Failed("timeout")."""
    try:
        async with asyncio.timeout(settings.media_download_seconds):
            if file_id.startswith(_MEDIA_PREFIX):
                return await whatsapp.fetch_media(file_id.removeprefix(_MEDIA_PREFIX),
                                                  settings.max_media_bytes)
            return await telegram.fetch_file(file_id, max_bytes=settings.max_media_bytes,
                                             declared=declared)
    except TimeoutError:
        return Failed("timeout")


async def download_voice(file_id: str) -> bytes | None:
    if file_id.startswith(_MEDIA_PREFIX):
        found = await whatsapp.download_media(file_id.removeprefix(_MEDIA_PREFIX),
                                              settings.max_voice_bytes)
        return found[0] if found else None
    return await telegram.download_voice(file_id)
