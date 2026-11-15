"""The test mode's SQL against a real Postgres: the migration, the cascade,
and that a real customer is never touched. Skipped unless TEST_DATABASE_URL
points at a throwaway database with infra/init.sql and every migration
applied; each test rolls back."""
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from core.repositories import contacts as contacts_repo
from core.repositories import test_mode as test_repo

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
MIGRATION = Path(__file__).resolve().parent.parent / "infra/migrations/0008_test_mode.sql"


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _one(conn, sql, params=()):
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        return await cur.fetchone()


async def _contact(conn, chat_id, test_mode=None) -> int:
    row = await _one(conn, "INSERT INTO contacts (chat_id, name, source, test_mode, "
                     "test_started_at) VALUES (%s, 'Fulano', 'telegram', %s, NOW()) "
                     "RETURNING id", (chat_id, test_mode))
    return row[0]


async def _staff(conn, chat_id):
    await _one(conn, "INSERT INTO staff (name, email, chat_id) VALUES ('Equipe', %s, %s) "
               "RETURNING id", (f"{chat_id}@exemplo.com", chat_id))


async def test_migration_applies_twice(conn):
    async with conn.cursor() as cur:
        await cur.execute(MIGRATION.read_text())
        await cur.execute(MIGRATION.read_text())
    with pytest.raises(psycopg.errors.CheckViolation):
        await _contact(conn, "t-check", "outro")


async def test_delete_cascades(conn):
    cid = await _contact(conn, "t-cascade", "novo")
    now = datetime.now(UTC)
    await _one(conn, "INSERT INTO sessions (contact_id, chat_id, role, message) "
               "VALUES (%s, 't-cascade', 'user', 'oi') RETURNING id", (cid,))
    await _one(conn, "INSERT INTO sessions (contact_id, chat_id, role, message) "
               "VALUES (NULL, 't-cascade', 'assistant', 'olá') RETURNING id")
    appt = await _one(conn, "INSERT INTO appointments (contact_id, calendar_id, provider, title, "
                      "starts_at, ends_at) VALUES (%s, 'c', 'google', 'x', %s, %s) RETURNING id",
                      (cid, now + timedelta(days=1), now + timedelta(days=1, minutes=30)))
    await _one(conn, "INSERT INTO offers (contact_id, chat_id, slots, calendar_id, expires_at) "
               "VALUES (%s, 't-cascade', '[]'::jsonb, 'c', %s) RETURNING id",
               (cid, now + timedelta(hours=1)))
    await _one(conn, "INSERT INTO workspace_actions (provider, capability, action, contact_id, "
               "succeeded) VALUES ('google', 'calendar', 'create_event', %s, true) RETURNING id",
               (cid,))
    session = await _one(conn, "INSERT INTO handoff_sessions (contact_id, chat_id, team_chat_id, "
                         "reason) VALUES (%s, 't-cascade', '-1', 'pedido') RETURNING id", (cid,))
    await _one(conn, "INSERT INTO handoff_messages (team_chat_id, message_id, session_id) "
               "VALUES ('-1', 1, %s) RETURNING session_id", (session[0],))
    queued = await _one(conn, "INSERT INTO automation_queue (automation_id, contact_id, "
                        "appointment_id, status) SELECT id, %s, %s, 'pending' FROM automations "
                        "LIMIT 1 RETURNING id", (cid, appt[0]))
    assert queued is not None  # the migration seeds the automations

    assert await test_repo.delete(conn, cid, staff_own=False) is True

    for table, column, value in [
        ("contacts", "id", cid), ("sessions", "chat_id", "t-cascade"),
        ("appointments", "contact_id", cid), ("offers", "contact_id", cid),
        ("workspace_actions", "contact_id", cid), ("handoff_sessions", "contact_id", cid),
        ("handoff_messages", "session_id", session[0]), ("automation_queue", "contact_id", cid),
    ]:
        left = await _one(conn, f"SELECT count(*) FROM {table} WHERE {column} = %s", (value,))
        assert left[0] == 0, table


async def test_delete_never_touches_a_real_customer(conn):
    cid = await _contact(conn, "t-real")
    await _one(conn, "INSERT INTO sessions (contact_id, chat_id, role, message) "
               "VALUES (%s, 't-real', 'user', 'oi') RETURNING id", (cid,))

    assert await test_repo.delete(conn, cid, staff_own=True) is False
    assert (await _one(conn, "SELECT count(*) FROM contacts WHERE id = %s", (cid,)))[0] == 1
    assert (await _one(conn, "SELECT count(*) FROM sessions WHERE chat_id = 't-real'"))[0] == 1


async def _billing(conn, cid, provider_id):
    """A customer with a subscription and a charge, as the Asaas flow leaves them."""
    customer = (await _one(conn, "INSERT INTO billing_customers (contact_id, provider_id) "
                           "VALUES (%s, %s) RETURNING id", (cid, provider_id)))[0]
    sub = (await _one(conn, "INSERT INTO billing_subscriptions (customer_id, value_cents, "
                      "description, next_due_date, status) VALUES (%s, 1000, 'plano', "
                      "CURRENT_DATE, 'ativa') RETURNING id", (customer,)))[0]
    await _one(conn, "INSERT INTO billing_charges (customer_id, subscription_id, origin, "
               "method, value_cents, description, due_date, status) VALUES (%s, %s, "
               "'painel', 'link', 1000, 'teste', CURRENT_DATE, 'pendente') RETURNING id",
               (customer, sub))


async def _billing_counts(conn, provider_id):
    """(customers, subscriptions, charges) of one customer, by the provider id."""
    customer = await _one(conn, "SELECT id FROM billing_customers WHERE provider_id = %s",
                          (provider_id,))
    if customer is None:
        return (0, 0, 0)
    counts = [1]
    for table in ("billing_subscriptions", "billing_charges"):
        row = await _one(conn, f"SELECT count(*) FROM {table} WHERE customer_id = %s",
                         (customer[0],))
        counts.append(row[0])
    return tuple(counts)


async def test_delete_takes_a_test_contacts_billing_and_spares_a_real_ones(conn):
    test_id = await _contact(conn, "t-billing", "novo")
    real_id = await _contact(conn, "t-real-billing")
    await _billing(conn, test_id, "cus_teste_1")
    await _billing(conn, real_id, "cus_real_1")

    assert await test_repo.delete(conn, test_id, staff_own=False) is True
    assert (await _one(conn, "SELECT count(*) FROM contacts WHERE id = %s", (test_id,)))[0] == 0
    assert await _billing_counts(conn, "cus_teste_1") == (0, 0, 0)
    assert await _billing_counts(conn, "cus_real_1") == (1, 1, 1)


async def test_delete_refuses_a_real_contact_and_keeps_its_billing(conn):
    cid = await _contact(conn, "t-real-billing-2")
    await _billing(conn, cid, "cus_real_2")

    assert await test_repo.delete(conn, cid, staff_own=False) is False
    assert await _billing_counts(conn, "cus_real_2") == (1, 1, 1)


async def test_delete_takes_the_staff_own_chat_only_when_asked(conn):
    await _staff(conn, "t-staff")
    cid = await _contact(conn, "t-staff")

    assert await test_repo.delete(conn, cid, staff_own=False) is False
    assert await test_repo.delete(conn, cid, staff_own=True) is True


async def test_predicates_on_real_rows(conn):
    await _staff(conn, "t-s-test")
    await _staff(conn, "t-s-plain")
    await _contact(conn, "t-s-test", "novo")
    await _contact(conn, "t-s-plain")
    await _contact(conn, "t-customer")
    expected = {  # chat: (hidden from lists, left out of metrics)
        "t-s-test": (False, True), "t-s-plain": (True, True), "t-customer": (False, False),
    }
    for chat, (hidden, left_out) in expected.items():
        row = await _one(
            conn,
            f"SELECT {contacts_repo.hidden_from_customer_lists_sql('c.chat_id')}, "
            f"{contacts_repo.left_out_of_metrics_sql('c.chat_id')} "
            "FROM contacts c WHERE c.chat_id = %s", (chat,))
        assert (row[0], row[1]) == (hidden, left_out), chat


async def test_idle_finds_only_tests_past_the_limit(conn):
    old = await _contact(conn, "t-idle", "novo")
    await _contact(conn, "t-fresh", "novo")
    await _contact(conn, "t-old-real")
    await _one(conn, "UPDATE contacts SET test_started_at = NOW() - INTERVAL '3 hours', "
               "last_inbound_at = NOW() - INTERVAL '3 hours' WHERE chat_id IN ('t-idle', "
               "'t-old-real') RETURNING id")

    found = [cid for cid, _ in await test_repo.idle(conn, 120)]
    assert old in found and len([c for c in found if c == old]) == 1
    assert await test_repo.still_idle(conn, old, 120) is True
    assert await test_repo.still_idle(conn, old, 240) is False


async def test_a_message_noted_during_the_expiry_keeps_the_test(conn):
    """C2: the expiry decided, then the tester wrote (committed elsewhere) while
    the calendar was cleaned. The delete re-checks in the same statement and
    leaves the test alone."""
    other = await psycopg.AsyncConnection.connect(URL or "", autocommit=True)
    try:
        row = await _one(other, "INSERT INTO contacts (chat_id, source, test_mode, test_started_at, "
                         "last_inbound_at) VALUES ('t-race', 'whatsapp', 'novo', "
                         "NOW() - INTERVAL '3 hours', NOW() - INTERVAL '3 hours') RETURNING id")
        cid = row[0]
        assert await test_repo.still_idle(conn, cid, 120) is True   # the decision
        await _one(other, "UPDATE contacts SET last_inbound_at = NOW() WHERE id = %s "
                   "RETURNING id", (cid,))                          # the tester writes

        assert await test_repo.delete(conn, cid, staff_own=False, idle_minutes=120) is False
        assert (await _one(other, "SELECT count(*) FROM contacts WHERE id = %s", (cid,)))[0] == 1
    finally:
        async with other.cursor() as cur:
            await cur.execute("DELETE FROM contacts WHERE chat_id = 't-race'")
        await other.close()


async def test_a_failing_reminder_does_not_undo_a_real_one(conn, monkeypatch):
    """R1 on the real database: the first reminder's history insert hits a
    foreign-key violation (its contact was wiped); the second, a real
    customer's, is still marked as reminded in the same transaction."""
    from core.integrations import telegram
    from core.models import Contact
    from core.services import reminders

    real = await _contact(conn, "t-real-rem")
    now = datetime.now(UTC)
    ids = []
    for hour in (22, 23):
        row = await _one(conn, "INSERT INTO appointments (contact_id, calendar_id, provider, title, "
                         "starts_at, ends_at) VALUES (%s, 'c', 'google', 'x', %s, %s) RETURNING id",
                         (real, now + timedelta(hours=hour), now + timedelta(hours=hour, minutes=30)))
        ids.append(row[0])
    first, second = ids
    await conn.commit()  # the batch starts with nothing pending, as in production
    contacts = {first: Contact(id=999999, chat_id="t-gone"),       # wiped meanwhile
                second: Contact(id=real, chat_id="t-real-rem", name="Ana")}

    async def due_reminders(c, hours, late):
        from core.repositories import appointments as appointments_repo
        rows = [await appointments_repo.get_by_id(c, i) for i in (first, second)]
        return [r for r in rows if r]

    async def get_by_id(c, contact_id):
        raise AssertionError("not used")

    async def send_message(chat_id, text, **kwargs):
        return {"message_id": 1}

    from core.repositories import appointments as appointments_repo
    from core.repositories import contacts as contacts_repo
    order = iter([contacts[first], contacts[second]])
    monkeypatch.setattr(appointments_repo, "due_reminders", due_reminders)
    monkeypatch.setattr(contacts_repo, "get_by_id", lambda c, i: _value(next(order)))
    monkeypatch.setattr(telegram, "send_message", send_message)

    try:
        assert await reminders.send_due(conn) == 1   # commits per reminder
        marked = await _one(conn, "SELECT array_agg(id ORDER BY id) FROM appointments "
                            "WHERE reminder_sent_at IS NOT NULL AND id = ANY(%s)", (ids,))
        assert marked[0] == [second]
    finally:
        await conn.rollback()
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM sessions WHERE chat_id = 't-real-rem'")
            await cur.execute("DELETE FROM appointments WHERE id = ANY(%s)", (ids,))
            await cur.execute("DELETE FROM contacts WHERE id = %s", (real,))
        await conn.commit()


async def _value(v):
    return v


async def test_an_interrupted_expiry_leaves_findable_rows_and_spares_customers(conn):
    """R4/R5: the expiry's wipe keeps only the appointments still to cancel in
    the calendar — ownerless and marked cancelled, so nothing else sees them —
    and deletes the rest. Leftovers are found whatever the time, and
    forgetting one never reaches a customer's appointment."""
    test = await _contact(conn, "t-orphan", "novo")
    customer = await _contact(conn, "t-orphan-customer")
    now = datetime.now(UTC)

    async def appointment(cid, hour, title, status="confirmed") -> int:
        # As the booking writes it: a test's appointment carries the mark.
        row = await _one(conn, "INSERT INTO appointments (contact_id, calendar_id, provider, title, "
                         "starts_at, ends_at, status, test_mode) "
                         "VALUES (%s, 'c', 'google', %s, %s, %s, %s, %s) "
                         "RETURNING id", (cid, title, now + timedelta(hours=hour),
                                          now + timedelta(hours=hour, minutes=30), status,
                                          title.startswith("[TESTE]")))
        return row[0]

    pending = await appointment(test, 40, "[TESTE] Atendimento: Paulo")
    past = await appointment(test, -30, "[TESTE] Atendimento: Paulo", "done")
    real = await appointment(customer, 41, "Atendimento: Ana")

    assert await test_repo.delete(conn, test, staff_own=False, keep_ids=[pending]) is True
    assert await _one(conn, "SELECT contact_id, status FROM appointments WHERE id = %s",
                      (pending,)) == (None, "cancelled")
    assert (await _one(conn, "SELECT count(*) FROM appointments WHERE id = %s", (past,)))[0] == 0

    await _one(conn, "UPDATE appointments SET starts_at = NOW() - INTERVAL '2 hours', "
               "ends_at = NOW() - INTERVAL '90 minutes' WHERE id = %s RETURNING id", (pending,))
    found = [a.id for a in await test_repo.orphan_appointments(conn)]
    assert pending in found and real not in found        # found even past its start

    await test_repo.forget_appointment(conn, real)        # never a customer's
    await test_repo.forget_appointment(conn, pending)
    left = await _one(conn, "SELECT array_agg(id ORDER BY id) FROM appointments WHERE id = ANY(%s)",
                      ([pending, real],))
    assert left[0] == [real]
