"""cli.seal_data against a real Postgres: converting the rows written in
clear, checking that nothing was left, locking the old columns, and re-keying.
Skipped unless TEST_DATABASE_URL points at a throwaway database with
infra/init.sql and every migration applied; each test rolls back."""
import base64
import os

import psycopg
import pytest

from core import crypto
from core.config import settings
from core.crypto import fingerprint, key_version_of, open_sealed
from core.sealed_fields import FIELDS
from core.services import sealing

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    await connection.execute("SELECT 1")  # the outer transaction the rollback undoes
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _one(conn, sql, params=()):
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchone()


async def _legacy_world(conn) -> dict:
    """Rows as the image before 0009 wrote them: everything in clear."""
    contact = (await _one(conn, "INSERT INTO contacts (chat_id, name, email, phone, notes, source) "
                          "VALUES ('wa:5591988887777', 'Maria', 'Maria@X.com', "
                          "'(91) 98888-7777', 'prefere manhã', 'whatsapp') RETURNING id"))[0]
    session = (await _one(conn, "INSERT INTO sessions (contact_id, chat_id, role, message) "
                          "VALUES (%s, 'wa:5591988887777', 'user', 'quero agendar') "
                          "RETURNING id", (contact,)))[0]
    appointment = (await _one(conn, "INSERT INTO appointments (contact_id, calendar_id, provider, "
                              "title, starts_at, ends_at) VALUES (%s, 'c', 'google', "
                              "'Conversa com Maria', NOW() + INTERVAL '1 day', "
                              "NOW() + INTERVAL '1 day 30 minutes') RETURNING id",
                              (contact,)))[0]
    return {"contact": contact, "session": session, "appointment": appointment}


async def test_convert_seals_old_rows_and_empties_them(conn):
    world = await _legacy_world(conn)
    converted = await sealing.convert(conn)
    assert converted["contacts.name"] >= 1 and converted["sessions.message"] >= 1

    raw = await _one(conn, "SELECT name, email, phone, notes, name_sealed, phone_fp, email_fp "
                     "FROM contacts WHERE id = %s", (world["contact"],))
    assert raw[:4] == (None, None, None, None)
    assert open_sealed("contacts.name", raw[4]) == "Maria"
    assert raw[5] == fingerprint("phone", "(91) 98888-7777")
    assert raw[6] == fingerprint("email", "maria@x.com")
    message = await _one(conn, "SELECT message, message_sealed FROM sessions WHERE id = %s",
                         (world["session"],))
    assert message[0] == "" and open_sealed("sessions.message", message[1]) == "quero agendar"
    title = await _one(conn, "SELECT title, title_sealed FROM appointments WHERE id = %s",
                       (world["appointment"],))
    assert title[0] == "" and open_sealed("appointments.title", title[1]) == "Conversa com Maria"


async def test_convert_keeps_an_old_empty_string(conn):
    contact = (await _one(conn, "INSERT INTO contacts (chat_id, notes, source) "
                          "VALUES ('tg-empty', '', 'telegram') RETURNING id"))[0]
    session = (await _one(conn, "INSERT INTO sessions (contact_id, chat_id, role, message) "
                          "VALUES (%s, 'tg-empty', 'user', '') RETURNING id", (contact,)))[0]
    await sealing.convert(conn)
    notes = await _one(conn, "SELECT notes, notes_sealed FROM contacts WHERE id = %s", (contact,))
    assert notes[0] is None and open_sealed("contacts.notes", notes[1]) == ""
    message = await _one(conn, "SELECT message, message_sealed FROM sessions WHERE id = %s",
                         (session,))
    assert message[0] == "" and open_sealed("sessions.message", message[1]) == ""


async def test_convert_twice_changes_nothing(conn):
    world = await _legacy_world(conn)
    await sealing.convert(conn)
    before = await _one(conn, "SELECT name_sealed FROM contacts WHERE id = %s", (world["contact"],))
    again = await sealing.convert(conn)
    assert sum(again.values()) == 0
    after = await _one(conn, "SELECT name_sealed FROM contacts WHERE id = %s", (world["contact"],))
    assert before == after


async def test_convert_loses_the_race_to_a_new_write(conn, monkeypatch):
    """Read, then the new image writes the row, then the conditional update:
    it touches nothing, and the new value stays."""
    world = await _legacy_world(conn)
    from core.repositories import contacts as contacts_repo

    async def new_image_writes(table, row_id):
        if table == "contacts" and row_id == world["contact"]:
            await contacts_repo.update_details(conn, row_id, name="Maria Clara")

    monkeypatch.setattr(sealing, "_before_write", new_image_writes)
    await sealing.convert(conn)
    contact = await contacts_repo.get_by_id(conn, world["contact"])
    assert contact is not None and contact.name == "Maria Clara"


async def test_test_bookings_written_by_the_old_image_are_marked(conn):
    tester = (await _one(conn, "INSERT INTO contacts (chat_id, source, test_mode) "
                         "VALUES ('wa:5591900000001', 'whatsapp', 'novo') RETURNING id"))[0]
    customer = (await _one(conn, "INSERT INTO contacts (chat_id, source) "
                           "VALUES ('wa:5591900000002', 'whatsapp') RETURNING id"))[0]

    async def booking(contact_id, title, hours):
        return (await _one(conn, "INSERT INTO appointments (contact_id, calendar_id, provider, "
                           "title, starts_at, ends_at) VALUES (%s, 'c', 'google', %s, "
                           "NOW() + make_interval(hours => %s), "
                           "NOW() + make_interval(hours => %s, mins => 30)) RETURNING id",
                           (contact_id, title, hours, hours)))[0]

    linked = await booking(tester, "[TESTE] Conversa", 1)
    orphan = await booking(None, "[TESTE] Conversa", 2)
    customers = await booking(customer, "Conversa com Ana", 3)
    marked = await sealing.mark_test_appointments(conn)
    assert marked == 2
    rows = dict((r[0], r[1]) for r in await (await conn.execute(
        "SELECT id, test_mode FROM appointments WHERE id = ANY(%s)",
        ([linked, orphan, customers],))).fetchall())
    assert rows == {linked: True, orphan: True, customers: False}


async def test_scrub_audit_removes_file_names_from_failed_uploads(conn):
    await _one(conn, "INSERT INTO workspace_actions (provider, capability, action, target, "
               "succeeded, detail) VALUES ('google', 'files', 'upload', "
               "'Contrato — Maria Silva.pdf', FALSE, 'Google request failed: https://x/maria') "
               "RETURNING id")
    assert await sealing.scrub_audit(conn) == 1
    row = await _one(conn, "SELECT target, detail FROM workspace_actions ORDER BY id DESC LIMIT 1")
    assert row[0] is None and "maria" not in (row[1] or "").lower()


async def test_check_reports_each_problem_by_item_without_content(conn):
    world = await _legacy_world(conn)
    problems = await sealing.check(conn, include_locks=False)
    assert {p.item for p in problems} >= {1, 2}
    assert not any("Maria" in str(p) or "agendar" in str(p) for p in problems)

    await sealing.convert(conn)
    assert await sealing.check(conn, include_locks=False) == []

    await _one(conn, "UPDATE contacts SET name_sealed = set_byte(name_sealed, 20, "
               "get_byte(name_sealed, 20) # 1) WHERE id = %s RETURNING id", (world["contact"],))
    await _one(conn, "UPDATE contacts SET phone_fp = '\\x00' WHERE id = %s RETURNING id",
               (world["contact"],))
    await _one(conn, "INSERT INTO appointments (calendar_id, provider, title, title_sealed, "
               "starts_at, ends_at) VALUES ('c', 'google', '', %s, NOW() + INTERVAL '5 days', "
               "NOW() + INTERVAL '5 days 30 minutes') RETURNING id",
               (crypto.seal("appointments.title", "[TESTE] esquecido"),))
    await _one(conn, "INSERT INTO workspace_actions (provider, capability, action, target, "
               "succeeded) VALUES ('google', 'files', 'upload', 'x.pdf', FALSE) RETURNING id")
    found = {p.item for p in await sealing.check(conn, include_locks=True)}
    assert found >= {3, 4, 5, 8}


async def test_locks_refuse_clear_text_after_a_clean_check(conn):
    await _legacy_world(conn)
    await sealing.convert(conn)
    assert await sealing.check(conn, include_locks=False) == []
    await sealing.install_locks(conn)
    await sealing.install_locks(conn)  # twice: idempotent
    count = await _one(conn, "SELECT count(*) FROM pg_constraint WHERE conname LIKE %s",
                       ("%_sealed_only",))
    assert count[0] == len(FIELDS)
    assert await sealing.check(conn, include_locks=True) == []
    with pytest.raises(psycopg.errors.CheckViolation):
        async with conn.transaction():  # a savepoint: the failure rolls back only it
            await conn.execute("UPDATE contacts SET name = 'x'")


async def test_rekey_moves_everything_to_the_new_key_and_spares_a_fresh_write(conn, monkeypatch):
    world = await _legacy_world(conn)
    await sealing.convert(conn)
    second = base64.b64encode(bytes(range(64, 96))).decode()
    monkeypatch.setattr(settings, "data_keys", f"2:{second},{settings.data_keys}")
    crypto.reset_keys()
    from core.repositories import contacts as contacts_repo

    async def panel_writes(table, row_id):
        if table == "contacts" and row_id == world["contact"]:
            await contacts_repo.update_details(conn, row_id, notes="nota nova")

    monkeypatch.setattr(sealing, "_before_write", panel_writes)
    moved = await sealing.rekey(conn)
    assert moved >= 3
    assert await sealing.check(conn, key_version=2, include_locks=False) == []
    notes = await _one(conn, "SELECT notes_sealed FROM contacts WHERE id = %s", (world["contact"],))
    assert key_version_of(notes[0]) == 2 and open_sealed("contacts.notes", notes[0]) == "nota nova"


async def test_reindex_recomputes_fingerprints(conn, monkeypatch):
    world = await _legacy_world(conn)
    await sealing.convert(conn)
    monkeypatch.setattr(settings, "data_index_key", base64.b64encode(bytes(range(96, 128))).decode())
    crypto.reset_keys()
    assert {p.item for p in await sealing.check(conn, include_locks=False)} == {4}
    assert await sealing.reindex(conn) >= 1
    row = await _one(conn, "SELECT phone_fp FROM contacts WHERE id = %s", (world["contact"],))
    assert row[0] == fingerprint("phone", "(91) 98888-7777")
    assert await sealing.check(conn, include_locks=False) == []


@pytest.fixture(autouse=True)
def queues(monkeypatch):
    """The queues' half of seal_data, stubbed: it runs against Redis and has
    its own tests (tests/test_queue_sealing.py). Records what was asked."""
    from core.integrations import update_queue

    asked: dict = {"sealed": [], "checked": [], "problems": []}

    async def seal_stored_lists(rekey=False):
        asked["sealed"].append(rekey)
        return 0

    async def check_lists(key_version=None, settle_seconds=60):
        asked["checked"].append(key_version)
        return list(asked["problems"])

    monkeypatch.setattr(update_queue, "seal_stored_lists", seal_stored_lists)
    monkeypatch.setattr(update_queue, "check_lists", check_lists)
    return asked


@pytest.fixture
def no_commit(conn, monkeypatch):
    """The command commits after each step; here the test's rollback must
    still undo it all (the locks included)."""
    async def nothing():
        return None
    monkeypatch.setattr(conn, "commit", nothing)
    return conn


async def test_the_command_converts_checks_and_locks_and_prints_no_content(no_commit):
    from cli import seal_data

    conn = no_commit

    await _legacy_world(conn)
    code, lines = await seal_data.run(conn, [])
    assert code == 0, lines
    out = "\n".join(lines)
    assert "Maria" not in out and "agendar" not in out
    assert "travas instaladas" in out
    assert (await _one(conn, "SELECT count(*) FROM pg_constraint WHERE conname LIKE %s",
                       ("%_sealed_only",)))[0] == len(FIELDS)
    again, _ = await seal_data.run(conn, [])
    assert again == 0
    checked, _ = await seal_data.run(conn, ["--check"])
    assert checked == 0


async def test_the_command_fails_and_lists_problems_when_the_check_is_not_clean(no_commit):
    from cli import seal_data

    conn = no_commit

    await _one(conn, "INSERT INTO workspace_actions (provider, capability, action, target, "
               "succeeded) VALUES ('google', 'files', 'upload', 'Maria.pdf', FALSE) RETURNING id")
    code, lines = await seal_data.run(conn, ["--check"])
    assert code == 1
    assert any("item 5" in line for line in lines)
    assert not any("Maria" in line for line in lines)


async def test_the_command_seals_and_checks_the_queues_too(no_commit, queues):
    from cli import seal_data

    code, _ = await seal_data.run(no_commit, [])
    assert code == 0 and queues["sealed"] == [False] and queues["checked"]
    queues["problems"].append("item 6: reception:queue:deferred posição 0: JSON puro")
    code, lines = await seal_data.run(no_commit, ["--check"])
    assert code == 1 and any("item 6" in line for line in lines)
    queues["problems"].clear()
    code, _ = await seal_data.run(no_commit, ["--rekey"])
    assert code == 0 and queues["sealed"][-1] is True and queues["checked"][-1] == 1


async def test_rekey_reseals_the_kept_files_too(no_commit, monkeypatch):
    from cli import seal_data
    from core.services import media_cleanup

    async def rekey_files(conn):
        return 3

    monkeypatch.setattr(media_cleanup, "rekey_files", rekey_files)
    code, _ = await seal_data.run(no_commit, [])  # sealed and locked, so the check is clean
    assert code == 0
    code, lines = await seal_data.run(no_commit, ["--rekey"])
    assert code == 0
    assert any(line.startswith("recifrados:") and line.endswith("3 arquivos") for line in lines)
