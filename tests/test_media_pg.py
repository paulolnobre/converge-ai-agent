"""The media tables against a real Postgres. Skipped unless TEST_DATABASE_URL
points at a throwaway database with infra/init.sql and every migration
applied; each test rolls back."""
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from core.repositories import contacts as contacts_repo
from core.repositories import media as media_repo

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
NOW = datetime(2030, 3, 5, 13, 0, tzinfo=UTC)


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    await connection.execute("SELECT 1")  # the outer transaction the rollback undoes
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _line(conn, chat_id: str) -> tuple[int, int]:
    contact = await contacts_repo.get_or_create(conn, chat_id, name="Ana")
    cur = await conn.execute(
        "INSERT INTO sessions (contact_id, chat_id, role, message, intent) "
        "VALUES (%s, %s, 'user', '', 'media') RETURNING id", (contact.id, chat_id))
    row = await cur.fetchone()
    assert row is not None and contact.id is not None
    return contact.id, row[0]


async def _media(conn, contact_id: int, session_id: int, chat_id: str, **overrides):
    values = {"contact_id": contact_id, "session_id": session_id, "chat_id": chat_id,
              "source_id": f"wa:{uuid.uuid4()}", "kind": "image", "status": "stored",
              "storage_key": uuid.uuid4(), "stored_at": NOW,
              "expires_at": NOW + timedelta(days=90), **overrides}
    columns = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    await conn.execute(f"INSERT INTO media ({columns}) VALUES ({marks})", tuple(values.values()))


async def test_media_rows_follow_the_contact(conn):
    contact_id, session_id = await _line(conn, "wa:5591900001111")
    await _media(conn, contact_id, session_id, "wa:5591900001111")
    await conn.execute("DELETE FROM contacts WHERE id = %s", (contact_id,))
    cur = await conn.execute("SELECT count(*) FROM media WHERE contact_id = %s", (contact_id,))
    assert (await cur.fetchone()) == (0,)


async def test_source_id_is_unique(conn):
    contact_id, session_id = await _line(conn, "wa:5591900002222")
    await _media(conn, contact_id, session_id, "wa:5591900002222", source_id="wa:X:1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        await _media(conn, contact_id, session_id, "wa:5591900002222", source_id="wa:X:1")


async def test_filename_in_clear_is_refused(conn):
    contact_id, session_id = await _line(conn, "wa:5591900003333")
    with pytest.raises(psycopg.errors.CheckViolation):
        await _media(conn, contact_id, session_id, "wa:5591900003333", filename="exame.pdf")


async def test_a_3gb_declared_size_fits(conn):
    contact_id, session_id = await _line(conn, "wa:5591900004444")
    await _media(conn, contact_id, session_id, "wa:5591900004444", status="too_large",
                 storage_key=None, stored_at=None, declared_bytes=3 * 1024**3)


async def test_unknown_status_is_refused(conn):
    contact_id, session_id = await _line(conn, "wa:5591900005555")
    with pytest.raises(psycopg.errors.CheckViolation):
        await _media(conn, contact_id, session_id, "wa:5591900005555", status="lost")


async def test_media_events_take_an_update_id_once(conn):
    await conn.execute("INSERT INTO media_events (update_id) VALUES ('wa:WAMID1')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        await conn.execute("INSERT INTO media_events (update_id) VALUES ('wa:WAMID1')")


# ── The repository (Task 5) ──────────────────────────────────────────────────



def _new(contact_id: int, chat_id: str, source_id: str, **overrides) -> media_repo.NewMedia:
    values: dict[str, Any] = dict(
        contact_id=contact_id, chat_id=chat_id, source_id=source_id, kind="image",
                  mime="image/jpeg", declared_bytes=None, stored_bytes=10, filename="exame.jpg",
                  storage_key=uuid.uuid4(), status="stored", stored_at=NOW, hold_until=None,
                  expires_at=NOW + timedelta(days=90))
    values.update(overrides)
    return media_repo.NewMedia(**values)


async def test_record_writes_the_line_and_the_sealed_name(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591900006666", name="Ana")
    assert contact.id is not None
    media_id = await media_repo.record(conn, "(enviou uma foto)",
                                       _new(contact.id, "wa:5591900006666", "wa:A:1"))
    assert media_id is not None
    cur = await conn.execute(
        "SELECT m.filename, m.filename_sealed IS NOT NULL, s.intent FROM media m "
        "JOIN sessions s ON s.id = m.session_id WHERE m.id = %s", (media_id,))
    assert await cur.fetchone() == (None, True, "media")
    assert await media_repo.exists(conn, "wa:A:1") is True


async def test_record_rolls_the_line_back_on_conflict(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591900007777", name="Ana")
    assert contact.id is not None
    first = _new(contact.id, "wa:5591900007777", "wa:B:1")
    assert await media_repo.record(conn, "(enviou uma foto)", first) is not None
    again = _new(contact.id, "wa:5591900007777", "wa:B:1")
    assert await media_repo.record(conn, "(enviou uma foto)", again) is None
    cur = await conn.execute("SELECT count(*) FROM sessions WHERE contact_id = %s AND intent = 'media'",
                             (contact.id,))
    assert await cur.fetchone() == (1,)


async def test_stored_total_counts_only_stored(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591900008888", name="Ana")
    assert contact.id is not None
    before = await media_repo.stored_total(conn)
    await media_repo.record(conn, "x", _new(contact.id, "wa:5591900008888", "wa:C:1",
                                            stored_bytes=100))
    await media_repo.record(conn, "x", _new(contact.id, "wa:5591900008888", "wa:C:2",
                                            stored_bytes=None, storage_key=None,
                                            status="too_large"))
    assert await media_repo.stored_total(conn) == before + 100


async def _available(conn, media_id: int) -> bool:
    cur = await conn.execute(f"SELECT {media_repo.AVAILABLE_SQL} FROM media m WHERE m.id = %s",
                             (media_id,))
    row = await cur.fetchone()
    assert row is not None
    return row[0]


async def test_the_audio_rule_in_sql(conn):
    chat = "wa:5591900009999"
    contact = await contacts_repo.get_or_create(conn, chat, name="Ana")
    assert contact.id is not None
    now = datetime.now(UTC)
    held = await media_repo.record(conn, "(enviou um áudio)", _new(
        contact.id, chat, "wa:D:1", kind="audio", stored_at=now - timedelta(hours=49),
        hold_until=now - timedelta(hours=1), expires_at=now + timedelta(hours=119)))
    assert held is not None
    assert await _available(conn, held) is False
    await conn.execute(
        "INSERT INTO handoff_sessions (contact_id, chat_id, team_chat_id, reason, relay_chat_id) "
        "VALUES (%s, %s, '-100', 'x', '-100')", (contact.id, chat))
    assert await _available(conn, held) is True
    await conn.execute("UPDATE media SET expires_at = %s WHERE id = %s",
                       (now - timedelta(minutes=1), held))
    assert await _available(conn, held) is False


async def test_events_mark_and_seen(conn):
    assert await media_repo.event_seen(conn, "tg-evt-1") is False
    await media_repo.mark_event(conn, "tg-evt-1")
    await media_repo.mark_event(conn, "tg-evt-1")  # twice: no error
    assert await media_repo.event_seen(conn, "tg-evt-1") is True


async def test_a_mark_rolls_back_with_the_turn(conn):
    try:
        async with conn.transaction():
            await media_repo.mark_event(conn, "tg-evt-2")
            raise RuntimeError("the turn fails")
    except RuntimeError:
        pass
    assert await media_repo.event_seen(conn, "tg-evt-2") is False


async def test_ending_a_test_deletes_its_files_at_once(conn, tmp_path, monkeypatch):
    from core.config import settings
    from core.repositories import test_mode as test_mode_repo
    from core.services import media_store

    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    chat = "wa:5591900012345"
    contact = await contacts_repo.get_or_create(conn, chat, name="Teste")
    assert contact.id is not None
    await conn.execute("UPDATE contacts SET test_mode = 'novo' WHERE id = %s", (contact.id,))
    key = uuid.uuid4()
    media_store.write(key, b"foto do teste")
    await media_repo.record(conn, "(enviou uma foto)", _new(contact.id, chat, "wa:T:1",
                                                            storage_key=key))
    assert await test_mode_repo.delete(conn, contact.id, staff_own=False) is True
    assert not media_store.path_for(key).exists()


async def test_a_refused_delete_keeps_the_files(conn, tmp_path, monkeypatch):
    from core.config import settings
    from core.repositories import test_mode as test_mode_repo
    from core.services import media_store

    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    chat = "wa:5591900054321"
    contact = await contacts_repo.get_or_create(conn, chat, name="Cliente de verdade")
    assert contact.id is not None
    key = uuid.uuid4()
    media_store.write(key, b"foto")
    await media_repo.record(conn, "(enviou uma foto)", _new(contact.id, chat, "wa:T:2",
                                                            storage_key=key))
    assert await test_mode_repo.delete(conn, contact.id, staff_own=False) is False
    assert media_store.path_for(key).exists()


async def test_for_sessions_brings_media_with_name_availability_and_text(conn):
    chat = "wa:5591900011111"
    contact = await contacts_repo.get_or_create(conn, chat, name="Ana")
    assert contact.id is not None
    now = datetime.now(UTC)
    ok = await media_repo.record(conn, "(enviou uma foto)", _new(
        contact.id, chat, "wa:E:1", stored_at=now, expires_at=now + timedelta(days=90)))
    gone = await media_repo.record(conn, "(enviou um documento)", _new(
        contact.id, chat, "wa:E:2", kind="document", mime="application/octet-stream",
        stored_at=now - timedelta(days=91), expires_at=now - timedelta(days=1)))
    cur = await conn.execute("SELECT session_id, id FROM media WHERE id IN (%s, %s)", (ok, gone))
    session_of = {row[1]: row[0] for row in await cur.fetchall()}
    found = await media_repo.for_sessions(conn, list(session_of.values()))
    shown = found[session_of[ok]]
    assert (shown["available"], shown["inline"], shown["filename"], shown["status_text"]) == (
        True, True, "exame.jpg", None)
    hidden = found[session_of[gone]]
    assert (hidden["available"], hidden["inline"]) == (False, False)
    assert hidden["status_text"] == "arquivo expirado"


async def test_servable_only_for_the_contact_and_while_available(conn):
    chat = "wa:5591900022222"
    contact = await contacts_repo.get_or_create(conn, chat, name="Ana")
    assert contact.id is not None
    now = datetime.now(UTC)
    media_id = await media_repo.record(conn, "(enviou uma foto)", _new(
        contact.id, chat, "wa:F:1", stored_at=now, expires_at=now + timedelta(days=90)))
    assert media_id is not None
    found = await media_repo.servable(conn, contact.id, media_id)
    assert found is not None and found.kind == "image" and found.filename == "exame.jpg"
    assert await media_repo.servable(conn, contact.id + 1000, media_id) is None
    await conn.execute("UPDATE media SET expires_at = %s WHERE id = %s",
                       (now - timedelta(seconds=1), media_id))
    assert await media_repo.servable(conn, contact.id, media_id) is None


async def test_mark_missing_only_touches_a_stored_record(conn):
    chat = "wa:5591900033333"
    contact = await contacts_repo.get_or_create(conn, chat, name="Ana")
    assert contact.id is not None
    stored = await media_repo.record(conn, "x", _new(contact.id, chat, "wa:G:1"))
    expired = await media_repo.record(conn, "x", _new(contact.id, chat, "wa:G:2", status="expired"))
    await media_repo.mark_missing(conn, stored or 0)
    await media_repo.mark_missing(conn, expired or 0)
    cur = await conn.execute("SELECT id, status FROM media WHERE id IN (%s, %s)", (stored, expired))
    assert dict(await cur.fetchall()) == {stored: "missing", expired: "expired"}
