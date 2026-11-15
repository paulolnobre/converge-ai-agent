"""Downloads of what the customer sends: a cap checked while streaming, a
deadline, and a typed result that never carries the content in its reason."""
import asyncio

import httpx
import pytest

from core import channels
from core.config import settings
from core.integrations import media_download, telegram, whatsapp
from core.integrations.media_download import Downloaded, Failed, TooLarge

MB = 1024 * 1024


class Server:
    """A fake Meta/Telegram: the file arrives in chunks, and what was read is kept."""

    def __init__(self, chunks: list[bytes], *, status: int = 200, file_size: int | None = None,
                 delay: float = 0.0):
        self.chunks, self.status, self.file_size, self.delay = chunks, status, file_size, delay
        self.requests: list[httpx.Request] = []
        self.yielded = 0

    async def body(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.yielded += 1
            yield chunk

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host in ("lookaside.example", "api.telegram.org") and (
                "/file/" in request.url.path or request.url.host == "lookaside.example"):
            return httpx.Response(self.status, content=self.body())
        if request.url.path.endswith("/getFile"):
            result: dict[str, object] = {"file_path": "voice/f.oga"}
            if self.file_size is not None:
                result["file_size"] = self.file_size
            return httpx.Response(200, json={"ok": True, "result": result})
        info: dict[str, object] = {"url": "https://lookaside.example/m1", "mime_type": "image/jpeg"}
        if self.file_size is not None:
            info["file_size"] = self.file_size
        return httpx.Response(200, json=info)


def _use(monkeypatch: pytest.MonkeyPatch, server: Server) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    monkeypatch.setattr(whatsapp, "_http", lambda: client)
    monkeypatch.setattr(telegram, "_http", lambda: client)
    monkeypatch.setattr(settings, "whatsapp_access_token", "TOK")


async def test_a_stream_over_the_cap_stops_and_is_too_large(monkeypatch):
    server = Server([b"a" * 8 * MB] * 3 + [b"never"])
    _use(monkeypatch, server)
    assert await whatsapp.fetch_media("m1", 20 * MB) == TooLarge(None)
    assert server.yielded == 3  # stopped at the chunk that crossed the cap


async def test_a_file_within_the_cap_comes_with_the_declared_type(monkeypatch):
    _use(monkeypatch, Server([b"\xff\xd8", b"\xff\xe0"]))
    assert await whatsapp.fetch_media("m1", 20 * MB) == Downloaded(b"\xff\xd8\xff\xe0", "image/jpeg")


async def test_a_declared_size_over_the_cap_is_never_downloaded(monkeypatch):
    server = Server([b"x"], file_size=25 * MB)
    _use(monkeypatch, server)
    assert await whatsapp.fetch_media("m1", 20 * MB) == TooLarge(25 * MB)
    assert [r.url.host for r in server.requests] == ["graph.facebook.com"]


async def test_telegram_declared_size_skips_getfile(monkeypatch):
    server = Server([b"x"])
    _use(monkeypatch, server)
    assert await telegram.fetch_file("f1", max_bytes=20 * MB, declared=30 * MB) == TooLarge(30 * MB)
    assert server.requests == []


async def test_telegram_getfile_size_over_the_cap_is_too_large(monkeypatch):
    server = Server([b"x"], file_size=21 * MB)
    _use(monkeypatch, server)
    assert await telegram.fetch_file("f1", max_bytes=20 * MB) == TooLarge(21 * MB)
    assert len(server.requests) == 1  # getFile only


async def test_a_slow_download_fails_with_timeout(monkeypatch):
    monkeypatch.setattr(settings, "media_download_seconds", 0.05)
    _use(monkeypatch, Server([b"a", b"b", b"c"], delay=0.2))
    assert await channels.fetch_attachment("wa-media:m1") == Failed("timeout")


async def test_cancellation_is_not_swallowed(monkeypatch):
    _use(monkeypatch, Server([b"a"] * 10, delay=0.2))
    task = asyncio.create_task(channels.fetch_attachment("wa-media:m1"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_server_error_is_failed_without_content_in_the_reason(monkeypatch):
    _use(monkeypatch, Server([b"segredo do cliente"], status=500))
    result = await whatsapp.fetch_media("m1", 20 * MB)
    assert result == Failed("status 500")


async def test_fetch_attachment_routes_by_prefix_with_the_media_cap(monkeypatch):
    heard = []

    async def fetch_media(media_id, max_bytes):
        heard.append(("wa", media_id, max_bytes))
        return Downloaded(b"W", None)

    async def fetch_file(file_id, *, max_bytes, declared=None, token=None):
        heard.append(("tg", file_id, max_bytes))
        return Downloaded(b"T", None)

    monkeypatch.setattr(whatsapp, "fetch_media", fetch_media)
    monkeypatch.setattr(telegram, "fetch_file", fetch_file)
    assert await channels.fetch_attachment("wa-media:m1") == Downloaded(b"W", None)
    assert await channels.fetch_attachment("tgfile") == Downloaded(b"T", None)
    assert heard == [("wa", "m1", settings.max_media_bytes), ("tg", "tgfile", settings.max_media_bytes)]


async def test_the_old_helpers_keep_their_contract(monkeypatch):
    _use(monkeypatch, Server([b"OGG"]))
    assert await whatsapp.download_media("m1", max_bytes=1000) == (b"OGG", "image/jpeg")
    assert await whatsapp.download_media("m1", max_bytes=2) is None
    assert await telegram.download_file("f1", max_bytes=1000) == b"OGG"
    assert await telegram.download_file("f1", max_bytes=2) is None


def test_the_result_types_are_values():
    assert media_download.Downloaded(b"x", None) == Downloaded(b"x", None)
    assert TooLarge(5) != TooLarge(None)
