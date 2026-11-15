"""OCR in its own container: the bot sends the file over a Unix socket and
trusts nothing in the answer beyond text, pages and a flag. The container has
no network, no secrets and no credentials (deploy/docker-compose.prod.yml)."""
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from core.config import settings
from core.services import ocr, ocr_remote, ocr_server
from core.services.ocr_limits import MAX_PAGES
from core.web import panel_ocr

COMPOSE = Path(__file__).resolve().parent.parent / "deploy" / "docker-compose.prod.yml"


@pytest.fixture
def folder():
    # Short: a Unix socket path must fit in 108 bytes.
    path = tempfile.mkdtemp(prefix="ocr")
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def _listen(handler, folder: str):
    path = os.path.join(folder, "ocr.sock")
    running = await asyncio.start_unix_server(handler, path)
    return running, path


@pytest.fixture
async def server(monkeypatch, folder):
    """The real OCR server on a socket, with the OCR itself faked."""
    seen: list[bytes] = []

    async def extract(data):
        seen.append(data)
        if data == b"fail":
            raise ocr.OcrError(422, "A leitura demorou demais. Tente uma imagem menor.")
        return {"text": "texto lido", "pages": 1, "limited": False}

    monkeypatch.setattr(ocr, "extract", extract)
    running, path = await _listen(ocr_server.handle, folder)
    try:
        yield path, seen
    finally:
        running.close()
        await running.wait_closed()


async def _raw_reply(path: str, payload: bytes) -> dict:
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(payload)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    return json.loads(raw)


async def test_round_trip_returns_only_the_text(server):
    path, seen = server
    result = await ocr_remote.extract(path, b"synthetic image")
    assert result == {"text": "texto lido", "pages": 1, "limited": False}
    assert seen == [b"synthetic image"]


async def test_a_refusal_in_the_container_reaches_the_panel(server):
    path, _ = server
    with pytest.raises(ocr.OcrError) as refused:
        await ocr_remote.extract(path, b"fail")
    assert refused.value.status == 422
    assert "demorou" in refused.value.detail


async def test_the_server_refuses_an_oversized_file_before_reading_it(server):
    path, seen = server
    reply = await _raw_reply(path, (ocr.MAX_INPUT + 1).to_bytes(8, "big"))
    assert reply["status"] == 413 and seen == []


async def test_the_server_refuses_a_second_file_while_reading_one(server):
    path, _ = server
    async with ocr_server._busy:
        reply = await _raw_reply(path, (5).to_bytes(8, "big"))
    assert reply["status"] == 429


async def test_the_server_stops_reading_when_the_bot_hangs_up(monkeypatch, folder):
    started, stopped = asyncio.Event(), asyncio.Event()

    async def extract(data):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.set()
            raise
        return {"text": "", "pages": 0, "limited": False}

    monkeypatch.setattr(ocr, "extract", extract)
    running, path = await _listen(ocr_server.handle, folder)
    try:
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write((5).to_bytes(8, "big") + b"hello")
        await writer.drain()
        await asyncio.wait_for(started.wait(), 2)
        writer.transport.abort()
        await asyncio.wait_for(stopped.wait(), 2)
        await asyncio.sleep(0.05)
        assert not ocr_server._busy.locked()
    finally:
        running.close()
        await running.wait_closed()


async def test_the_client_refuses_an_oversized_file_without_connecting(folder):
    with pytest.raises(ocr.OcrError) as refused:
        await ocr_remote.extract(os.path.join(folder, "none.sock"), b"x" * (ocr.MAX_INPUT + 1))
    assert refused.value.status == 413


async def test_a_missing_socket_is_unavailable(folder):
    with pytest.raises(ocr.OcrError) as refused:
        await ocr_remote.extract(os.path.join(folder, "none.sock"), b"synthetic image")
    assert refused.value.status == 503


async def test_something_other_than_a_socket_is_not_trusted(folder):
    path = os.path.join(folder, "ocr.sock")
    Path(path).write_text("not a socket")
    with pytest.raises(ocr.OcrError) as refused:
        await ocr_remote.extract(path, b"synthetic image")
    assert refused.value.status == 503


async def test_a_container_that_stops_reading_cannot_hold_the_bot(monkeypatch, folder):
    release = asyncio.Event()

    async def stuck(reader, writer):
        await release.wait()  # accepts, never reads
        writer.transport.abort()

    running, path = await _listen(stuck, folder)
    monkeypatch.setattr(ocr_remote, "DEADLINE", 0.3)
    try:
        with pytest.raises(ocr.OcrError) as refused:
            await asyncio.wait_for(ocr_remote.extract(path, b"x" * ocr.MAX_INPUT), 5)
        assert refused.value.status == 422
    finally:
        release.set()
        running.close()
        await running.wait_closed()


async def _fake_container(reply: bytes, folder: str):
    async def answer(reader, writer):
        size = int.from_bytes(await reader.readexactly(8), "big")
        await reader.readexactly(size)
        writer.write(reply)
        await writer.drain()
        writer.close()

    return await _listen(answer, folder)


@pytest.mark.parametrize("reply", [
    b"not json",
    b"[]",
    json.dumps({"text": 5, "pages": 1, "limited": False}).encode(),
    json.dumps({"text": "a", "pages": "1", "limited": False}).encode(),
    json.dumps({"text": "a", "pages": True, "limited": False}).encode(),
    json.dumps({"status": 200, "detail": "x"}).encode(),
    json.dumps({"status": [], "detail": "x"}).encode(),
    json.dumps({"status": 422.0, "detail": "x"}).encode(),
    b'{"text": "\\ud800", "pages": 1, "limited": false}',
    b'{"status": 422, "detail": "\\ud800"}',
    b'{"x":' + b"[" * 12000 + b"]" * 12000 + b"}",
    b"{" + b" " * (ocr_remote.MAX_REPLY + 10) + b"}",
])
async def test_a_misbehaving_container_gets_nothing_through(reply, folder):
    running, path = await _fake_container(reply, folder)
    try:
        with pytest.raises(ocr.OcrError) as refused:
            await ocr_remote.extract(path, b"synthetic image")
        assert refused.value.status == 422
        assert refused.value.detail == "Não foi possível ler este arquivo."
    finally:
        running.close()
        await running.wait_closed()


async def test_a_long_answer_and_detail_are_cut(folder):
    reply = json.dumps({"text": "a" * (ocr.MAX_TEXT + 50), "pages": 9, "limited": True}).encode()
    running, path = await _fake_container(reply, folder)
    try:
        result = await ocr_remote.extract(path, b"synthetic image")
        assert len(result["text"]) == ocr.MAX_TEXT and result["pages"] == MAX_PAGES
    finally:
        running.close()
        await running.wait_closed()
    os.unlink(path)
    running, path = await _fake_container(json.dumps({"status": 422, "detail": "x" * 500}).encode(),
                                          folder)
    try:
        with pytest.raises(ocr.OcrError) as refused:
            await ocr_remote.extract(path, b"synthetic image")
        assert len(refused.value.detail) <= 200
    finally:
        running.close()
        await running.wait_closed()


async def test_the_panel_uses_the_container_when_configured(monkeypatch):
    calls = []

    async def remote(path, data):
        calls.append(("remote", path))
        return {"text": "", "pages": 0, "limited": False}

    async def local(data):
        calls.append(("local", None))
        return {"text": "", "pages": 0, "limited": False}

    monkeypatch.setattr(ocr_remote, "extract", remote)
    monkeypatch.setattr(ocr, "extract", local)
    monkeypatch.setattr(settings, "panel_ocr_socket", "/run/ocr/ocr.sock")
    await panel_ocr.extract(b"x")
    monkeypatch.setattr(settings, "panel_ocr_socket", "")
    await panel_ocr.extract(b"x")
    assert calls == [("remote", "/run/ocr/ocr.sock"), ("local", None)]


def test_production_never_reads_inside_the_bot(monkeypatch):
    monkeypatch.setattr(settings, "inbox_ocr_enabled", True)
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "panel_ocr_socket", "")
    assert panel_ocr.enabled() is False
    monkeypatch.setattr(settings, "panel_ocr_socket", "/run/ocr/ocr.sock")
    assert panel_ocr.enabled() is True
    monkeypatch.setattr(settings, "inbox_ocr_enabled", False)
    assert panel_ocr.enabled() is False
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "inbox_ocr_enabled", True)
    monkeypatch.setattr(settings, "panel_ocr_socket", "")
    assert panel_ocr.enabled() is True


def test_the_container_has_no_network_no_secrets_and_no_files():
    data = yaml.safe_load(COMPOSE.read_text())
    service = data["services"]["ocr"]
    assert service["network_mode"] == "none" and "networks" not in service
    assert "env_file" not in service and "environment" not in service
    assert service["volumes"] == ["ocr_socket:/run/ocr"]
    assert service["read_only"] is True and service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    bot = data["services"]["reception-bot"]
    assert "ocr_socket:/run/ocr:ro" in bot["volumes"]
    assert bot["environment"]["PANEL_OCR_SOCKET"] == "/run/ocr/ocr.sock"
    assert bot["depends_on"]["ocr"]["required"] is False
    assert bot["environment"]["PANEL_OCR_ENABLED"] == "false"  # rollback images
    options = data["volumes"]["ocr_socket"]["driver_opts"]
    assert options["type"] == "tmpfs" and "size=64k" in options["o"] and "nr_inodes=16" in options["o"]
    for name in ("postgres", "redis"):
        assert not any("ocr_socket" in v for v in data["services"][name].get("volumes", []))


class _Browser:
    """An ASGI request whose browser hangs up when told to."""

    def __init__(self):
        self.hang_up = asyncio.Event()
        self.first = True

    async def receive(self):
        if self.first:
            self.first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.hang_up.wait()
        return {"type": "http.disconnect"}


async def test_closing_the_dialog_stops_the_reading():
    browser, stopped = _Browser(), asyncio.Event()

    async def reading():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.set()
            raise
        return {}

    pending = asyncio.ensure_future(panel_ocr.unless_gone(browser, reading()))
    await asyncio.sleep(0.05)
    browser.hang_up.set()
    with pytest.raises(ocr.OcrError) as refused:
        await asyncio.wait_for(pending, 2)
    assert refused.value.status == 499 and stopped.is_set()


async def test_a_browser_that_waits_gets_the_text():
    async def reading():
        return {"text": "ok", "pages": 1, "limited": False}

    assert (await panel_ocr.unless_gone(_Browser(), reading()))["text"] == "ok"
