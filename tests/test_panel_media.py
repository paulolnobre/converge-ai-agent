"""The files in the Inbox: only for a signed-in panel, only the contact's own,
only while they may still be opened; the type from the bytes, ranges for the
players, and at most MEDIA_SERVE_CONCURRENCY files in memory at once."""
# ruff: noqa: F811
import uuid

import pytest

from core.config import settings
from core.repositories import inbox as inbox_repo
from core.repositories import media as media_repo
from core.services import media_store
from core.web import panel, panel_media
from tests.test_panel import accounts, client, clock  # noqa: F401

CONN = object()
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4  # 1032 bytes
SECURITY = {"x-content-type-options": "nosniff", "cache-control": "private, no-store",
            "content-security-policy": "default-src 'none'; sandbox"}


@pytest.fixture
def files(monkeypatch, tmp_path):
    """Records by (contact, id); the bytes really sealed on a temporary disk."""
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    rows: dict[tuple[int, int], media_repo.Servable] = {}
    missing: list[int] = []

    async def servable(conn, contact_id, media_id):
        return rows.get((contact_id, media_id))

    async def mark_missing(conn, media_id):
        missing.append(media_id)

    monkeypatch.setattr(media_repo, "servable", servable)
    monkeypatch.setattr(media_repo, "mark_missing", mark_missing)
    panel_media.reset_slots()

    def add(media_id: int, data: bytes, *, kind: str = "image", contact_id: int = 7,
            filename: str | None = "foto.png", write: bool = True) -> uuid.UUID:
        key = uuid.uuid4()
        if write:
            media_store.write(key, data)
        rows[(contact_id, media_id)] = media_repo.Servable(media_id, kind, key, filename)
        return key

    add.missing = missing  # type: ignore[attr-defined]
    return add


async def _body(response) -> bytes:
    if hasattr(response, "body_iterator"):
        return b"".join([chunk async for chunk in response.body_iterator])
    return bytes(response.body)


def _headers(response) -> dict[str, str]:
    return {k.lower(): v for k, v in response.headers.items()}


async def _serve(media_id=1, contact_id=7, *, method="GET", range_header=None):
    return await panel_media.serve(CONN, contact_id, media_id, method=method,
                                   range_header=range_header)


def test_without_a_session_it_is_401(client):
    assert client.get("/painel/api/inbox/7/media/1").status_code == 401
    assert client.head("/painel/api/inbox/7/media/1").status_code == 401


async def test_a_record_of_another_contact_is_404(files):
    files(1, PNG, contact_id=8)
    response = await _serve(1, contact_id=7)
    assert response.status_code == 404


async def test_expired_but_still_on_disk_is_404(files):
    # servable() applies the availability rule in SQL: an expired record is
    # simply not returned, whatever is still on disk.
    files(1, PNG)
    assert (await _serve(2)).status_code == 404


async def test_a_file_deleted_meanwhile_is_404_and_marked_only_if_still_stored(files):
    """Review Focus 2: the sweep deleted the file between the row and the read."""
    files(1, PNG, write=False)
    response = await _serve(1)
    assert response.status_code == 404
    assert files.missing == [1]  # mark_missing is conditional on status='stored' in SQL


async def test_a_corrupt_envelope_is_404_without_detail(files, caplog):
    key = files(1, PNG)
    media_store.path_for(key).write_bytes(b"\x01\x01" + b"\x00" * 40)
    response = await _serve(1)
    assert response.status_code == 404 and await _body(response) == b""
    assert "foto.png" not in caplog.text


async def test_a_png_is_inline_and_html_as_image_downloads(files):
    files(1, PNG)
    files(2, b"<html><script>alert(1)</script>", filename="foto.png")
    inline, html = await _serve(1), await _serve(2)
    assert _headers(inline)["content-type"] == "image/png"
    assert _headers(inline)["content-disposition"].startswith("inline;")
    assert await _body(inline) == PNG
    assert _headers(html)["content-type"] == "application/octet-stream"
    assert _headers(html)["content-disposition"].startswith("attachment;")


@pytest.mark.parametrize("header, status, content_range, length", [
    ("bytes=0-99", 206, "bytes 0-99/1032", 100),
    ("bytes=-100", 206, "bytes 932-1031/1032", 100),
    ("bytes=932-", 206, "bytes 932-1031/1032", 100),
    ("bytes=0-99999", 206, "bytes 0-1031/1032", 1032),
    ("bytes=999999999-", 416, "bytes */1032", None),
    ("bytes=0-1,5-6", 200, None, 1032),
    ("lixo", 200, None, 1032),
])
async def test_ranges(files, header, status, content_range, length):
    files(1, PNG)
    response = await _serve(1, range_header=header)
    headers = _headers(response)
    assert response.status_code == status
    assert headers.get("content-range") == content_range
    assert headers["accept-ranges"] == "bytes"
    if length is not None:
        body = await _body(response)
        assert len(body) == length == int(headers["content-length"])
        if status == 206 and header == "bytes=0-99":
            assert body == PNG[:100]


async def test_head_has_headers_and_no_body(files):
    files(1, PNG)
    response = await _serve(1, method="HEAD")
    assert response.status_code == 200 and await _body(response) == b""
    assert _headers(response)["content-length"] == str(len(PNG))


async def test_a_fifth_request_with_four_slow_clients_is_503(files, monkeypatch):
    monkeypatch.setattr(settings, "media_serve_concurrency", 4)
    monkeypatch.setattr(panel_media, "_WAIT_SECONDS", 0.05)
    panel_media.reset_slots()
    files(1, PNG)
    held = [await _serve(1) for _ in range(4)]  # bodies not read: four slow clients
    busy = await _serve(1)
    assert busy.status_code == 503 and _headers(busy)["retry-after"] == "5"
    for response in held:
        await _body(response)
    assert (await _serve(1)).status_code == 200


async def test_a_dropped_connection_gives_the_slot_back(files, monkeypatch):
    monkeypatch.setattr(settings, "media_serve_concurrency", 1)
    monkeypatch.setattr(panel_media, "_WAIT_SECONDS", 0.05)
    panel_media.reset_slots()
    files(1, PNG)
    response = await _serve(1)
    await getattr(response, "body_iterator").aclose()  # the client went away before reading
    assert (await _serve(1)).status_code == 200


@pytest.mark.parametrize("case", ["200", "206", "404", "416", "503"])
async def test_security_headers_on_every_status(files, monkeypatch, case):
    files(1, PNG)
    if case == "503":
        monkeypatch.setattr(settings, "media_serve_concurrency", 1)
        monkeypatch.setattr(panel_media, "_WAIT_SECONDS", 0.05)
        panel_media.reset_slots()
        await _serve(1)
    header = {"206": "bytes=0-9", "416": "bytes=99999-"}.get(case)
    response = await _serve(1 if case != "404" else 9, range_header=header)
    assert str(response.status_code) == case
    headers = _headers(response)
    for name, value in SECURITY.items():
        assert headers[name] == value


def test_parse_range():
    assert panel_media.parse_range(None, 10) is None
    assert panel_media.parse_range("bytes=2-4", 10) == (2, 4)
    assert panel_media.parse_range("bytes=-3", 10) == (7, 9)
    assert panel_media.parse_range("bytes=8-", 10) == (8, 9)
    assert panel_media.parse_range("bytes=10-", 10) == "unsatisfiable"
    assert panel_media.parse_range("bytes=-0", 10) == "unsatisfiable"
    assert panel_media.parse_range("bytes=5-2", 10) is None
    assert panel_media.parse_range("items=0-1", 10) is None


async def test_the_conversation_carries_media_in_one_query(monkeypatch):
    """Review Focus 4: 200 lines, one call for their media."""
    calls: list[list[int]] = []
    lines = [{"id": n, "role": "user", "message": "x"} for n in range(1, 201)]

    async def for_sessions(conn, session_ids):
        calls.append(list(session_ids))
        return {5: {"id": 1, "kind": "image", "status": "stored", "available": True}}

    async def conversation(conn, contact_id, user_id):
        return {"contact_id": contact_id, "channel": "telegram", "chat_id": "42"}

    async def messages(conn, contact_id, after=None):
        return [dict(line) for line in lines]

    monkeypatch.setattr(media_repo, "for_sessions", for_sessions)
    monkeypatch.setattr(inbox_repo, "conversation", conversation)
    monkeypatch.setattr(inbox_repo, "messages", messages)
    found = await panel._conversation(CONN, 7, 1)
    assert len(calls) == 1 and len(calls[0]) == 200
    by_id = {m["id"]: m for m in found["messages"]}
    assert by_id[5]["media"]["kind"] == "image" and "media" not in by_id[6]



async def test_a_client_gone_before_the_first_byte_gives_the_slot_back(files, monkeypatch):
    """Through Starlette itself: the disconnect arrives before streaming starts."""
    monkeypatch.setattr(settings, "media_serve_concurrency", 1)
    monkeypatch.setattr(panel_media, "_WAIT_SECONDS", 0.05)
    panel_media.reset_slots()
    files(1, PNG)
    response = await _serve(1)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        raise OSError("the socket is closed")

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "http_version": "1.1"}
    try:
        await response(scope, receive, send)
    except Exception:
        pass  # Starlette turns the failed send into ClientDisconnect
    assert (await _serve(1)).status_code == 200


async def test_a_range_of_thousands_of_digits_is_the_whole_file_and_frees_its_slot(files, monkeypatch):
    """Python refuses int() past 4300 digits: such a header must not raise
    with the slot taken (four of them would leave every download at 503)."""
    monkeypatch.setattr(settings, "media_serve_concurrency", 1)
    monkeypatch.setattr(panel_media, "_WAIT_SECONDS", 0.05)
    panel_media.reset_slots()
    files(1, PNG)
    for header in ("bytes=" + "9" * 5000 + "-", "bytes=-" + "9" * 5000,
                   "bytes=0-" + "9" * 5000):
        response = await _serve(1, range_header=header)
        assert response.status_code == 200 and await _body(response) == PNG
    assert (await _serve(1)).status_code == 200


async def test_any_failure_after_the_slot_is_taken_gives_it_back(files, monkeypatch):
    monkeypatch.setattr(settings, "media_serve_concurrency", 1)
    monkeypatch.setattr(panel_media, "_WAIT_SECONDS", 0.05)
    panel_media.reset_slots()
    files(1, PNG)
    real = panel_media.sniff

    def broken(data, kind):
        raise RuntimeError("sniff")

    monkeypatch.setattr(panel_media, "sniff", broken)
    with pytest.raises(RuntimeError):
        await _serve(1)
    monkeypatch.setattr(panel_media, "sniff", real)
    assert (await _serve(1)).status_code == 200


async def test_a_download_is_always_an_attachment(files):
    """The "Baixar" beside a player: an Ogg voice note an iPhone cannot play
    must still be saved, never opened inline in the Inbox's tab."""
    files(1, b"OggS" + b"\x00" * 64, kind="audio", filename="nota.ogg")
    response = await panel_media.serve(CONN, 7, 1, method="GET", range_header=None,
                                       download=True)
    headers = _headers(response)
    assert response.status_code == 200
    assert headers["content-type"] == "application/octet-stream"
    assert headers["content-disposition"].startswith("attachment;")
    assert "nota.ogg" in headers["content-disposition"]


def test_baixar_asks_for_the_download_not_the_inline_file():
    from pathlib import Path

    script = (Path(panel.__file__).parent / "static" / "painel" / "inbox.js").read_text()
    button = script[script.index("function downloadButton"):]
    button = button[:button.index("\n  }\n")]
    assert "?download=1" in button
    assert "window.location.href" not in button
