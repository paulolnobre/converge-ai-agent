"""Sending a push: encrypted for the device, signed with our VAPID key, posted
to the push service without following redirects, in the background, at most
four at a time. The result is recorded on the subscription."""
import asyncio
import base64
import json

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from core.config import settings
from core.repositories import push as push_repo
from core.services import push


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _vapid_keys() -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    return _b64(public), _b64(private)


class Device:
    """A browser's subscription, with the private key to read what it gets."""

    def __init__(self, n: int = 1):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = b"0123456789abcdef"
        self.sub = {"id": n, "endpoint": f"https://fcm.googleapis.com/fcm/send/device-{n}",
                    "p256dh": _b64(self.key.public_key().public_bytes(
                        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)),
                    "auth": _b64(self.auth)}

    def read(self, body: bytes) -> dict:
        return json.loads(http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth,
                                           version="aes128gcm"))


@pytest.fixture
def results(monkeypatch):
    """Configured keys, and the repository calls the sender makes, in memory."""
    public, private = _vapid_keys()
    monkeypatch.setattr(settings, "push_vapid_public_key", public)
    monkeypatch.setattr(settings, "push_vapid_private_key", private)
    monkeypatch.setattr(settings, "push_vapid_subject", "mailto:team@getconverge.me")
    calls: list[tuple[str, int]] = []

    class Pool:
        def connection(self):
            class Conn:
                async def __aenter__(self):
                    return object()

                async def __aexit__(self, *exc):
                    return False
            return Conn()

    async def get_pool():
        return Pool()

    for name in ("mark_ok", "mark_failed", "delete"):
        async def record(conn, sub_id, name=name):
            calls.append((name, sub_id))
        monkeypatch.setattr(push_repo, name, record)
    monkeypatch.setattr(push, "get_pool", get_pool)
    calls_public = public
    return calls, calls_public


def _service(monkeypatch, handler):
    monkeypatch.setattr(push, "_transport", httpx.MockTransport(handler))


async def _settled():
    while push._pending or push._lookups:
        await asyncio.gather(*list(push._pending), *list(push._lookups), return_exceptions=True)


async def test_the_device_reads_the_notice_and_the_service_sees_our_signature(monkeypatch, results):
    calls, public = results
    device, seen = Device(), []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201)

    _service(monkeypatch, handler)
    push.schedule([device.sub], title="Nova mensagem de Ana.", url="/painel/#inbox/7", tag="inbox-7")
    await _settled()
    (request,) = seen
    assert device.read(request.content) == {"title": "Nova mensagem de Ana.",
                                            "url": "/painel/#inbox/7", "tag": "inbox-7"}
    assert request.headers["ttl"] == "3600" and request.headers["urgency"] == "high"
    assert request.headers["content-encoding"] == "aes128gcm"
    assert request.headers["authorization"].startswith("vapid t=")
    assert f"k={public}" in request.headers["authorization"]
    assert calls == [("mark_ok", 1)]


@pytest.mark.parametrize("status,expected", [(404, "delete"), (410, "delete"),
                                             (500, "mark_failed"), (301, "mark_failed")])
async def test_the_answer_is_recorded_on_the_subscription(monkeypatch, results, status, expected):
    calls, _ = results
    _service(monkeypatch, lambda request: httpx.Response(
        status, headers={"location": "https://evil.example/x"} if status == 301 else {}))
    push.schedule([Device().sub], title="t", url="/painel/", tag="t")
    await _settled()
    assert calls == [(expected, 1)]


async def test_a_redirect_is_never_followed(monkeypatch, results):
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    _service(monkeypatch, handler)
    push.schedule([Device().sub], title="t", url="/painel/", tag="t")
    await _settled()
    assert hosts == ["fcm.googleapis.com"]


async def test_a_service_that_errors_counts_as_a_failure(monkeypatch, results):
    calls, _ = results

    def handler(request):
        raise httpx.ConnectTimeout("slow")

    _service(monkeypatch, handler)
    push.schedule([Device().sub], title="t", url="/painel/", tag="t")
    await _settled()
    assert calls == [("mark_failed", 1)]


async def test_an_endpoint_that_is_no_longer_valid_is_not_contacted(monkeypatch, results):
    calls, _ = results
    contacted = []
    _service(monkeypatch, lambda request: contacted.append(request) or httpx.Response(201))
    sub = {**Device().sub, "endpoint": "https://evil.example/x"}
    push.schedule([sub], title="t", url="/painel/", tag="t")
    await _settled()
    assert contacted == [] and calls == [("delete", 1)]


async def test_at_most_four_are_sent_at_once(monkeypatch, results):
    active, peak, release = 0, 0, asyncio.Event()

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1
        return httpx.Response(201)

    _service(monkeypatch, handler)
    push.schedule([Device(n).sub for n in range(1, 7)], title="t", url="/painel/", tag="t")
    await asyncio.sleep(0.2)
    assert peak == 4
    release.set()
    await _settled()
    assert peak == 4


async def test_schedule_returns_before_the_service_answers(monkeypatch, results):
    answered = asyncio.Event()

    async def handler(request):
        await asyncio.sleep(0.3)
        answered.set()
        return httpx.Response(201)

    _service(monkeypatch, handler)
    push.schedule([Device().sub], title="t", url="/painel/", tag="t")
    assert not answered.is_set() and push._pending
    await _settled()
    assert answered.is_set()


async def test_a_full_queue_drops_the_excess(monkeypatch, results):
    release = asyncio.Event()

    async def handler(request):
        await release.wait()
        return httpx.Response(201)

    _service(monkeypatch, handler)
    push.schedule([Device(n).sub for n in range(1, push.MAX_PENDING + 6)], title="t",
                  url="/painel/", tag="t")
    assert len(push._pending) == push.MAX_PENDING
    release.set()
    await _settled()


async def test_without_keys_nothing_is_read_or_sent(monkeypatch):
    monkeypatch.setattr(settings, "push_vapid_private_key", "")

    async def active_for_user(*args, **kwargs):
        raise AssertionError("not configured: no query")

    monkeypatch.setattr(push_repo, "active_for_user", active_for_user)
    assert await push.recipients(object(), 5) == []
    push.schedule([Device().sub], title="t", url="/painel/", tag="t")
    assert not push._pending


async def test_ten_seconds_is_the_whole_budget(monkeypatch, results):
    calls, _ = results
    monkeypatch.setattr(push, "TIMEOUT_SECONDS", 0.2)

    async def handler(request):
        await asyncio.sleep(5)  # a service that never finishes answering
        return httpx.Response(201)

    _service(monkeypatch, handler)
    push.schedule([Device().sub], title="t", url="/painel/", tag="t")
    await asyncio.wait_for(_settled(), 2)
    assert calls == [("mark_failed", 1)]


async def test_the_answer_body_is_never_read(monkeypatch, results):
    calls, _ = results

    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.05)
                yield b"x"

    _service(monkeypatch, lambda request: httpx.Response(201, stream=Trickle()))
    push.schedule([Device().sub], title="t", url="/painel/", tag="t")
    await asyncio.wait_for(_settled(), 2)
    assert calls == [("mark_ok", 1)]


async def test_the_team_notice_reads_and_sends_away_from_the_turn(monkeypatch, results):
    calls, _ = results
    devices, sent = [Device(1), Device(2)], []

    async def active_for_all(conn, **limits):
        return [d.sub for d in devices]

    monkeypatch.setattr(push_repo, "active_for_all", active_for_all)

    def handler(request):
        sent.append(request.url.path)
        return httpx.Response(201)

    _service(monkeypatch, handler)
    push.schedule_team(title="💬 Nova conversa: Ana · WhatsApp", url="/painel/#inbox/7", tag="inbox-7")
    assert sent == []  # nothing read or sent inside the call
    await _settled()
    assert sorted(sent) == ["/fcm/send/device-1", "/fcm/send/device-2"]
    assert sorted(calls) == [("mark_ok", 1), ("mark_ok", 2)]


async def test_a_failing_team_lookup_is_only_logged(monkeypatch, results):
    async def active_for_all(conn, **limits):
        raise RuntimeError("database down")

    monkeypatch.setattr(push_repo, "active_for_all", active_for_all)
    push.schedule_team(title="t", url="/painel/", tag="t")
    await _settled()


async def test_no_team_notice_without_keys(monkeypatch):
    monkeypatch.setattr(settings, "push_vapid_private_key", "")
    push.schedule_team(title="t", url="/painel/", tag="t")
    assert not push._pending


async def test_nothing_about_the_teams_own_chat(monkeypatch, results):
    calls, _ = results
    looked: list[str] = []

    async def is_team_chat(conn, chat_id):
        looked.append(chat_id)
        return chat_id == "team-chat"

    async def active_for_all(conn, **limits):
        return [Device().sub]

    monkeypatch.setattr(push_repo, "is_team_chat", is_team_chat)
    monkeypatch.setattr(push_repo, "active_for_all", active_for_all)
    _service(monkeypatch, lambda request: httpx.Response(201))
    push.schedule_team(title="t", url="/painel/", tag="t", chat_id="team-chat")
    await _settled()
    assert calls == [] and looked == ["team-chat"]
    push.schedule_team(title="t", url="/painel/", tag="t", chat_id="customer")
    await _settled()
    assert calls == [("mark_ok", 1)]


async def test_many_team_notices_at_once_still_send(monkeypatch, results):
    calls, _ = results

    async def active_for_all(conn, **limits):
        await asyncio.sleep(0.05)
        return [Device().sub]

    monkeypatch.setattr(push_repo, "active_for_all", active_for_all)
    _service(monkeypatch, lambda request: httpx.Response(201))
    for _ in range(push.MAX_PENDING + 10):
        push.schedule_team(title="t", url="/painel/", tag="t")
    await asyncio.wait_for(_settled(), 5)
    assert 0 < len(calls) <= push.MAX_LOOKUPS
