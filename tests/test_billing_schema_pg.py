"""The billing tables hold what the Asaas webhook and the panel rely on,
against a real Postgres (TEST_DATABASE_URL): one billing customer per contact,
no charge for zero, each webhook event recorded once, the tax id only sealed."""
import os
import uuid

import psycopg
import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _customer(conn) -> tuple[int, int]:
    cur = await conn.execute(
        "INSERT INTO contacts (chat_id, source) VALUES (%s, 'telegram') RETURNING id",
        (uuid.uuid4().hex[:12],))
    contact_id = (await cur.fetchone())[0]
    cur = await conn.execute(
        "INSERT INTO billing_customers (contact_id, provider_id, tax_id_sealed) "
        "VALUES (%s, %s, %s) RETURNING id",
        (contact_id, f"cus_{uuid.uuid4().hex[:8]}", b"box"))
    return contact_id, (await cur.fetchone())[0]


async def test_charge_value_must_be_positive(conn):
    _, customer_id = await _customer(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        await conn.execute(
            "INSERT INTO billing_charges (customer_id, origin, method, value_cents, "
            "description, due_date, status) "
            "VALUES (%s, 'painel', 'link', 0, 'x', CURRENT_DATE, 'pendente')",
            (customer_id,))


async def test_one_billing_customer_per_contact(conn):
    contact_id, _ = await _customer(conn)
    with pytest.raises(psycopg.errors.UniqueViolation):
        await conn.execute(
            "INSERT INTO billing_customers (contact_id, provider_id) VALUES (%s, 'cus_other')",
            (contact_id,))


async def test_event_id_is_unique(conn):
    await conn.execute("INSERT INTO billing_events (event_id) VALUES ('evt_1')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        await conn.execute("INSERT INTO billing_events (event_id) VALUES ('evt_1')")


async def test_tax_id_is_never_kept_in_clear(conn):
    contact_id, _ = await _customer(conn)
    await conn.execute("DELETE FROM billing_customers WHERE contact_id = %s", (contact_id,))
    with pytest.raises(psycopg.errors.CheckViolation):
        await conn.execute(
            "INSERT INTO billing_customers (contact_id, provider_id, tax_id) "
            "VALUES (%s, 'cus_clear', '52998224725')", (contact_id,))
