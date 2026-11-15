"""Multipart upload and channel refusal/uncertainty against synthetic HTTP."""

import json

import httpx
import pytest

from core.config import settings
from core.integrations import telegram, whatsapp


@pytest.mark.parametrize(
    "kind,mime", [("image", "image/jpeg"), ("audio", "audio/ogg"), ("document", "application/pdf")]
)
async def test_whatsapp_uploads_bytes_privately_then_sends_by_id(monkeypatch, kind, mime):
    requests = []

    async def serve(request):
        requests.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "media-test"})
        return httpx.Response(200, json={"messages": [{"id": "message-test"}]})

    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "TEST-PN")
    monkeypatch.setattr(settings, "whatsapp_access_token", "synthetic-only")
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        monkeypatch.setattr(whatsapp, "_http", lambda: client)
        assert (
            await whatsapp.send_attachment(
                "15550001111",
                b"synthetic-bytes",
                kind,
                mime,
                "test.dat",
                "" if kind == "audio" else "<text>",
            )
            == "ok"
        )
    assert len(requests) == 2 and requests[0].url.path.endswith("/TEST-PN/media")
    assert b"synthetic-bytes" in requests[0].content and mime.encode() in requests[0].content
    assert requests[0].headers["authorization"] == "Bearer synthetic-only"
    body = json.loads(requests[1].content)
    assert body[kind]["id"] == "media-test" and "link" not in body[kind]
    assert body["to"] == "15550001111"
    if kind == "audio":
        assert "caption" not in body[kind]


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("upload-timeout", "failed"),
        ("send-timeout", "uncertain"),
        ("no-id", "uncertain"),
        ("window", "window_closed"),
        ("server-error", "uncertain"),
    ],
)
async def test_wa_does_not_confuse_upload_failure_or_ambiguous_send_with_delivery(
    monkeypatch, outcome, expected
):
    requests = []

    async def serve(request):
        requests.append(request)
        if request.url.path.endswith("/media"):
            if outcome == "upload-timeout":
                raise httpx.ReadTimeout("synthetic")
            return httpx.Response(200, json={"id": "media-test"})
        if outcome == "send-timeout":
            raise httpx.ReadTimeout("synthetic")
        if outcome == "window":
            return httpx.Response(400, json={"error": {"code": 131047}})
        if outcome == "server-error":
            return httpx.Response(503, json={"error": {"code": 1}})
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        monkeypatch.setattr(whatsapp, "_http", lambda: client)
        assert (
            await whatsapp.send_attachment(
                "15550001111", b"file", "document", "text/plain", "a.txt"
            )
            == expected
        )
    assert len(requests) == (1 if outcome == "upload-timeout" else 2)


@pytest.mark.parametrize(
    "kind,method,field",
    [
        ("image", "sendPhoto", "photo"),
        ("audio", "sendVoice", "voice"),
        ("document", "sendDocument", "document"),
    ],
)
async def test_telegram_uses_the_native_media_method(monkeypatch, kind, method, field):
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        monkeypatch.setattr(telegram, "_http", lambda: client)
        assert (
            await telegram.send_attachment(
                "1234", b"synthetic", kind, "application/octet-stream", "test.dat"
            )
            == "ok"
        )
    assert (
        requests[0].url.path.endswith("/" + method)
        and f'name="{field}"'.encode() in requests[0].content
    )
