"""Meta's webhook, read into the update shape the handlers already process.

A WhatsApp text is a Telegram-shaped message, a tapped button a callback query
— so booking, cancelling and the Inbox run the same code on both channels.
Pure functions: nothing here talks to Meta.
"""
from __future__ import annotations

import json

from core import content
from core.channels import WA_PREFIX
from core.config import settings
from core.integrations.whatsapp_events import preview

_MEDIA_PREFIX = "wa-media:"
_MEDIA_KINDS = ("image", "document", "video", "audio", "sticker")


def _values(payload: dict):
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value")
            if isinstance(value, dict):
                yield value


def _tap(update_id: str, base: dict, data: str, label: str, date: int | None) -> dict:
    return {"update_id": update_id, "callback_query": {
        "id": update_id, "_acked": True, "data": data, "from": base["from"], "date": date,
        "message": {"chat": base["chat"], "reply_markup": {"inline_keyboard": [[
            {"text": label, "callback_data": data}]]}},
    }}


def _flow_answer(body: dict) -> tuple[str, dict] | None:
    """A finished WhatsApp Flow: its token (ours, set when it was sent) and the
    answers. None when it cannot be read, so the team sees it as an event."""
    raw = (body.get("nfm_reply") or {}).get("response_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    token = raw.get("flow_token")
    if not isinstance(token, str) or not token:
        return None
    return token, {key: value for key, value in raw.items() if key != "flow_token"}


def _convert(item: dict, names: dict[str, str | None]) -> dict | None:
    mid, sender, kind = item.get("id"), item.get("from"), item.get("type")
    if not mid or not sender or not isinstance(kind, str) or not kind:
        return None
    update_id = f"{WA_PREFIX}{mid}"
    base = {"chat": {"id": f"{WA_PREFIX}{sender}", "type": "private"},
            "from": {"first_name": names.get(str(sender))}}
    body = item.get(kind) or {}
    # When the person wrote, by Meta's clock — like Telegram's message.date.
    date = int(item["timestamp"]) if str(item.get("timestamp") or "").isdigit() else None

    if kind == "interactive":
        answered = _flow_answer(body)
        if answered is not None:
            token, answers = answered
            update = _tap(update_id, base, f"flow:{token}", content.text("forms.sent_label"),
                          date)
            update["callback_query"]["_flow"] = answers
            return update
        reply = body.get("button_reply") or body.get("list_reply") or {}
        if reply.get("id"):
            return _tap(update_id, base, reply["id"], reply.get("title") or reply["id"], date)
    if kind == "button" and body.get("payload"):
        return _tap(update_id, base, body["payload"], body.get("text") or body["payload"], date)

    message = {"message_id": mid, "_wa_message_id": mid, "date": date, **base}
    if kind == "text":
        message["text"] = body.get("body") or ""
    elif kind == "audio" and body.get("id"):
        message["voice"] = {"file_id": f"{_MEDIA_PREFIX}{body['id']}",
                            "mime_type": body.get("mime_type")}
    else:
        message["_wa_type"] = kind
    if settings.media_enabled and kind in _MEDIA_KINDS and body.get("id"):
        # Kept for the Inbox (core/services/media.py). Only with capture on:
        # off, the update is exactly what it was before, caption included —
        # a caption reaching the handler would set the bot answering a photo.
        message["_media"] = {
            "kind": kind, "file_id": f"{_MEDIA_PREFIX}{body['id']}",
            "mime_type": body.get("mime_type"), "filename": body.get("filename"),
            "declared_bytes": None, "source_id": f"{update_id}:{body['id']}",
        }
        if body.get("caption"):
            message["caption"] = body["caption"]
    if kind not in ('text', 'audio', *_MEDIA_KINDS) or (kind in _MEDIA_KINDS and not body.get('id')):
        message['_wa_event'] = preview(kind, body, item.get('errors'))
    return {"update_id": update_id, "message": message}


def to_updates(payload: dict, phone_number_id: str) -> list[dict]:
    """Every customer message in the webhook, in order, as an update. A
    message to another number on the same account is not this desk's."""
    updates: list[dict] = []
    for value in _values(payload):
        if (value.get("metadata") or {}).get("phone_number_id") != phone_number_id:
            continue
        names: dict[str, str | None] = {
            str(c.get("wa_id")): (c.get("profile") or {}).get("name")
            for c in value.get("contacts") or []
        }
        for item in value.get("messages") or []:
            update = _convert(item, names)
            if update is not None:
                updates.append(update)
    return updates


def delivery_failures(payload: dict) -> list[tuple[str, int | None]]:
    """The messages Meta could not deliver, with the reason's code."""
    failed: list[tuple[str, int | None]] = []
    for value in _values(payload):
        for status in value.get("statuses") or []:
            if status.get("status") == "failed":
                errors = status.get("errors") or [{}]
                failed.append((status.get("id") or "", errors[0].get("code")))
    return failed
