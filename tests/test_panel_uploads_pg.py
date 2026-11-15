"""Real authorization/SQL, encrypted files and API; providers are synthetic."""

# ruff: noqa: F811
import asyncio
import base64
import io
import json
import uuid

import pytest
from PIL import Image

from core.config import settings
from core.integrations import telegram, whatsapp
from core.repositories import admin_room, contacts
from core.services import media_cleanup, media_store, push
from core.web import panel_uploads
from tests.test_admin_commands_pg import operations  # noqa: F401
from tests.test_admin_room_pg import URL, client, database  # noqa: F401

pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture(autouse=True)
def uploads(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "panel_uploads_enabled", True)
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    monkeypatch.setattr(media_store, "free_bytes", lambda: 10 * 1024**3)
    monkeypatch.setattr(push, "schedule", lambda *a, **k: None)
    panel_uploads._limiter._events.clear()
    panel_uploads._limiter._notified.clear()
    calls = []

    async def send(*args):
        calls.append(args)
        return "ok"

    monkeypatch.setattr(telegram, "send_attachment", send)
    monkeypatch.setattr(whatsapp, "send_attachment", send)
    return calls


def photo():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (60, 100, 20)).save(buf, format="PNG")
    return buf.getvalue()


def headers(kind="document", name="arquivo.txt", caption="", nonce=None):
    return {
        "Content-Type": "application/octet-stream",
        "X-Upload-Metadata": base64.b64encode(
            json.dumps(
                {
                    "nonce": str(nonce or uuid.uuid4()),
                    "kind": kind,
                    "filename": name,
                    "caption": caption,
                }
            ).encode()
        ).decode(),
    }


async def upload(client, url="/painel/api/admin/attachments", data=b"conteudo sintetico", **meta):
    return await client.post(url, content=data, headers=headers(**meta))


async def test_admin_attachment_is_encrypted_scoped_and_idempotent(client, database, uploads):
    nonce = uuid.uuid4()
    one = await upload(client, nonce=nonce, name="privado.txt", caption="/sincronizar <img src=x>")
    assert one.status_code == 200 and one.json()["delivery_state"] == "sent"
    again = await upload(
        client, nonce=nonce, name="privado.txt", caption="/sincronizar <img src=x>"
    )
    assert again.json()["duplicate"] and one.json()["media_id"] == again.json()["media_id"]
    assert not uploads, "an admin file must never go to a customer or Telegram"
    data = (await client.get("/painel/api/admin")).json()
    assert len(data["messages"]) == 1 and "/sincronizar" in data["messages"][0]["text"]
    mid = one.json()["media_id"]
    path = f"/painel/api/admin/media/{mid}"
    found = await client.get(path)
    assert found.status_code == 200 and found.content == b"conteudo sintetico"
    assert found.headers["content-disposition"].startswith("attachment;")
    assert "no-store" in found.headers["cache-control"]
    async with database.connection() as conn:
        cur = await conn.execute(
            "SELECT storage_key,filename,filename_sealed FROM media WHERE id=%s", (mid,)
        )
        key, plain, sealed = await cur.fetchone()
        assert plain is None and b"privado" not in bytes(sealed)
        assert b"conteudo sintetico" not in media_store.path_for(key).read_bytes()
        cur = await conn.execute("SELECT count(*) FROM admin_room_commands")
        assert await cur.fetchone() == (0,)
    client.cookies.set("__dummy", "unused")
    from core.web import panel

    client.cookies.set(panel.SESSION_COOKIE, "synthetic-token-3")

    def refused(*a, **k):
        pytest.fail("must refuse before decrypting")

    import core.services.media_store as store

    original = store.read
    store.read = refused
    try:
        assert (await client.get(path)).status_code == 403
        assert (await upload(client)).status_code == 403
    finally:
        store.read = original
    client.cookies.set(panel.SESSION_COOKIE, "synthetic-token-2")
    assert (await client.get(path)).status_code == 200
    async with database.connection() as conn:
        await conn.execute("DELETE FROM admin_room_members WHERE user_id=2")
    assert (await client.get(path)).status_code == 403


async def test_auth_origin_and_membership_are_checked_before_processing(client, monkeypatch):
    async def forbidden(*a, **k):
        pytest.fail("unauthorized caller reached normalization")

    monkeypatch.setattr(panel_uploads, "normalize", forbidden)
    h = headers()
    h["Origin"] = "https://evil.test"
    assert (
        await client.post("/painel/api/admin/attachments", content=b"file", headers=h)
    ).status_code == 403
    client.cookies.clear()
    assert (await upload(client)).status_code == 401


async def test_inbox_requires_current_owner_then_records_one_channel_send(
    client, database, operations, uploads
):
    person, _, _ = operations
    url = f"/painel/api/inbox/{person.id}/attachments"
    assert (await upload(client, url)).status_code == 409
    assert not list(media_store.entries())
    assert (
        await client.post(
            f"/painel/api/inbox/{person.id}/claim", json={"state": "bot", "owner_user_id": None}
        )
    ).status_code == 200
    nonce = uuid.uuid4()
    sent = await upload(
        client,
        url,
        kind="image",
        name="foto.png",
        data=photo(),
        nonce=nonce,
        caption="Confirme esta foto",
    )
    assert sent.status_code == 200 and sent.json()["delivery_state"] == "sent"
    assert (
        await upload(
            client,
            url,
            kind="image",
            name="foto.png",
            data=photo(),
            nonce=nonce,
            caption="Confirme esta foto",
        )
    ).json()["duplicate"]
    assert len(uploads) == 1 and uploads[0][0] == person.chat_id
    data = (await client.get(f"/painel/api/inbox/{person.id}")).json()
    outgoing = [m for m in data["messages"] if m.get("media")]
    assert len(outgoing) == 1 and outgoing[0]["author"] == "Paulo"
    assert outgoing[0]["media"]["delivery_state"] == "sent"
    assert outgoing[0]["role"] == "assistant"
    mid = sent.json()["media_id"]
    assert (await client.get(f"/painel/api/inbox/{person.id}/media/{mid}")).status_code == 200
    assert (await client.get(f"/painel/api/admin/media/{mid}")).status_code == 404


async def test_same_nonce_cannot_retarget_or_change_content(client, database, uploads):
    nonce = uuid.uuid4()
    assert (await upload(client, nonce=nonce)).status_code == 200
    assert (await upload(client, nonce=nonce, data=b"different")).status_code == 409
    assert len(list(media_store.entries())) == 1


async def test_unknown_send_result_never_resends_the_nonce(
    client, database, operations, uploads, monkeypatch
):
    person, _, _ = operations
    url = f"/painel/api/inbox/{person.id}/attachments"
    await client.post(
        f"/painel/api/inbox/{person.id}/claim", json={"state": "bot", "owner_user_id": None}
    )

    async def uncertain(*args):
        uploads.append(args)
        return "uncertain"

    monkeypatch.setattr(telegram, "send_attachment", uncertain)
    nonce = uuid.uuid4()
    first = await upload(client, url, nonce=nonce)
    assert first.json()["delivery_state"] == "uncertain"
    assert (await upload(client, url, nonce=nonce)).json()["duplicate"]
    assert len(uploads) == 1


async def test_membership_removed_during_upload_refuses_before_storing(
    client, database, uploads, monkeypatch
):
    original = panel_uploads.normalize

    async def normalized(*args):
        result = await original(*args)
        async with database.connection() as conn:
            await conn.execute("DELETE FROM admin_room_members WHERE user_id=1")
        return result

    monkeypatch.setattr(panel_uploads, "normalize", normalized)
    assert (await upload(client)).status_code == 403
    assert not list(media_store.entries())


async def test_closed_whatsapp_window_never_uploads(client, database, uploads):
    async with database.connection() as conn:
        person = await contacts.get_or_create(conn, "wa:15550001111")
    await client.post(
        f"/painel/api/inbox/{person.id}/claim", json={"state": "bot", "owner_user_id": None}
    )
    assert (await upload(client, f"/painel/api/inbox/{person.id}/attachments")).status_code == 409
    assert not uploads


async def test_expired_admin_media_is_gone_and_cleaned(client, database):
    sent = await upload(client)
    mid = sent.json()["media_id"]
    async with database.connection() as conn:
        await conn.execute(
            "UPDATE media SET expires_at=NOW()-interval '1 second' WHERE id=%s", (mid,)
        )
    assert (await client.get(f"/painel/api/admin/media/{mid}")).status_code == 404
    async with database.connection() as conn:
        await media_cleanup.sweep(conn)
    assert not list(media_store.entries())


async def test_concurrent_duplicate_creates_only_one_admin_message(client, database):
    nonce = uuid.uuid4()
    a, b = await asyncio.gather(upload(client, nonce=nonce), upload(client, nonce=nonce))
    assert a.status_code == b.status_code == 200
    assert a.json()["media_id"] == b.json()["media_id"]
    async with database.connection() as conn:
        assert len(await admin_room.messages(conn, 1)) == 1


async def test_limits_format_and_disabled_flag(client, monkeypatch):
    assert (await upload(client, data=b"", name="empty.txt")).status_code == 422
    assert (await upload(client, data=b"not a photo", kind="image")).status_code == 422
    assert (
        await client.post(
            "/painel/api/admin/attachments",
            content=b"bad",
            headers={**headers(), "Content-Length": str(21 * 1024**2)},
        )
    ).status_code == 413
    monkeypatch.setattr(settings, "panel_uploads_enabled", False)
    assert (await upload(client)).status_code == 503


@pytest.mark.parametrize("change", ["session", "ownership"])
async def test_access_lost_during_decode_refuses_before_storage(
    client, database, operations, uploads, monkeypatch, change
):

    person, _, _ = operations
    await client.post(
        f"/painel/api/inbox/{person.id}/claim", json={"state": "bot", "owner_user_id": None}
    )
    original = panel_uploads.normalize

    async def decode(*args):
        result = await original(*args)
        async with database.connection() as conn:
            if change == "session":
                await conn.execute("DELETE FROM panel_sessions WHERE user_id=1")
            else:
                await conn.execute(
                    "UPDATE handoff_sessions SET claimed_by_user_id=2,claimed_by_name='Victor' WHERE contact_id=%s",
                    (person.id,),
                )
        return result

    monkeypatch.setattr(panel_uploads, "normalize", decode)
    response = await upload(client, f"/painel/api/inbox/{person.id}/attachments")
    assert response.status_code == (401 if change == "session" else 409)
    assert not uploads and not list(media_store.entries())


async def test_cancelled_send_keeps_durable_attempt_and_does_not_retry(
    client, database, operations, uploads, monkeypatch
):
    person, _, _ = operations
    await client.post(
        f"/painel/api/inbox/{person.id}/claim", json={"state": "bot", "owner_user_id": None}
    )
    entered = asyncio.Event()

    async def lost(*args):
        uploads.append(args)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(telegram, "send_attachment", lost)
    nonce = uuid.uuid4()
    url = f"/painel/api/inbox/{person.id}/attachments"
    task = asyncio.create_task(upload(client, url, nonce=nonce))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    again = await upload(client, url, nonce=nonce)
    assert again.status_code == 200 and again.json()["duplicate"]
    assert again.json()["delivery_state"] in ("sending", "uncertain")
    assert len(uploads) == 1


async def test_commit_failure_after_provider_acceptance_never_resends(
    client, database, operations, uploads, monkeypatch
):
    from contextlib import asynccontextmanager

    person, _, _ = operations
    await client.post(
        f"/painel/api/inbox/{person.id}/claim", json={"state": "bot", "owner_user_id": None}
    )
    commit_fault = [False]
    original = database.connection

    @asynccontextmanager
    async def connection():
        async with original() as conn:
            yield conn
            if commit_fault[0]:
                commit_fault[0] = False
                raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(database, "connection", connection)

    async def accepted(*args):
        uploads.append(args)
        commit_fault[0] = True
        return "ok"

    monkeypatch.setattr(telegram, "send_attachment", accepted)
    nonce = uuid.uuid4()
    url = f"/painel/api/inbox/{person.id}/attachments"
    first = await upload(client, url, nonce=nonce)
    assert first.json()["delivery_state"] == "uncertain"
    assert (await upload(client, url, nonce=nonce)).json()["duplicate"]
    assert len(uploads) == 1


async def test_quota_failure_never_sends_or_leaves_a_file(client, monkeypatch, uploads):
    monkeypatch.setattr(media_store, "has_room", lambda *a: False)
    assert (await upload(client)).status_code == 507
    assert not uploads and not list(media_store.entries())


async def test_nonce_used_by_text_cannot_turn_into_an_attachment(client, database):
    nonce = uuid.uuid4()
    assert (
        await client.post("/painel/api/admin", json={"text": "Original", "nonce": str(nonce)})
    ).status_code == 200
    assert (await upload(client, nonce=nonce)).status_code == 409
    assert not list(media_store.entries())


async def test_chunked_body_is_capped_even_without_content_length(client, monkeypatch):
    monkeypatch.setattr(panel_uploads, "MAX_BYTES", 32)

    async def chunks():
        yield b"x" * 20
        yield b"x" * 20

    response = await client.post(
        "/painel/api/admin/attachments", headers=headers(), content=chunks()
    )
    assert response.status_code == 413
    assert not list(media_store.entries())


async def test_bot_history_only_includes_confirmed_outgoing_media(
    client, database, operations, monkeypatch
):
    person, _, _ = operations
    await client.post(
        f"/painel/api/inbox/{person.id}/claim", json={"state": "bot", "owner_user_id": None}
    )

    async def uncertain(*args):
        return "uncertain"

    monkeypatch.setattr(telegram, "send_attachment", uncertain)
    sent = await upload(
        client, f"/painel/api/inbox/{person.id}/attachments", caption="texto ainda nao entregue"
    )
    assert sent.json()["delivery_state"] == "uncertain"
    async with database.connection() as conn:
        assert not any(
            "texto ainda nao entregue" in text
            for _, text in await contacts.recent_history(conn, person.chat_id)
        )
        await conn.execute(
            "UPDATE media SET delivery_state='sent' WHERE id=%s", (sent.json()["media_id"],)
        )
        assert any(
            "texto ainda nao entregue" in text
            for _, text in await contacts.recent_history(conn, person.chat_id)
        )
