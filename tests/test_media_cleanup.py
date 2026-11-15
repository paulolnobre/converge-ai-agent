"""The sweep that keeps the promises of the privacy policy: audio gone after
48 h (or the request), at most 168 h; photos and files after 90 days; names
with their files; orphans, temporaries and old marks removed. Against a real
Postgres (TEST_DATABASE_URL) and a temporary MEDIA_DIR."""
import asyncio
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from core import crypto
from core.config import settings
from core.repositories import contacts as contacts_repo
from core.repositories import media as media_repo
from core.services import media_cleanup, media_store

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def conn(monkeypatch, tmp_path):
    """A connection that commits (the sweep runs its own transactions), on a
    database emptied of media first; every media row is removed after."""
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    connection = await psycopg.AsyncConnection.connect(URL or "", autocommit=True)
    await connection.execute("DELETE FROM media; DELETE FROM media_events")
    try:
        yield connection
    finally:
        await connection.execute("DELETE FROM media; DELETE FROM media_events")
        await connection.execute("DELETE FROM handoff_sessions WHERE reason = 'cleanup-test'")
        await connection.close()


async def _contact(conn, chat: str) -> int:
    contact = await contacts_repo.get_or_create(conn, chat, name="Ana")
    assert contact.id is not None
    return contact.id


async def _kept(conn, chat: str, *, kind="image", stored_ago=timedelta(0), status="stored",
                write=True, **overrides) -> tuple[int, uuid.UUID]:
    contact_id = await _contact(conn, chat)
    now = datetime.now(UTC)
    stored_at = now - stored_ago
    if kind == "audio":
        hold, expires = stored_at + timedelta(hours=48), stored_at + timedelta(hours=168)
    else:
        hold, expires = None, stored_at + timedelta(days=90)
    key = uuid.uuid4()
    if write:
        media_store.write(key, b"conteudo")
    values: dict[str, Any] = dict(contact_id=contact_id, chat_id=chat, source_id=f"t:{uuid.uuid4()}", kind=kind,
                  mime=None, declared_bytes=None, stored_bytes=8, filename="nome.pdf",
                  storage_key=key, status=status, stored_at=stored_at, hold_until=hold,
                  expires_at=expires)
    values.update(overrides)
    media_id = await media_repo.record(conn, "(enviou)", media_repo.NewMedia(**values))
    assert media_id is not None
    return media_id, key


async def _row(conn, media_id: int):
    cur = await conn.execute("SELECT status, filename_sealed IS NOT NULL, deleted_at IS NOT NULL "
                             "FROM media WHERE id = %s", (media_id,))
    return await cur.fetchone()


async def _open_request(conn, chat: str) -> None:
    contact_id = await _contact(conn, chat)
    await conn.execute(
        "INSERT INTO handoff_sessions (contact_id, chat_id, team_chat_id, reason, relay_chat_id) "
        "VALUES (%s, %s, '-100', 'cleanup-test', '-100')", (contact_id, chat))


async def test_audio_of_49h_without_a_request_goes_and_loses_its_name(conn):
    media_id, key = await _kept(conn, "wa:5591930000001", kind="audio",
                                stored_ago=timedelta(hours=49))
    await media_cleanup.sweep(conn)
    assert await _row(conn, media_id) == ("expired", False, True)
    assert not media_store.path_for(key).exists()


async def test_audio_with_an_open_request_stays(conn):
    media_id, key = await _kept(conn, "wa:5591930000002", kind="audio",
                                stored_ago=timedelta(hours=49))
    await _open_request(conn, "wa:5591930000002")
    await media_cleanup.sweep(conn)
    assert await _row(conn, media_id) == ("stored", True, False)
    assert media_store.path_for(key).exists()


async def test_audio_of_169h_goes_even_with_a_request(conn):
    media_id, key = await _kept(conn, "wa:5591930000003", kind="audio",
                                stored_ago=timedelta(hours=169))
    await _open_request(conn, "wa:5591930000003")
    await media_cleanup.sweep(conn)
    assert (await _row(conn, media_id))[0] == "expired"
    assert not media_store.path_for(key).exists()


async def test_a_photo_of_91_days_goes_and_a_recent_one_stays(conn):
    old, old_key = await _kept(conn, "wa:5591930000004", stored_ago=timedelta(days=91))
    new, new_key = await _kept(conn, "wa:5591930000004", stored_ago=timedelta(days=1))
    await media_cleanup.sweep(conn)
    assert await _row(conn, old) == ("expired", False, True)
    assert await _row(conn, new) == ("stored", True, False)
    assert media_store.path_for(new_key).exists() and not media_store.path_for(old_key).exists()


async def test_a_too_large_of_91_days_loses_its_name(conn):
    contact_id = await _contact(conn, "wa:5591930000005")
    created = datetime.now(UTC) - timedelta(days=91)
    media_id = await media_repo.record(conn, "(enviou)", media_repo.NewMedia(
        contact_id=contact_id, chat_id="wa:5591930000005", source_id="t:big", kind="document",
        mime=None, declared_bytes=10**9, stored_bytes=None, filename="grande.zip",
        storage_key=None, status="too_large", stored_at=None, hold_until=None,
        expires_at=created + timedelta(days=90)))
    await media_cleanup.sweep(conn)
    assert await _row(conn, media_id or 0) == ("too_large", False, False)


async def test_a_vanished_file_is_missing_without_name(conn):
    media_id, _ = await _kept(conn, "wa:5591930000006", write=False)
    await media_cleanup.sweep(conn)
    assert await _row(conn, media_id) == ("missing", False, False)


def _age(path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


async def test_orphans_and_temporaries_of_2h_go_and_of_10min_stay(conn, tmp_path):
    old_orphan, new_orphan = uuid.uuid4(), uuid.uuid4()
    media_store.write(old_orphan, b"x")
    media_store.write(new_orphan, b"y")
    _age(media_store.path_for(old_orphan), 7200)
    _age(media_store.path_for(new_orphan), 600)
    (tmp_path / "ab").mkdir(exist_ok=True)
    old_tmp, new_tmp = tmp_path / "ab" / ".tmp-old", tmp_path / "ab" / ".tmp-new"
    old_tmp.write_bytes(b"1")
    new_tmp.write_bytes(b"2")
    _age(old_tmp, 7200)
    _age(new_tmp, 600)
    await media_cleanup.sweep(conn)
    assert not media_store.path_for(old_orphan).exists() and media_store.path_for(new_orphan).exists()
    assert not old_tmp.exists() and new_tmp.exists()


async def test_a_file_of_an_expired_record_is_deleted(conn):
    media_id, key = await _kept(conn, "wa:5591930000007", status="expired")
    _age(media_store.path_for(key), 7200)
    await media_cleanup.sweep(conn)
    assert not media_store.path_for(key).exists()
    assert (await _row(conn, media_id))[0] == "expired"


async def test_events_older_than_90_days_go(conn):
    await conn.execute("INSERT INTO media_events (update_id, created_at) VALUES "
                       "('old', NOW() - INTERVAL '91 days'), ('new', NOW())")
    await media_cleanup.sweep(conn)
    cur = await conn.execute("SELECT update_id FROM media_events")
    assert [row[0] for row in await cur.fetchall()] == ["new"]


async def test_the_sweep_waits_for_the_file_lock(conn):
    """Rekey × cleanup: while a rekey holds a file's lock, the sweep does not
    touch it; once the lock is released, it does."""
    media_id, key = await _kept(conn, "wa:5591930000008", stored_ago=timedelta(days=91))
    holder = await psycopg.AsyncConnection.connect(URL or "")
    await holder.execute("SELECT pg_advisory_xact_lock(7411302, %s)", (media_id,))
    sweeper = await psycopg.AsyncConnection.connect(URL or "", autocommit=True)
    try:
        task = asyncio.create_task(media_cleanup.sweep(sweeper))
        await asyncio.sleep(0.5)
        assert media_store.path_for(key).exists() and not task.done()
        await holder.rollback()
        await asyncio.wait_for(task, timeout=5)
        assert not media_store.path_for(key).exists()
    finally:
        await holder.close()
        await sweeper.close()


async def test_rekey_moves_files_to_the_current_key_and_they_open(conn, monkeypatch):
    media_id, key = await _kept(conn, "wa:5591930000009")
    old_key = crypto.current_key_version()
    fresh = os.urandom(32)
    import base64
    monkeypatch.setattr(settings, "data_keys",
                        f"{old_key + 1}:{base64.b64encode(fresh).decode()},{settings.data_keys}")
    crypto.reset_keys()
    try:
        assert await media_cleanup.rekey_files(conn) == 1
        assert media_store.key_version(key) == old_key + 1
        assert media_store.read(key) == b"conteudo"
        assert await media_cleanup.rekey_files(conn) == 0
    finally:
        monkeypatch.undo()
        crypto.reset_keys()


async def test_rekey_leaves_an_expired_record_alone(conn, monkeypatch):
    _, key = await _kept(conn, "wa:5591930000010", status="expired")
    before = media_store.path_for(key).read_bytes()
    import base64
    monkeypatch.setattr(settings, "data_keys",
                        f"{crypto.current_key_version() + 1}:"
                        f"{base64.b64encode(os.urandom(32)).decode()},{settings.data_keys}")
    crypto.reset_keys()
    try:
        assert await media_cleanup.rekey_files(conn) == 0
        assert media_store.path_for(key).read_bytes() == before
    finally:
        monkeypatch.undo()
        crypto.reset_keys()


async def test_purge_empties_the_folder_and_the_names(conn):
    media_id, key = await _kept(conn, "wa:5591930000011")
    orphan = uuid.uuid4()
    media_store.write(orphan, b"o")
    result = await media_cleanup.purge(conn)
    assert result["files"] == 2
    assert list(media_store.entries()) == []
    assert await _row(conn, media_id) == ("expired", False, True)


async def test_inventory_counts_files_by_status_and_key(conn, monkeypatch):
    await _kept(conn, "wa:5591930000012")
    orphan = uuid.uuid4()
    media_store.write(orphan, b"o")

    async def no_queue():
        return {"active": 0}

    monkeypatch.setattr(media_cleanup.update_queue, "media_payload_counts", no_queue)
    found = await media_cleanup.inventory(conn)
    records = found["records"]
    assert isinstance(records, dict) and records["stored"]["count"] == 1
    assert found["files_by_key_version"] == {crypto.current_key_version(): 2}
    assert found["orphans"] == 1 and found["temporaries"] == 0 and found["unreadable"] == 0
    assert found["queued_media_updates"] == 0


async def test_the_command_shows_the_status_and_purge_ends_with_it(conn, monkeypatch):
    from cli import media as media_cli

    await _kept(conn, "wa:5591930000013")

    async def no_queue():
        return {"active": 0, "dead_letter": 0}

    monkeypatch.setattr(media_cleanup.update_queue, "media_payload_counts", no_queue)
    code, lines = await media_cli.run(conn, ["status"])
    out = "\n".join(lines)
    assert code == 0 and "stored: 1" in out and "nome.pdf" not in out
    code, lines = await media_cli.run(conn, ["purge"])
    out = "\n".join(lines)
    assert code == 0 and "arquivos apagados: 1" in out
    assert "queued_media_updates: 0" in out and "stored" not in out.split("arquivos apagados")[1]


async def test_the_command_sweeps_and_normalizes(conn, monkeypatch):
    from cli import media as media_cli

    asked = []

    async def normalize(seen):
        asked.append(await seen("wa:nunca"))
        return {"dropped": 0, "stripped": 0, "unreadable": 0}

    monkeypatch.setattr(media_cleanup.update_queue, "normalize_media_payloads", normalize)
    code, lines = await media_cli.run(conn, ["cleanup"])
    assert code == 0 and any("errors: 0" in line for line in lines)
    code, lines = await media_cli.run(conn, ["normalize-queues"])
    assert code == 0 and asked == [False] and any("stripped: 0" in line for line in lines)
