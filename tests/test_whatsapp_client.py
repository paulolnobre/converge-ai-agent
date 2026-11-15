"""The WhatsApp Cloud API client: what goes to Meta, and how Meta's answers
read back — a closed 24h window told apart from any other refusal."""
import hashlib
import hmac
import json
import logging

import httpx
import pytest

from core.config import settings
from core.integrations import whatsapp

TO = "5591999990000"
MESSAGES = "https://graph.facebook.com/v23.0/PN1/messages"


class Meta:
    """A fake Graph API: records every request, answers what the test set."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: dict = {"messages": [{"id": "wamid.out"}]}
        self.media_bytes = b"OGG"
        self.raise_error: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_error is not None:
            raise self.raise_error
        if request.url.host == "lookaside.example":
            return httpx.Response(200, content=self.media_bytes)
        if request.method == "GET":
            return httpx.Response(200, json={"url": "https://lookaside.example/m1",
                                             "mime_type": "audio/ogg"})
        return httpx.Response(self.status, json=self.body)

    def sent(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


@pytest.fixture
def meta(monkeypatch: pytest.MonkeyPatch):
    fake = Meta()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    monkeypatch.setattr(whatsapp, "_http", lambda: client)
    monkeypatch.setattr(settings, "whatsapp_access_token", "TOK-S3CRET-9")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "PN1")
    monkeypatch.setattr(settings, "whatsapp_app_secret", "s3cret")
    monkeypatch.setattr(settings, "whatsapp_webhook_verify_token", "vt")
    monkeypatch.setattr(settings, "whatsapp_graph_version", "v23.0")
    return fake


async def test_text_goes_to_the_phone_number_endpoint(meta):
    assert await whatsapp.send_text(TO, "oi") == "ok"

    request = meta.requests[0]
    assert str(request.url) == MESSAGES
    assert request.headers["Authorization"] == "Bearer TOK-S3CRET-9"
    assert meta.sent() == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": TO,
        "type": "text", "text": {"body": "oi", "preview_url": True},
    }


async def test_reply_buttons_payload(meta):
    assert await whatsapp.send_buttons(
        TO, "Período?", [("Manhã", "period:morning:none:none")]
    ) == "ok"

    sent = meta.sent()
    assert sent["type"] == "interactive" and sent["to"] == TO
    assert sent["interactive"] == {
        "type": "button", "body": {"text": "Período?"},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": "period:morning:none:none", "title": "Manhã"}},
        ]},
    }


async def test_list_payload(meta):
    assert await whatsapp.send_list(
        TO, "Qual conversa?", "Escolher", [("qui 09/10 10:00", "appt:move:1")]
    ) == "ok"

    interactive = meta.sent()["interactive"]
    assert interactive["type"] == "list"
    assert interactive["body"] == {"text": "Qual conversa?"}
    assert interactive["action"]["button"] == "Escolher"
    assert interactive["action"]["sections"] == [
        {"rows": [{"id": "appt:move:1", "title": "qui 09/10 10:00"}]}
    ]


async def test_template_payload(meta):
    assert await whatsapp.send_template(
        TO, "lembrete_conversa", "pt_BR",
        ["Ana", "quinta-feira, 09/10", "10:00", "https://meet.google.com/abc"],
        ["appt:ok:7", "appt:move:7", "appt:cancel:7"],
    ) == "ok"

    sent = meta.sent()
    assert sent["type"] == "template"
    assert sent["template"] == {
        "name": "lembrete_conversa",
        "language": {"code": "pt_BR"},
        "components": [
            {"type": "body", "parameters": [
                {"type": "text", "text": "Ana"},
                {"type": "text", "text": "quinta-feira, 09/10"},
                {"type": "text", "text": "10:00"},
                {"type": "text", "text": "https://meet.google.com/abc"},
            ]},
            {"type": "button", "sub_type": "quick_reply", "index": "0",
             "parameters": [{"type": "payload", "payload": "appt:ok:7"}]},
            {"type": "button", "sub_type": "quick_reply", "index": "1",
             "parameters": [{"type": "payload", "payload": "appt:move:7"}]},
            {"type": "button", "sub_type": "quick_reply", "index": "2",
             "parameters": [{"type": "payload", "payload": "appt:cancel:7"}]},
        ],
    }


async def test_window_closed_is_told_apart(meta):
    meta.status, meta.body = 400, {"error": {"code": 131047, "message": "Re-engagement message"}}
    assert await whatsapp.send_text(TO, "oi") == "window_closed"

    meta.body = {"error": {"code": 100, "message": "Invalid parameter"}}
    assert await whatsapp.send_text(TO, "oi") == "failed"

    meta.raise_error = httpx.ConnectError("down")
    assert await whatsapp.send_text(TO, "oi") == "failed"


async def test_expired_token_names_the_variable(meta, caplog):
    meta.status, meta.body = 401, {"error": {"code": 190, "message": "expired"}}

    with caplog.at_level(logging.ERROR):
        assert await whatsapp.send_text(TO, "oi") == "failed"

    assert "WHATSAPP_ACCESS_TOKEN" in caplog.text
    assert "TOK-S3CRET-9" not in caplog.text


async def test_typing_marks_read(meta):
    assert await whatsapp.mark_read_typing("wamid.1") is True

    assert str(meta.requests[0].url) == MESSAGES
    assert meta.sent() == {
        "messaging_product": "whatsapp", "status": "read", "message_id": "wamid.1",
        "typing_indicator": {"type": "text"},
    }


async def test_media_is_fetched_in_two_steps(meta):
    assert await whatsapp.download_media("m1", max_bytes=1000) == (b"OGG", "audio/ogg")

    lookup, download = meta.requests
    assert str(lookup.url) == "https://graph.facebook.com/v23.0/m1"
    assert download.url.host == "lookaside.example"
    assert download.headers["Authorization"] == "Bearer TOK-S3CRET-9"

    assert await whatsapp.download_media("m1", max_bytes=2) is None


def _signed(raw: bytes, secret: str = "s3cret") -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_signature(meta, monkeypatch: pytest.MonkeyPatch):
    raw = b'{"object":"whatsapp_business_account"}'

    assert whatsapp.verify_signature(raw, _signed(raw)) is True
    assert whatsapp.verify_signature(raw + b" ", _signed(raw)) is False
    assert whatsapp.verify_signature(raw, None) is False
    assert whatsapp.verify_signature(raw, "sha256=") is False

    monkeypatch.setattr(settings, "whatsapp_app_secret", "")
    assert whatsapp.verify_signature(raw, _signed(raw, "")) is False


def test_configured_needs_the_four(meta, monkeypatch: pytest.MonkeyPatch):
    assert settings.whatsapp_configured() is True
    for name in ("whatsapp_app_secret", "whatsapp_phone_number_id",
                 "whatsapp_access_token", "whatsapp_webhook_verify_token"):
        with monkeypatch.context() as m:
            m.setattr(settings, name, None)
            assert settings.whatsapp_configured() is False, name


def test_a_signature_with_odd_characters_is_false(meta):
    assert whatsapp.verify_signature(b"{}", "sha256=çã") is False


async def test_list_rows_carry_descriptions(meta):
    assert await whatsapp.send_list(
        TO, "Escolha o dia da conversa:", "Escolher dia",
        [("Terça, 21/10", "day:2026-10-21:-:-", "6 horários"), ("Outro", "x")],
    ) == "ok"

    rows = meta.sent()["interactive"]["action"]["sections"][0]["rows"]
    assert rows == [{"id": "day:2026-10-21:-:-", "title": "Terça, 21/10", "description": "6 horários"},
                    {"id": "x", "title": "Outro"}]


async def test_a_flow_goes_as_an_interactive_message(meta):
    sent = await whatsapp.send_flow(
        TO, "Vamos montar a sua proposta", flow_id="F1", token="proposta:7", cta="Responder",
        screen="PROPOSTA", data={"quem": "Ana", "email": ""})

    assert sent == "ok"
    body = meta.sent()
    assert body["to"] == TO and body["type"] == "interactive"
    interactive = body["interactive"]
    assert interactive["type"] == "flow" and interactive["body"] == {
        "text": "Vamos montar a sua proposta"}
    assert interactive["action"] == {"name": "flow", "parameters": {
        "flow_message_version": "3", "flow_token": "proposta:7", "flow_id": "F1",
        "flow_cta": "Responder", "flow_action": "navigate",
        "flow_action_payload": {"screen": "PROPOSTA",
                                "data": {"quem": "Ana", "email": ""}}}}


async def test_a_flow_without_data_leaves_it_out(meta):
    await whatsapp.send_flow(TO, "Oi", flow_id="F1", token="t", cta="Ir", screen="S", data={})
    payload = meta.sent()["interactive"]["action"]["parameters"]["flow_action_payload"]
    assert payload == {"screen": "S"}, "Meta wants a non-empty object, or nothing"


async def test_a_refused_flow_is_failed(meta):
    meta.status = 400
    meta.body = {"error": {"code": 131000, "message": "boom"}}
    assert await whatsapp.send_flow(
        TO, "Oi", flow_id="F1", token="t", cta="Ir", screen="S", data={}) == "failed"
