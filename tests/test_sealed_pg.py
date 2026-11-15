"""The encrypted fields against a real Postgres: what lands in the row is
bytes, and the repositories still give the text back. Skipped unless
TEST_DATABASE_URL points at a throwaway database with infra/init.sql and every
migration applied; each test rolls back."""
import os
from pathlib import Path

import psycopg
import pytest

from core.sealed_fields import FIELDS

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
MIGRATION = Path(__file__).resolve().parent.parent / "infra/migrations/0009_sealed_fields.sql"


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    # Open the outer transaction first: a repository's conn.transaction() is
    # then a savepoint inside it, and the rollback below undoes everything.
    # Without this, a test whose first call is such a block commits for real.
    await connection.execute("SELECT 1")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _all(conn, sql, params=()):
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def test_migration_0009_is_applied_and_can_run_again(conn):
    async with conn.cursor() as cur:
        await cur.execute(MIGRATION.read_text())
    columns = {(t, c, d) for t, c, d in await _all(
        conn, "SELECT table_name, column_name, data_type FROM information_schema.columns "
              "WHERE table_schema = 'public'")}
    for f in FIELDS:
        assert (f.table, f.sealed, "bytea") in columns, f.context
    assert ("contacts", "phone_fp", "bytea") in columns
    assert ("contacts", "email_fp", "bytea") in columns
    assert ("appointments", "test_mode", "boolean") in columns
    assert ("data_format", "version", "smallint") in columns


# --- Contacts -------------------------------------------------------------

from datetime import UTC, datetime  # noqa: E402

from core.repositories import automations as automations_repo  # noqa: E402
from core.repositories import briefing as briefing_repo  # noqa: E402
from core.repositories import contacts as contacts_repo  # noqa: E402
from core.repositories import funnel as funnel_repo  # noqa: E402
from core.repositories import handoffs as handoffs_repo  # noqa: E402
from core.repositories import inbox as inbox_repo  # noqa: E402
from core.repositories import test_mode as test_repo  # noqa: E402

_CONTACT_TEXT = ("Maria", "maria@x.com", "98888-7777", "Clínica Sol", "prefere manhã",
                 "indicação", "Implantes")


async def _one(conn, sql, params=()):
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchone()


async def _raw_contact(conn, contact_id):
    return await _one(conn, "SELECT name, email, phone, organization, notes, origin_detail, "
                      "interest, name_sealed, email_sealed, phone_sealed, organization_sealed, "
                      "notes_sealed, origin_detail_sealed, interest_sealed, phone_fp, email_fp "
                      "FROM contacts WHERE id = %s", (contact_id,))


def _no_clear_text(row, texts=_CONTACT_TEXT):
    blob = b"".join(bytes(v) for v in row if isinstance(v, (bytes, memoryview)))
    for text in texts:
        assert text.encode() not in blob, text
    assert all(v is None for v in row if isinstance(v, str)), row


async def test_a_new_contact_is_stored_sealed(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591988887777", name="Maria")
    assert contact.id is not None
    await contacts_repo.update_details(conn, contact.id, email="Maria@X.com",
                                       phone="(91) 98888-7777", notes="prefere manhã")
    raw = await _raw_contact(conn, contact.id)
    assert raw[:7] == (None,) * 7
    _no_clear_text(raw)
    back = await contacts_repo.get_by_id(conn, contact.id)
    assert back is not None
    assert (back.name, back.email, back.phone, back.notes) == (
        "Maria", "Maria@X.com", "(91) 98888-7777", "prefere manhã")


async def test_a_contact_is_found_by_phone_or_email_in_another_format(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591988887777", name="Maria")
    assert contact.id is not None
    await contacts_repo.update_details(conn, contact.id, email="maria@x.com",
                                       phone="(91) 98888-7777")
    by_phone = await contacts_repo.find_by_phone(conn, "+55 91 98888-7777")
    by_email = await contacts_repo.find_by_email(conn, " Maria@X.com ")
    assert by_phone is not None and by_phone.id == contact.id
    assert by_email is not None and by_email.id == contact.id


async def test_an_unconverted_contact_is_read_from_the_old_columns(conn):
    row = await _one(conn, "INSERT INTO contacts (chat_id, name, source) "
                     "VALUES ('tg-legacy-1', 'João', 'telegram') RETURNING id")
    back = await contacts_repo.get_by_id(conn, row[0])
    assert back is not None and back.name == "João"


async def test_the_name_is_filled_once_also_when_sealed(conn):
    first = await contacts_repo.get_or_create(conn, "wa:5591977776666", name="Ana Paula")
    again = await contacts_repo.get_or_create(conn, "wa:5591977776666", name="🔥Zé🔥")
    assert first.id == again.id and again.name == "Ana Paula"


async def test_panel_edits_go_sealed(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591966665555", name="Bia")
    assert contact.id is not None
    assert await funnel_repo.update_lead(conn, contact.id, {
        "interest": "Implantes", "phone": "91 98888 7777", "organization": "Clínica Sol",
        "notes": "prefere manhã", "email": "maria@x.com", "stage": "respondido"})
    raw = await _raw_contact(conn, contact.id)
    _no_clear_text(raw)
    assert raw[14] is not None and raw[15] is not None  # fingerprints
    lead = await funnel_repo.lead(conn, contact.id)
    assert lead is not None
    assert (lead["interest"], lead["phone"], lead["organization"], lead["stage"]) == (
        "Implantes", "91 98888 7777", "Clínica Sol", "respondido")


async def test_origin_detail_goes_sealed(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591955554444", name="Caio")
    assert contact.id is not None
    user = await _one(conn, "INSERT INTO panel_users (email, name, password_hash) "
                      "VALUES ('p@x.com', 'P', 'h') RETURNING id")
    await inbox_repo.set_origin(conn, contact.id, "indicacao", "indicação", user[0])
    raw = await _raw_contact(conn, contact.id)
    assert raw[5] is None and raw[12] is not None
    view = await inbox_repo.conversation(conn, contact.id, user[0])
    assert view is not None and view["origin_detail"] == "indicação"


async def test_test_mode_creates_a_sealed_contact(conn):
    contact = await test_repo.create(conn, "wa:5591944443333", "novo", name="Teste Maria",
                                     email="maria@x.com", phone="98888-7777",
                                     inbound_at=datetime.now(UTC))
    assert contact.id is not None and contact.name == "Teste Maria"
    _no_clear_text(await _raw_contact(conn, contact.id), ("Teste Maria", "maria@x.com"))


async def test_lists_survive_an_unreadable_name(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591933332222", name="Maria")
    assert contact.id is not None
    await contacts_repo.record_message(conn, contact.id, "wa:5591933332222", "user", "oi")
    await _one(conn, "UPDATE contacts SET name_sealed = set_byte(name_sealed, 20, "
               "get_byte(name_sealed, 20) # 1) WHERE id = %s RETURNING id", (contact.id,))
    leads = await funnel_repo.list_leads(conn)
    assert any(lead["id"] == contact.id and lead["name"] == "[ilegível]" for lead in leads)
    user = await _one(conn, "INSERT INTO panel_users (email, name, password_hash) "
                      "VALUES ('q@x.com', 'Q', 'h') RETURNING id")
    rows = await inbox_repo.list_conversations(conn, user_id=user[0])
    assert any(r["contact_id"] == contact.id and r["name"] == "[ilegível]" for r in rows)


async def test_every_reader_of_a_contact_name_gets_the_text(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591922221111", name="Maria")
    assert contact.id is not None
    new = await briefing_repo.new_contacts_since(conn, datetime(2000, 1, 1, tzinfo=UTC))
    assert any(row["name"] == "Maria" for row in new)
    session = await handoffs_repo.open_session(conn, contact_id=contact.id,
                                               chat_id="wa:5591922221111",
                                               team_chat_id="-100", reason="pedido")
    await _one(conn, "UPDATE handoff_sessions SET last_activity_at = NOW() - INTERVAL '2 hours' "
               "WHERE id = %s RETURNING id", (session.id,))
    stale = await handoffs_repo.stale_sessions(conn, 60)
    assert any(s.contact_name == "Maria" for s in stale)
    names = [r["contact_name"] for r in await briefing_repo.open_handoffs(conn)]
    assert "Maria" in names
    automation = await _one(conn, "SELECT id FROM automations LIMIT 1")
    if automation:
        await automations_repo.enqueue(conn, automation[0], contact.id, None, "pending", "x")
        assert any(row["name"] == "Maria" for row in await automations_repo.pending(conn))


# --- Conversations --------------------------------------------------------

async def _user(conn, email):
    row = await _one(conn, "INSERT INTO panel_users (email, name, password_hash) "
                     "VALUES (%s, 'P', 'h') RETURNING id", (email,))
    return row[0]


async def test_messages_are_stored_sealed(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591911110000", name="Maria")
    assert contact.id is not None
    await contacts_repo.record_message(conn, contact.id, "wa:5591911110000", "user",
                                       "quero agendar amanhã")
    await contacts_repo.record_message(conn, contact.id, "wa:5591911110000", "assistant",
                                       "Claro! Qual horário?")
    raw = await _all(conn, "SELECT message, message_sealed FROM sessions WHERE contact_id = %s",
                     (contact.id,))
    assert [r[0] for r in raw] == ["", ""]
    assert all(b"agendar" not in bytes(r[1]) and b"Claro" not in bytes(r[1]) for r in raw)
    assert await contacts_repo.recent_history(conn, "wa:5591911110000") == [
        ("user", "quero agendar amanhã"), ("assistant", "Claro! Qual horário?")]


async def test_the_last_message_is_either_way(conn):
    chat = "wa:5591911110009"
    contact = await contacts_repo.get_or_create(conn, chat, name="Ivan")
    assert contact.id is not None
    assert await contacts_repo.last_message_at(conn, chat) is None
    await contacts_repo.record_message(conn, contact.id, chat, "user", "Oi")
    await contacts_repo.record_message(conn, contact.id, chat, "assistant", "Quer marcar?")
    rows = await _all(conn, "SELECT MAX(created_at) FROM sessions WHERE chat_id = %s", (chat,))
    assert await contacts_repo.last_message_at(conn, chat) == rows[0][0]


async def test_the_inbox_preview_is_cut_in_python(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591911110001", name="Maria")
    assert contact.id is not None
    await contacts_repo.record_message(conn, contact.id, "wa:5591911110001", "user", "x" * 300)
    rows = await inbox_repo.list_conversations(conn, user_id=await _user(conn, "a@x.com"))
    mine = next(r for r in rows if r["contact_id"] == contact.id)
    assert mine["last_message"] == "x" * 120


async def test_an_unreadable_message_does_not_break_the_inbox(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591911110002", name="Maria")
    assert contact.id is not None
    await contacts_repo.record_message(conn, contact.id, "wa:5591911110002", "user", "oi")
    await _one(conn, "UPDATE sessions SET message_sealed = set_byte(message_sealed, 20, "
               "get_byte(message_sealed, 20) # 1) WHERE contact_id = %s RETURNING id",
               (contact.id,))
    user = await _user(conn, "b@x.com")
    rows = await inbox_repo.list_conversations(conn, user_id=user)
    assert next(r for r in rows if r["contact_id"] == contact.id)["last_message"] == "[ilegível]"
    messages = await inbox_repo.messages(conn, contact.id)
    assert [m["message"] for m in messages] == ["[ilegível]"]
    lead = await funnel_repo.lead(conn, contact.id)
    assert lead is not None and lead["last_message"] == "[ilegível]"
    assert [m["message"] for m in lead["messages"]] == ["[ilegível]"]


async def test_an_unconverted_message_is_read_from_the_old_column(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591911110003", name="Maria")
    assert contact.id is not None
    await _one(conn, "INSERT INTO sessions (contact_id, chat_id, role, message) "
               "VALUES (%s, 'wa:5591911110003', 'user', 'oi antigo') RETURNING id",
               (contact.id,))
    assert await contacts_repo.recent_history(conn, "wa:5591911110003") == [
        ("user", "oi antigo")]
    messages = await inbox_repo.messages(conn, contact.id)
    assert [m["message"] for m in messages] == ["oi antigo"]


async def test_the_test_mode_seed_is_sealed(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591911110004", name="Teste")
    assert contact.id is not None
    await test_repo.seed_yesterday(conn, contact.id, "wa:5591911110004", "olá de ontem",
                                   "Oi! Como posso ajudar?")
    raw = await _all(conn, "SELECT message, message_sealed FROM sessions WHERE contact_id = %s",
                     (contact.id,))
    assert [r[0] for r in raw] == ["", ""] and all(r[1] is not None for r in raw)
    history = await contacts_repo.recent_history(conn, "wa:5591911110004")
    assert [text for _, text in history] == ["olá de ontem", "Oi! Como posso ajudar?"]


# --- Appointments, automations, mail -------------------------------------

from datetime import timedelta  # noqa: E402

from core.models import Appointment  # noqa: E402
from core.repositories import appointments as appointments_repo  # noqa: E402
from core.repositories import mailbox as mailbox_repo  # noqa: E402


def _appointment(contact_id, title, *, minutes=0, test_mode=False, meeting_url=None):
    start = datetime(2030, 1, 7, 13, 0, tzinfo=UTC) + timedelta(minutes=minutes)
    return Appointment(contact_id=contact_id, calendar_id="cal", provider="google",
                       provider_event_id=f"ev-{title}-{minutes}", title=title,
                       starts_at=start, ends_at=start + timedelta(minutes=30),
                       meeting_url=meeting_url, test_mode=test_mode)


async def test_appointments_are_stored_sealed(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591900001111", name="Maria")
    saved = await appointments_repo.record(conn, _appointment(
        contact.id, "Conversa com Maria", meeting_url="https://meet.example/abc-defg"))
    assert saved is not None and saved.id is not None
    raw = await _one(conn, "SELECT title, meeting_url, title_sealed, meeting_url_sealed "
                     "FROM appointments WHERE id = %s", (saved.id,))
    assert raw[0] == "" and raw[1] is None
    assert b"Maria" not in bytes(raw[2]) and b"meet.example" not in bytes(raw[3])
    back = await appointments_repo.get_by_id(conn, saved.id)
    assert back is not None
    assert (back.title, back.meeting_url) == ("Conversa com Maria", "https://meet.example/abc-defg")
    upcoming = await appointments_repo.upcoming_for_contact(conn, contact.id or 0)
    assert [a.title for a in upcoming] == ["Conversa com Maria"]


async def test_a_retried_booking_keeps_the_sealed_title(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591900001112", name="Maria")
    first = await appointments_repo.record(conn, _appointment(contact.id, "Primeira"))
    again = await appointments_repo.record(conn, _appointment(contact.id, "Primeira"))
    assert first is not None and again is not None and first.id == again.id
    assert again.title == "Primeira"


async def test_the_panel_sees_sealed_appointments(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591900001113", name="Maria")
    saved = await appointments_repo.record(conn, _appointment(
        contact.id, "Conversa com Maria", meeting_url="https://meet.example/x"))
    assert saved is not None
    rows = await funnel_repo.appointments_between(conn, datetime(2030, 1, 1, tzinfo=UTC),
                                                  datetime(2030, 2, 1, tzinfo=UTC))
    mine = next(r for r in rows if r["id"] == saved.id)
    assert (mine["title"], mine["meeting_url"], mine["contact_name"]) == (
        "Conversa com Maria", "https://meet.example/x", "Maria")
    lead = await funnel_repo.lead(conn, contact.id or 0)
    assert lead is not None and [a["title"] for a in lead["appointments"]] == ["Conversa com Maria"]


async def test_a_test_appointment_is_found_by_its_mark_not_its_title(conn):
    test_booking = await appointments_repo.record(conn, _appointment(None, "[TESTE] Conversa",
                                                                     test_mode=True))
    customer = await appointments_repo.record(conn, _appointment(None, "[TESTE] de cliente",
                                                                 minutes=60))
    assert test_booking is not None and customer is not None
    orphans = await test_repo.orphan_appointments(conn)
    assert [a.id for a in orphans] == [test_booking.id]
    await test_repo.forget_appointment(conn, customer.id or 0)
    assert await appointments_repo.get_by_id(conn, customer.id or 0) is not None
    await test_repo.forget_appointment(conn, test_booking.id or 0)
    assert await appointments_repo.get_by_id(conn, test_booking.id or 0) is None


async def test_automation_reason_is_sealed(conn):
    contact = await contacts_repo.get_or_create(conn, "wa:5591900001114", name="Maria")
    automation = await _one(conn, "SELECT id FROM automations LIMIT 1")
    assert automation is not None, "init.sql seeds the automations"
    assert contact.id is not None
    queued = await automations_repo.enqueue(conn, automation[0], contact.id, None, "pending",
                                            "Maria parou há 5 dias")
    assert queued is not None
    raw = await _one(conn, "SELECT reason, reason_sealed FROM automation_queue WHERE id = %s",
                     (queued,))
    assert raw[0] is None and b"Maria" not in bytes(raw[1])
    pending = await automations_repo.pending(conn)
    assert next(r for r in pending if r["id"] == queued)["reason"] == "Maria parou há 5 dias"


async def test_mail_subject_and_draft_are_sealed(conn):
    thread, new = await mailbox_repo.upsert_thread(
        conn, mailbox="caixa@x.com", provider="google", provider_thread_id="t-1",
        subject="Orçamento da Maria", last_message_at=datetime(2030, 1, 1, tzinfo=UTC))
    same, again = await mailbox_repo.upsert_thread(
        conn, mailbox="caixa@x.com", provider="google", provider_thread_id="t-1",
        subject="Outro assunto", last_message_at=datetime(2030, 1, 2, tzinfo=UTC))
    assert new and not again and same == thread
    raw = await _one(conn, "SELECT subject, subject_sealed FROM mail_threads WHERE id = %s",
                     (thread,))
    assert raw[0] is None and b"Maria" not in bytes(raw[1])
    assert any(row[0] == thread and row[1] == "Orçamento da Maria"
               for row in await mailbox_repo.open_threads(conn))
    draft = await mailbox_repo.save_draft(conn, thread, None, "Olá Maria, segue o orçamento.")
    raw = await _one(conn, "SELECT body_text, body_text_sealed FROM mail_drafts WHERE id = %s",
                     (draft,))
    assert raw[0] == "" and b"Maria" not in bytes(raw[1])
    assert await mailbox_repo.approve_draft(conn, draft, None) == "Olá Maria, segue o orçamento."


async def test_every_automation_finder_runs_and_opens_the_name(conn):
    """The four candidate queries, for real: an f-string missed here sends
    '{_NAME_SQL}' to Postgres and stops every automation."""
    automation = await _one(conn, "SELECT id FROM automations LIMIT 1")
    assert automation is not None
    for finder in (automations_repo.reactivate_candidates, automations_repo.stalled_candidates,
                   automations_repo.no_show_candidates, automations_repo.no_proposal_candidates):
        rows = await finder(conn, automation[0], 1)
        assert all("name_sealed" not in row for row in rows), finder.__name__


async def test_one_unreadable_appointment_does_not_stop_a_worker_list(conn, caplog):
    """The reminder and test-mode sweeps read lists: a bad row is skipped and
    named in the log (table, column, id), the others go on."""
    import logging

    contact = await contacts_repo.get_or_create(conn, "wa:5591900002222", name="Maria")
    good = await appointments_repo.record(conn, _appointment(contact.id, "Boa", minutes=0))
    bad = await appointments_repo.record(conn, _appointment(contact.id, "Ruim", minutes=60))
    assert good is not None and bad is not None
    await _one(conn, "UPDATE appointments SET title_sealed = set_byte(title_sealed, 20, "
               "get_byte(title_sealed, 20) # 1) WHERE id = %s RETURNING id", (bad.id,))
    with caplog.at_level(logging.ERROR):
        upcoming = await appointments_repo.upcoming_for_contact(conn, contact.id or 0)
    assert [a.id for a in upcoming] == [good.id]
    assert f"appointments.title id={bad.id}" in caplog.text and "Ruim" not in caplog.text


async def _aged_conversation(conn, chat_id, days):
    contact = await contacts_repo.get_or_create(conn, chat_id, name="Antiga")
    assert contact.id is not None
    await contacts_repo.record_message(conn, contact.id, chat_id, "user", "oi")
    await _one(conn, "UPDATE sessions SET created_at = NOW() - make_interval(days => %s) "
               "WHERE contact_id = %s RETURNING id", (days, contact.id))
    return contact.id


async def test_old_conversations_are_archived_after_15_days(conn):
    user = await _user(conn, "arq@x.com")
    recent = await _aged_conversation(conn, "wa:5591911110001", 10)
    old = await _aged_conversation(conn, "wa:5591911110002", 20)
    old_open = await _aged_conversation(conn, "wa:5591911110003", 20)
    await handoffs_repo.open_session(conn, contact_id=old_open, chat_id="wa:5591911110003",
                                     team_chat_id="-100", reason="pedido")

    def ids(rows):
        return {r["contact_id"] for r in rows}

    everyone = ids(await inbox_repo.list_conversations(conn, user_id=user))
    archived = ids(await inbox_repo.list_conversations(conn, user_id=user, state="archived"))
    assert recent in everyone and old_open in everyone and old not in everyone
    assert archived >= {old} and not archived & {recent, old_open}
