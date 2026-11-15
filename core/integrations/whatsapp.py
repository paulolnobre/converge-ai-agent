"""Async WhatsApp Cloud API client — straight with Meta, no intermediary.

Shaped like telegram.py: one lazy HTTP client, every call answers instead of
raising. A send says more than yes/no, because one refusal is different from
the others: outside the 24h window Meta takes only approved templates, and the
Inbox has to tell the person that instead of "could not send".
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Literal

import httpx

from core.config import settings
from core.integrations.media_download import (
    Download,
    Downloaded,
    Failed,
    TooLarge,
    declared_over,
    fetch_capped,
)

logger = logging.getLogger(__name__)

Delivery = Literal["ok", "failed", "window_closed"]

_WINDOW_CLOSED = 131047   # "Re-engagement message": more than 24h since the customer wrote
_BAD_TOKEN = 190          # expired or revoked access token

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=35.0))
    return _client


def _graph(path: str) -> str:
    return f"https://graph.facebook.com/{settings.whatsapp_graph_version}/{path}"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.whatsapp_access_token or ''}"}


def _error_code(resp: httpx.Response) -> int | None:
    try:
        return int(resp.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return None


async def _post(payload: dict) -> Delivery:
    return (await _post_reply(payload))[0]


async def _post_reply(payload: dict) -> tuple[Delivery, dict]:
    """The send, and Meta's answer when it took it."""
    url = _graph(f"{settings.whatsapp_phone_number_id}/messages")
    try:
        resp = await _http().post(url, json={"messaging_product": "whatsapp", **payload},
                                  headers=_auth())
    except httpx.HTTPError as exc:
        logger.error("WhatsApp send transport error: %s", type(exc).__name__)
        return "failed", {}
    if resp.is_success:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return "ok", body if isinstance(body, dict) else {}
    return _refused(resp), {}


def _refused(resp: httpx.Response) -> Delivery:
    code = _error_code(resp)
    if code == _WINDOW_CLOSED:
        logger.info("WhatsApp refused a free-form message: the 24h window is closed")
        return "window_closed"
    if code == _BAD_TOKEN:
        logger.error("WhatsApp rejected the access token (code 190): renew WHATSAPP_ACCESS_TOKEN")
    else:
        logger.error("WhatsApp send returned status %s, error code %s", resp.status_code, code)
    return "failed"


def _to(to: str) -> dict:
    return {"recipient_type": "individual", "to": to}


async def send_text(to: str, text: str) -> Delivery:
    return await _post({**_to(to), "type": "text",
                        "text": {"body": text, "preview_url": True}})


async def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]) -> Delivery:
    """Up to three reply buttons, as (title, id)."""
    return await _post({**_to(to), "type": "interactive", "interactive": {
        "type": "button", "body": {"text": body},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": ident, "title": title}} for title, ident in buttons
        ]},
    }})


async def send_list(to: str, body: str, button_label: str,
                    rows: list[tuple[str, str] | tuple[str, str, str]]) -> Delivery:
    """Up to ten rows behind one button, as (title, id) or (title, id,
    description)."""
    def row(entry) -> dict:
        title, ident, *rest = entry
        return {"id": ident, "title": title, **({"description": rest[0]} if rest else {})}

    return await _post({**_to(to), "type": "interactive", "interactive": {
        "type": "list", "body": {"text": body},
        "action": {"button": button_label, "sections": [{"rows": [row(r) for r in rows]}]},
    }})


async def send_flow(to: str, body: str, *, flow_id: str, token: str, cta: str, screen: str,
                    data: dict[str, Any]) -> Delivery:
    """A WhatsApp Flow (a form built in Meta's Flow Builder) as an interactive
    message. `token` comes back with the answer, to tell which request it is
    for; `data` fills the first screen (Meta wants a non-empty object or
    nothing). Inside the 24h window, like any message that is not a template."""
    parameters: dict = {
        "flow_message_version": "3", "flow_token": token, "flow_id": flow_id,
        "flow_cta": cta, "flow_action": "navigate",
        "flow_action_payload": {"screen": screen, **({"data": data} if data else {})},
    }
    return await _post({**_to(to), "type": "interactive", "interactive": {
        "type": "flow", "body": {"text": body},
        "action": {"name": "flow", "parameters": parameters},
    }})


async def upload_media(data: bytes, mime: str, filename: str) -> str | None:
    """Store a file with Meta, privately: the id a message then points to.
    None when it was not taken; nothing reaches the customer either way."""
    try:
        uploaded = await _http().post(
            _graph(f"{settings.whatsapp_phone_number_id}/media"), headers=_auth(),
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (filename, data, mime)},
        )
        media_id = uploaded.json().get("id") if uploaded.is_success else None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return None
    return media_id if isinstance(media_id, str) and media_id else None


async def _post_once(payload: dict) -> str:
    """A send that must not be repeated blindly: "uncertain" when the answer
    was lost, since Meta may have taken it."""
    try:
        resp = await _http().post(_graph(f"{settings.whatsapp_phone_number_id}/messages"),
                                  json={"messaging_product": "whatsapp", **payload},
                                  headers=_auth())
    except httpx.HTTPError as exc:
        logger.error("WhatsApp send outcome uncertain: %s", type(exc).__name__)
        return "uncertain"
    if resp.status_code >= 500:
        # Meta, or a proxy before it, may have taken it before failing.
        logger.error("WhatsApp send outcome uncertain: status %s", resp.status_code)
        return "uncertain"
    return "ok" if resp.is_success else _refused(resp)


async def send_document_buttons(to: str, media_id: str, filename: str, body: str,
                                buttons: list[tuple[str, str]]) -> str:
    """A document with text and up to three reply buttons, in one message.
    Free-form: only within the 24h window. "ok", "failed", "window_closed"
    or "uncertain"."""
    return await _post_once({**_to(to), "type": "interactive", "interactive": {
        "type": "button",
        "header": {"type": "document", "document": {"id": media_id, "filename": filename}},
        "body": {"text": body},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": ident, "title": title}} for title, ident in buttons
        ]},
    }})


async def send_document_template(to: str, name: str, language: str, media_id: str,
                                 filename: str, body_params: list[str],
                                 button_payloads: list[str]) -> str:
    """An approved template whose header is a document: past the window too.
    "ok", "failed" or "uncertain"."""
    payload = _template(to, name, language, body_params, button_payloads)
    payload["template"]["components"].insert(0, {"type": "header", "parameters": [
        {"type": "document", "document": {"id": media_id, "filename": filename}}]})
    return await _post_once(payload)


def _template(to: str, name: str, language: str, body_params: list[str],
              button_payloads: list[str]) -> dict:
    components: list[dict] = [{"type": "body", "parameters": [
        {"type": "text", "text": value} for value in body_params
    ]}]
    components += [
        {"type": "button", "sub_type": "quick_reply", "index": str(i),
         "parameters": [{"type": "payload", "payload": payload}]}
        for i, payload in enumerate(button_payloads)
    ]
    return {**_to(to), "type": "template", "template": {
        "name": name, "language": {"code": language}, "components": components,
    }}


async def send_template(to: str, name: str, language: str, body_params: list[str],
                        button_payloads: list[str]) -> Delivery:
    """An approved template: the body's {{n}} in order, and one payload per
    quick-reply button, in the template's button order."""
    return await _post(_template(to, name, language, body_params, button_payloads))


async def send_first_template(to: str, name: str, language: str, body_params: list[str],
                              button_payloads: list[str]) -> tuple[Delivery, str | None]:
    """The same, to someone who never wrote, with the id Meta knows them by:
    a Brazilian mobile typed with the ninth digit may be registered without
    it, and replies come from that id."""
    delivery, body = await _post_reply(_template(to, name, language, body_params,
                                                 button_payloads))
    try:
        wa_id = str(body["contacts"][0]["wa_id"]) or None
    except (KeyError, IndexError, TypeError):
        wa_id = None
    return delivery, wa_id


async def mark_read_typing(message_id: str) -> bool:
    """Blue ticks on what arrived, and "digitando…" until the answer goes out."""
    return await _post({"status": "read", "message_id": message_id,
                        "typing_indicator": {"type": "text"}}) == "ok"


async def fetch_media(media_id: str, max_bytes: int) -> Download:
    """Two calls: Meta first says where the file is (and its size), then the
    file itself, which also needs the token. A declared size over the cap is
    not downloaded at all."""
    try:
        lookup = await _http().get(_graph(media_id), headers=_auth())
        if not lookup.is_success:
            logger.error("WhatsApp media lookup returned status %s", lookup.status_code)
            return Failed(f"status {lookup.status_code}")
        info = lookup.json()
        over = declared_over(info.get("file_size"), max_bytes)
        if over is not None:
            return TooLarge(over)
        found = await fetch_capped(_http(), info["url"], headers=_auth(), max_bytes=max_bytes)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.error("Failed to download WhatsApp media: %s", type(exc).__name__)
        return Failed(type(exc).__name__)
    if isinstance(found, Downloaded):
        return Downloaded(found.data, info.get("mime_type") or found.mime)
    return found


async def download_media(media_id: str, max_bytes: int) -> tuple[bytes, str] | None:
    """The media's bytes and type, or None (the voice-note path)."""
    found = await fetch_media(media_id, max_bytes)
    if isinstance(found, Downloaded):
        return found.data, found.mime or ""
    if isinstance(found, TooLarge):
        logger.warning("WhatsApp media too large, rejected")
    return None


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    """Meta signs every webhook with the app secret over the exact bytes sent."""
    secret = settings.whatsapp_app_secret
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # Compared as bytes: a str with accents makes compare_digest raise.
    return hmac.compare_digest(header.removeprefix("sha256=").encode(), expected.encode())


async def close() -> None:
    """Release the shared HTTP client on shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def send_attachment(
    to: str, data: bytes, kind: str, mime: str, filename: str, caption: str = ""
) -> str:
    """Upload privately, then send by media id. A lost message response is uncertain."""
    if kind not in ("image", "audio", "document"):
        return "failed"
    try:
        uploaded = await _http().post(
            _graph(f"{settings.whatsapp_phone_number_id}/media"),
            headers=_auth(),
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (filename, data, mime)},
        )
        if not uploaded.is_success:
            return "failed"
        media_id = uploaded.json().get("id")
        if not isinstance(media_id, str) or not media_id:
            return "failed"
    except (httpx.HTTPError, ValueError, TypeError):
        return "failed"  # upload alone never sends a message to a customer
    body = {"id": media_id}
    if kind == "document":
        body["filename"] = filename
    if caption and kind != "audio":
        body["caption"] = caption
    try:
        response = await _http().post(
            _graph(f"{settings.whatsapp_phone_number_id}/messages"),
            headers=_auth(),
            json={"messaging_product": "whatsapp", **_to(to), "type": kind, kind: body},
        )
    except httpx.HTTPError:
        return "uncertain"
    if response.is_success:
        try:
            return "ok" if response.json().get("messages", [{}])[0].get("id") else "uncertain"
        except (ValueError, IndexError, TypeError):
            return "uncertain"
    if _error_code(response) == _WINDOW_CLOSED:
        return "window_closed"
    return "uncertain" if response.status_code >= 500 else "failed"
