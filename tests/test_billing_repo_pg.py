"""Billing customers and charges against a real Postgres (TEST_DATABASE_URL):
the tax id lands sealed, a charge belongs to one contact, and the panel's
list never carries the payment link."""
import os
import uuid
from datetime import date

import psycopg
import pytest

from core.repositories import billing as billing_repo

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    await connection.execute("SELECT 1")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _contact(conn) -> int:
    cur = await conn.execute(
        "INSERT INTO contacts (chat_id, source) VALUES (%s, 'telegram') RETURNING id",
        (uuid.uuid4().hex[:12],))
    return (await cur.fetchone())[0]


async def _charge(conn, customer_id: int, **overrides) -> int:
    values: dict = dict(customer_id=customer_id, origin="painel", method="link", value_cents=1990,
                  description="Plano", due_date=date(2026, 11, 5), status="pendente",
                  requested_by=None)
    values.update(overrides)
    return await billing_repo.insert_charge(conn, **values)


async def test_customer_round_trip_keeps_tax_id_sealed(conn):
    contact_id = await _contact(conn)
    assert await billing_repo.customer_of(conn, contact_id) is None

    customer_id = await billing_repo.save_customer(conn, contact_id, "cus_1", "52998224725")

    cur = await conn.execute(
        "SELECT tax_id, tax_id_sealed FROM billing_customers WHERE id = %s", (customer_id,))
    clear, sealed = await cur.fetchone()
    assert clear is None and sealed is not None and b"52998224725" not in bytes(sealed)
    assert await billing_repo.customer_of(conn, contact_id) == {
        "id": customer_id, "provider_id": "cus_1", "tax_id": "52998224725"}


async def test_charge_lifecycle(conn):
    contact_id = await _contact(conn)
    customer_id = await billing_repo.save_customer(conn, contact_id, "cus_1", "52998224725")
    charge_id = await _charge(conn, customer_id)

    await billing_repo.set_provider(conn, charge_id, "pay_1", "https://x/i/1")
    [listed] = await billing_repo.charges_of(conn, contact_id)
    assert listed["sent"] is False
    await billing_repo.mark_sent(conn, charge_id)
    await billing_repo.set_status(conn, charge_id, "cancelado")

    [listed] = await billing_repo.charges_of(conn, contact_id)
    assert listed["id"] == charge_id
    assert listed["sent"] is True and listed["status"] == "cancelado"
    assert listed["value_cents"] == 1990 and listed["method"] == "link"
    assert "invoice_url" not in listed and "provider_id" not in listed
    found = await billing_repo.charge_of(conn, contact_id, charge_id)
    assert found is not None and found["provider_id"] == "pay_1"


async def test_charge_of_other_contact_is_none(conn):
    contact_id, other = await _contact(conn), await _contact(conn)
    customer_id = await billing_repo.save_customer(conn, contact_id, "cus_1", "52998224725")
    charge_id = await _charge(conn, customer_id)

    assert await billing_repo.charge_of(conn, other, charge_id) is None


async def test_charges_of_newest_first(conn):
    contact_id = await _contact(conn)
    customer_id = await billing_repo.save_customer(conn, contact_id, "cus_1", "52998224725")
    first = await _charge(conn, customer_id)
    second = await _charge(conn, customer_id)

    assert [c["id"] for c in await billing_repo.charges_of(conn, contact_id)] == [second, first]


async def test_event_claim_is_idempotent_and_rollback_releases_it(conn):
    key = "evt_" + uuid.uuid4().hex
    assert await billing_repo.claim_event(conn, key) is True
    assert await billing_repo.claim_event(conn, key) is False
    await conn.rollback()
    assert await billing_repo.claim_event(conn, key) is True


async def test_lookup_by_provider_and_paid_timestamp(conn):
    contact_id = await _contact(conn)
    customer_id = await billing_repo.save_customer(
        conn, contact_id, "cus_" + uuid.uuid4().hex, "52998224725")
    charge_id = await _charge(conn, customer_id)
    provider_id = "pay_" + uuid.uuid4().hex
    await billing_repo.set_provider(conn, charge_id, provider_id, None)
    found = await billing_repo.charge_for_event(conn, provider_id=provider_id, local_id=None)
    assert found is not None and found["contact_id"] == contact_id and found["id"] == charge_id
    await billing_repo.set_paid(conn, charge_id)
    cur = await conn.execute("SELECT status, paid_at FROM billing_charges WHERE id = %s",
                             (charge_id,))
    status, paid_at = await cur.fetchone()
    assert status == "pago" and paid_at is not None


async def test_local_reference_only_recovers_unbound_charge(conn):
    contact_id = await _contact(conn)
    customer_id = await billing_repo.save_customer(
        conn, contact_id, "cus_" + uuid.uuid4().hex, "52998224725")
    charge_id = await _charge(conn, customer_id)
    found = await billing_repo.charge_for_event(conn, provider_id="pay_lost", local_id=charge_id)
    assert found is not None and found["id"] == charge_id and found["provider_id"] is None
    await billing_repo.set_provider(conn, charge_id, "pay_" + uuid.uuid4().hex, None)
    assert await billing_repo.charge_for_event(
        conn, provider_id="pay_other", local_id=charge_id) is None


async def _briefing(conn, contact_id: int) -> int:
    from core.repositories import proposal_briefings as briefings_repo
    return (await briefings_repo.create(conn, contact_id, {}, [], None)).id


async def test_a_signed_contract_has_one_charge_per_part(conn):
    contact_id = await _contact(conn)
    customer_id = await billing_repo.save_customer(conn, contact_id, "cus_" + uuid.uuid4().hex,
                                                   "52998224725")
    briefing_id = await _briefing(conn, contact_id)
    values: dict = dict(customer_id=customer_id, briefing_id=briefing_id, method="cartao",
                  value_cents=123115, description="Implantação", due_date=date(2026, 11, 16))
    first = await billing_repo.insert_setup_charge(conn, part=1, **values)
    assert first is not None
    assert await billing_repo.insert_setup_charge(conn, part=1, **values) is None
    assert await billing_repo.insert_setup_charge(conn, part=2, **values) not in (None, first)
    cur = await conn.execute("SELECT origin, method, status FROM billing_charges WHERE id = %s",
                             (first,))
    assert await cur.fetchone() == ("contrato", "cartao", "pendente")
