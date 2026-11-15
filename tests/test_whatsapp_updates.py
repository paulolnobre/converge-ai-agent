"""What Meta's webhook says becomes the update the handlers already read:
text, voice, a tapped button — so the conversation code is the same code."""
import json

import pytest

from core import content
from core.config import settings
from core.integrations.whatsapp_updates import delivery_failures, to_updates

NUMBER = "5591999990000"
CHAT = {"id": "wa:5591999990000", "type": "private"}
FROM = {"first_name": "Ana Souza"}


def payload(*messages: dict, phone_number_id: str = "PN1", statuses: list | None = None,
            wa_id: str = NUMBER) -> dict:
    value: dict = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "559192456225", "phone_number_id": phone_number_id},
        "contacts": [{"profile": {"name": "Ana Souza"}, "wa_id": wa_id}],
    }
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": value}]}]}


def message(mid: str, kind: str, body: dict, sender: str = NUMBER) -> dict:
    return {"from": sender, "id": mid, "timestamp": "1759780000", "type": kind, kind: body}


def test_text_becomes_a_message():
    assert to_updates(payload(message("wamid.1", "text", {"body": "oi"})), "PN1") == [{
        "update_id": "wa:wamid.1",
        "message": {"message_id": "wamid.1", "_wa_message_id": "wamid.1", "date": 1759780000,
                    "chat": CHAT, "from": FROM, "text": "oi"},
    }]


def test_audio_becomes_a_voice_note():
    audio = {"id": "m1", "mime_type": "audio/ogg; codecs=opus", "voice": True}

    update, = to_updates(payload(message("wamid.2", "audio", audio)), "PN1")

    assert update["message"]["voice"] == {"file_id": "wa-media:m1",
                                          "mime_type": "audio/ogg; codecs=opus"}
    assert "text" not in update["message"]


def _tap(update: dict) -> dict:
    return update["callback_query"]


def test_a_reply_button_becomes_a_tap():
    reply = {"type": "button_reply",
             "button_reply": {"id": "period:any:none:none", "title": "Tanto faz"}}

    update, = to_updates(payload(message("wamid.3", "interactive", reply)), "PN1")

    assert update["update_id"] == "wa:wamid.3"
    assert _tap(update) == {
        "id": "wa:wamid.3", "_acked": True, "data": "period:any:none:none", "from": FROM,
        "date": 1759780000,
        "message": {"chat": CHAT, "reply_markup": {"inline_keyboard": [[
            {"text": "Tanto faz", "callback_data": "period:any:none:none"}]]}},
    }


def test_a_list_choice_becomes_a_tap():
    reply = {"type": "list_reply", "list_reply": {"id": "appt:move:9", "title": "qui 09/10 10:00"}}

    update, = to_updates(payload(message("wamid.4", "interactive", reply)), "PN1")

    assert _tap(update)["data"] == "appt:move:9"
    assert _tap(update)["message"]["reply_markup"]["inline_keyboard"][0][0]["text"] == (
        "qui 09/10 10:00"
    )


def test_a_template_button_becomes_a_tap():
    button = {"payload": "appt:ok:7", "text": "Confirmo"}

    update, = to_updates(payload(message("wamid.5", "button", button)), "PN1")

    assert _tap(update)["data"] == "appt:ok:7"
    assert _tap(update)["message"]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Confirmo"


def test_what_the_desk_cannot_read_arrives_without_text():
    for kind in ("image", "document", "sticker", "location", "contacts", "video"):
        update, = to_updates(payload(message(f"wamid.{kind}", kind, {"id": "x"})), "PN1")
        assert update["message"]["_wa_type"] == kind
        assert "text" not in update["message"] and "voice" not in update["message"]


def test_a_batch_becomes_one_update_per_message_in_order():
    first = payload(message("wamid.a", "text", {"body": "1"}),
                    message("wamid.b", "text", {"body": "2"}))
    second = payload(message("wamid.c", "text", {"body": "3"}))
    first["entry"].append(second["entry"][0])

    assert [u["message"]["text"] for u in to_updates(first, "PN1")] == ["1", "2", "3"]


def test_another_phone_number_is_ignored():
    assert to_updates(payload(message("wamid.1", "text", {"body": "oi"}),
                              phone_number_id="OTHER"), "PN1") == []


def test_statuses_alone_queue_nothing():
    statuses = [
        {"id": "wamid.8", "status": "delivered", "recipient_id": NUMBER},
        {"id": "wamid.9", "status": "failed", "recipient_id": NUMBER,
         "errors": [{"code": 131026, "title": "Message undeliverable"}]},
    ]
    sent = payload(statuses=statuses)

    assert to_updates(sent, "PN1") == []
    assert delivery_failures(sent) == [("wamid.9", 131026)]


def test_a_fifteen_digit_number_fits():
    long_number = "123456789012345"
    sent = payload(message("wamid.1", "text", {"body": "oi"}, sender=long_number),
                   wa_id=long_number)

    chat_id = to_updates(sent, "PN1")[0]["message"]["chat"]["id"]

    assert chat_id == "wa:123456789012345" and len(chat_id) <= 20


# ── Media (MEDIA_ENABLED) ────────────────────────────────────────────────────

_MEDIA_BODIES = {
    "image": {"id": "M1", "mime_type": "image/jpeg", "caption": "meu exame", "sha256": "x"},
    "document": {"id": "M1", "mime_type": "application/pdf", "filename": "exame.pdf",
                 "caption": "segue"},
    "video": {"id": "M1", "mime_type": "video/mp4", "caption": "olha"},
    "audio": {"id": "M1", "mime_type": "audio/ogg; codecs=opus", "voice": True},
    "sticker": {"id": "M1", "mime_type": "image/webp", "animated": False},
}
_BASE = {"message_id": "WAMID", "_wa_message_id": "WAMID", "date": 1759780000,
         "chat": CHAT, "from": FROM}
# What to_updates returned for these before media capture existed, recorded literally.
_TODAY = {
    "image": {**_BASE, "_wa_type": "image"},
    "document": {**_BASE, "_wa_type": "document"},
    "video": {**_BASE, "_wa_type": "video"},
    "audio": {**_BASE, "voice": {"file_id": "wa-media:M1", "mime_type": "audio/ogg; codecs=opus"}},
    "sticker": {**_BASE, "_wa_type": "sticker"},
}


@pytest.mark.parametrize("kind", list(_MEDIA_BODIES))
def test_with_capture_off_the_update_is_exactly_today(monkeypatch, kind):
    monkeypatch.setattr(settings, "media_enabled", False)
    update, = to_updates(payload(message("WAMID", kind, _MEDIA_BODIES[kind])), "PN1")
    assert update == {"update_id": "wa:WAMID", "message": _TODAY[kind]}


@pytest.mark.parametrize("kind", list(_MEDIA_BODIES))
def test_media_is_carried_when_capture_is_on(monkeypatch, kind):
    monkeypatch.setattr(settings, "media_enabled", True)
    body = _MEDIA_BODIES[kind]
    update, = to_updates(payload(message("WAMID", kind, body)), "PN1")
    msg = update["message"]
    assert msg["_media"] == {
        "kind": kind, "file_id": "wa-media:M1", "mime_type": body["mime_type"],
        "filename": body.get("filename"), "declared_bytes": None, "source_id": "wa:WAMID:M1",
    }
    assert msg.get("caption") == body.get("caption")
    if kind == "audio":
        assert msg["voice"] == _TODAY["audio"]["voice"]


# ── A WhatsApp Flow, answered ───────────────────────────────────────────────

def _flow_reply(response, name: str = "flow") -> dict:
    return {"type": "nfm_reply", "nfm_reply": {"name": name, "body": "Sent",
                                               "response_json": response}}


def test_a_completed_flow_becomes_a_tap_with_its_answers():
    answers = json.dumps({"flow_token": "proposta:7", "quem": "Ana", "areas": ["atendimento", "vendas"],
                          "email": "ana@exemplo.com.br"})

    update, = to_updates(payload(message("wamid.9", "interactive", _flow_reply(answers))), "PN1")

    tap = _tap(update)
    assert update["update_id"] == "wa:wamid.9"
    assert tap["data"] == "flow:proposta:7" and tap["_acked"] is True
    assert tap["_flow"] == {"quem": "Ana", "areas": ["atendimento", "vendas"],
                            "email": "ana@exemplo.com.br"}
    assert tap["message"]["chat"] == CHAT and tap["from"] == FROM
    label = tap["message"]["reply_markup"]["inline_keyboard"][0][0]
    assert label == {"text": content.text("forms.sent_label"), "callback_data": "flow:proposta:7"}


def test_an_already_parsed_response_is_read_too():
    reply = _flow_reply({"flow_token": "agendar:3", "horario": "2"})
    update, = to_updates(payload(message("wamid.10", "interactive", reply)), "PN1")
    assert _tap(update)["data"] == "flow:agendar:3" and _tap(update)["_flow"] == {"horario": "2"}


@pytest.mark.parametrize("response", [
    json.dumps({"quem": "Ana"}),            # no token
    json.dumps({"flow_token": "", "quem": "Ana"}),
    json.dumps({"flow_token": 7, "quem": "Ana"}),
    "{ isso não é json",
    json.dumps(["a", "b"]),
    None,
])
def test_a_flow_answer_that_cannot_be_read_is_an_event_for_the_team(response):
    update, = to_updates(payload(message("wamid.11", "interactive", _flow_reply(response))),
                         "PN1")

    assert "callback_query" not in update
    assert update["message"]["_wa_event"].startswith("Evento recebido pelo WhatsApp")
