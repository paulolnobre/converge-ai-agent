"""Async Telegram Bot API client (long polling + messaging).

Hardening:
- every request carries a timeout (read timeout > long-poll window);
- send_message retries without parse_mode when Telegram rejects broken
  Markdown (LLM output or user-controlled text can contain stray markers);
- voice downloads are size-capped.
"""
import asyncio
import logging
from dataclasses import dataclass

import httpx

from core.config import TELEGRAM_TOKEN, TELEGRAM_URL, settings
from core.integrations.media_download import (
    Download,
    Downloaded,
    Failed,
    TooLarge,
    declared_over,
    fetch_capped,
)

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


# Failures before the request left this machine: Telegram never saw it, so one
# more try cannot deliver it twice. A timeout after sending may have delivered.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout)
_RETRY_SECONDS = 1.0


async def _retry_pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _post_once_more(url: str, **kwargs) -> httpx.Response:
    try:
        return await _http().post(url, **kwargs)
    except _NEVER_SENT as exc:
        logger.warning("Telegram unreachable (%s); trying once more", type(exc).__name__)
        await _retry_pause(_RETRY_SECONDS)
        return await _http().post(url, **kwargs)


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        # read timeout must exceed the getUpdates long-poll window (10s)
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=35.0))
    return _client


@dataclass(frozen=True)
class Choice:
    """One option of a menu. `description` is the line under the title on
    WhatsApp ("6 horários") and follows the label on Telegram."""

    label: str
    data: str
    description: str | None = None


def menu(rows: list[list[Choice]], *, list_button: str | None = None) -> dict:
    """An inline keyboard that also carries what WhatsApp needs to show a list
    with descriptions. The extra keys start with "_" and never reach Telegram
    (channels.telegram_markup removes them). With `list_button`, WhatsApp always
    shows a list behind that button, even for one to three options."""
    keyboard: dict = {"inline_keyboard": [
        [{"text": f"{c.label} · {c.description}" if c.description else c.label,
          "callback_data": c.data, "_label": c.label,
          **({"_description": c.description} if c.description else {})}
         for c in row]
        for row in rows
    ]}
    if list_button:
        keyboard["_list_button"] = list_button
    return keyboard


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """A Telegram inline keyboard from rows of (label, callback_data).

    A tap sends `callback_data` back on a `callback_query` update — read by
    code, never by the model, so the same choice made in a menu is never
    ambiguous the way free text can be.
    """
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in rows
        ]
    }


def link_keyboard(label: str, url: str) -> dict:
    """One button that opens an address instead of sending a tap back — the
    Inbox notices' "Abrir no painel"."""
    return {"inline_keyboard": [[{"text": label, "url": url}]]}


async def send_message(
    chat_id,
    text,
    base_url=None,
    parse_mode: str | None = "Markdown",
    reply_markup: dict | None = None,
) -> bool:
    base = base_url or TELEGRAM_URL
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = await _post_once_more(f"{base}/sendMessage", json=payload)
        if resp.status_code != 200 and parse_mode:
            # Broken Markdown from LLM/user content — retry as plain text
            fallback = {"chat_id": chat_id, "text": text}
            if reply_markup:
                fallback["reply_markup"] = reply_markup
            resp = await _post_once_more(f"{base}/sendMessage", json=fallback)
        if resp.status_code != 200:
            logger.error("Telegram sendMessage returned status %s", resp.status_code)
            return False
        return True
    except httpx.HTTPError as e:
        # The type goes in the template: the admin alert shows only that.
        logger.error(f"sendMessage transport error ({type(e).__name__})", exc_info=e)
        return False


async def post_message(chat_id, text, reply_markup: dict | None = None,
                       base_url=None) -> int | None:
    """Send plain text and return Telegram's message id, or None if it failed.

    Plain text on purpose: what goes in a handoff card was written by someone
    else, and Markdown would let it reshape the card.
    """
    base = base_url or TELEGRAM_URL
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = await _post_once_more(f"{base}/sendMessage", json=payload)
        if resp.status_code != 200:
            logger.error("Telegram sendMessage returned status %s", resp.status_code)
            return None
        return resp.json().get("result", {}).get("message_id")
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"post_message transport error ({type(e).__name__})", exc_info=e)
        return None


async def answer_callback_query(
    callback_query_id, text: str | None = None, base_url=None
) -> bool:
    """Acknowledge a button tap. Telegram leaves the tap's loading spinner on
    the person's screen until this is called, whatever we do with the tap."""
    base = base_url or TELEGRAM_URL
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        resp = await _http().post(f"{base}/answerCallbackQuery", json=payload)
        if resp.status_code != 200:
            logger.warning(
                "answerCallbackQuery returned status %s", resp.status_code
            )
            return False
        return True
    except httpx.HTTPError as e:
        logger.error("answerCallbackQuery transport error: %s", e)
        return False


async def send_document(
    chat_id,
    data: bytes,
    filename: str,
    *,
    caption: str | None = None,
    base_url: str | None = None,
) -> bool:
    """Upload a generated file (report, spreadsheet, PDF) back into the chat."""
    base = base_url or TELEGRAM_URL
    form = {"chat_id": str(chat_id)}
    if caption:
        form["caption"] = caption
    try:
        response = await _http().post(
            f"{base}/sendDocument",
            data=form,
            files={"document": (filename, data)},
        )
        if response.status_code != 200:
            logger.error("Telegram sendDocument returned status %s", response.status_code)
            return False
        return True
    except httpx.HTTPError as exc:
        logger.error("sendDocument transport error: %s", exc)
        return False


async def send_typing(chat_id, base_url=None):
    base = base_url or TELEGRAM_URL
    try:
        await _http().post(
            f"{base}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}
        )
    except httpx.HTTPError as e:
        logger.debug("sendChatAction failed: %s", e)


async def download_voice(file_id: str, token: str | None = None) -> bytes | None:
    """Fetch a voice/audio file. `token` selects the bot — the file endpoint
    carries the token in the path, so a base_url alone would not be enough."""
    return await download_file(file_id, token=token, max_bytes=settings.max_voice_bytes)


async def fetch_file(file_id: str, *, max_bytes: int, declared: int | None = None,
                     token: str | None = None) -> Download:
    """A Telegram file with a hard cap. `declared` (the update's file_size)
    over the cap skips even getFile; getFile's own size is checked too."""
    over = declared_over(declared, max_bytes)
    if over is not None:
        return TooLarge(over)
    bot = token or TELEGRAM_TOKEN
    try:
        resp = await _http().get(
            f"https://api.telegram.org/bot{bot}/getFile", params={"file_id": file_id}
        )
        result = resp.json().get("result", {})
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Failed to look up a Telegram file: %s", type(exc).__name__)
        return Failed(type(exc).__name__)
    file_path = result.get("file_path")
    if not file_path:
        return Failed("no file_path")
    over = declared_over(result.get("file_size"), max_bytes)
    if over is not None:
        return TooLarge(over)
    return await fetch_capped(_http(), f"https://api.telegram.org/file/bot{bot}/{file_path}",
                              max_bytes=max_bytes)


async def download_file(
    file_id: str,
    token: str | None = None,
    max_bytes: int | None = None,
) -> bytes | None:
    """Download a Telegram file with an optional hard size limit."""
    found = await fetch_file(file_id, token=token,
                             max_bytes=max_bytes if max_bytes is not None else 2**62)
    if isinstance(found, TooLarge):
        logger.warning("Telegram file too large, rejected")
    return found.data if isinstance(found, Downloaded) else None


async def close() -> None:
    """Release the shared Telegram HTTP client on replica shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def send_attachment(
    chat_id, data: bytes, kind: str, mime: str, filename: str, caption: str = ""
) -> str:
    methods = {
        "image": ("sendPhoto", "photo"),
        "audio": ("sendVoice", "voice"),
        "document": ("sendDocument", "document"),
    }
    if kind not in methods:
        return "failed"
    method, field = methods[kind]
    try:
        response = await _http().post(
            f"{TELEGRAM_URL}/{method}",
            data={"chat_id": str(chat_id), **({"caption": caption} if caption else {})},
            files={field: (filename, data, mime)},
        )
    except httpx.HTTPError:
        return "uncertain"
    try:
        body = response.json()
        if response.is_success and body.get("ok") and (body.get("result") or {}).get("message_id"):
            return "ok"
    except (ValueError, TypeError):
        return "uncertain"
    return "uncertain" if response.status_code >= 500 else "failed"
