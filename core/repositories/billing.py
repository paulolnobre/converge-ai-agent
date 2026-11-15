"""Billing customers and charges (migration 0019).

The tax id is sealed from the start. The payment link (invoice_url) is kept for
the record but never listed to the panel: a resend asks the provider for the
current one.
"""
from __future__ import annotations

from datetime import date

from psycopg.rows import dict_row

from core.sealed_fields import field, read, write

_TAX_ID = field("billing_customers", "tax_id")


async def customer_of(conn, contact_id: int) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, provider_id, tax_id, tax_id_sealed FROM billing_customers "
            "WHERE contact_id = %s", (contact_id,))
        row = await cur.fetchone()
    if row is None:
        return None
    return {"id": row["id"], "provider_id": row["provider_id"],
            "tax_id": read(_TAX_ID, row["tax_id"], row["tax_id_sealed"], row["id"])}


async def save_customer(conn, contact_id: int, provider_id: str, tax_id: str) -> int:
    sealed = write(_TAX_ID, tax_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO billing_customers (contact_id, provider_id, tax_id, tax_id_sealed) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (contact_id, provider_id, sealed["tax_id"], sealed["tax_id_sealed"]))
        return (await cur.fetchone())[0]


async def insert_charge(conn, *, customer_id: int, origin: str, method: str, value_cents: int,
                        description: str, due_date: date, status: str,
                        requested_by: int | None) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO billing_charges (customer_id, origin, method, value_cents, "
            "description, due_date, status, requested_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (customer_id, origin, method, value_cents, description, due_date, status,
             requested_by))
        return (await cur.fetchone())[0]


async def insert_setup_charge(conn, *, customer_id: int, briefing_id: int, part: int,
                              method: str, value_cents: int, description: str,
                              due_date: date) -> int | None:
    """A payment of a signed contract's setup, once per request and part
    (migration 0032). None when it exists already."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO billing_charges (customer_id, origin, method, value_cents, "
            "description, due_date, status, briefing_id, setup_part) "
            "VALUES (%s, 'contrato', %s, %s, %s, %s, 'pendente', %s, %s) "
            "ON CONFLICT (briefing_id, setup_part) WHERE briefing_id IS NOT NULL DO NOTHING "
            "RETURNING id",
            (customer_id, method, value_cents, description, due_date, briefing_id, part))
        row = await cur.fetchone()
    return row[0] if row else None


async def setup_charge(conn, briefing_id: int, part: int) -> dict | None:
    """That payment of a signed contract's setup, as far as it got."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, provider_id, status, method, value_cents, description, due_date, "
            "sent_at FROM billing_charges WHERE briefing_id = %s AND setup_part = %s",
            (briefing_id, part))
        return await cur.fetchone()


async def set_provider(conn, charge_id: int, provider_id: str, invoice_url: str | None) -> None:
    await conn.execute(
        "UPDATE billing_charges SET provider_id = %s, invoice_url = %s, updated_at = NOW() "
        "WHERE id = %s", (provider_id, invoice_url, charge_id))


async def mark_sent(conn, charge_id: int) -> None:
    await conn.execute(
        "UPDATE billing_charges SET sent_at = NOW(), updated_at = NOW() WHERE id = %s",
        (charge_id,))


async def set_status(conn, charge_id: int, status: str) -> None:
    await conn.execute(
        "UPDATE billing_charges SET status = %s, updated_at = NOW() WHERE id = %s",
        (status, charge_id))


async def claim_event(conn, event_id: str) -> bool:
    """Claim in the same transaction as the status change: failures can retry."""
    cur = await conn.execute(
        "INSERT INTO billing_events (event_id) VALUES (%s) "
        "ON CONFLICT DO NOTHING RETURNING event_id", (event_id,))
    return await cur.fetchone() is not None


async def charge_for_event(conn, *, provider_id: str | None, local_id: int | None
                           ) -> dict | None:
    """Lock the charge; a local reference only recovers an unbound charge.

    Never attach an event to a different payment already bound locally, even
    if the provider's externalReference points at it.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        if provider_id:
            await cur.execute(
                "SELECT ch.id, ch.provider_id, ch.status, ch.description, ch.value_cents, "
                "cu.contact_id, c.chat_id FROM billing_charges ch "
                "JOIN billing_customers cu ON cu.id = ch.customer_id "
                "JOIN contacts c ON c.id = cu.contact_id "
                "WHERE ch.provider_id = %s FOR UPDATE OF ch", (provider_id,))
            found = await cur.fetchone()
            if found is not None:
                return found
        if local_id is None:
            return None
        await cur.execute(
            "SELECT ch.id, ch.provider_id, ch.status, ch.description, ch.value_cents, "
            "cu.contact_id, c.chat_id FROM billing_charges ch "
            "JOIN billing_customers cu ON cu.id = ch.customer_id "
            "JOIN contacts c ON c.id = cu.contact_id "
            "WHERE ch.id = %s AND ch.provider_id IS NULL FOR UPDATE OF ch", (local_id,))
        return await cur.fetchone()


async def set_paid(conn, charge_id: int) -> None:
    await conn.execute(
        "UPDATE billing_charges SET status = 'pago', paid_at = NOW(), updated_at = NOW() "
        "WHERE id = %s", (charge_id,))


async def charge_of(conn, contact_id: int, charge_id: int) -> dict | None:
    """The charge, only if it is this contact's; locked until the transaction ends."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT ch.id, ch.provider_id, ch.method, ch.value_cents, ch.description, "
            "ch.due_date, ch.status, ch.sent_at IS NOT NULL AS sent "
            "FROM billing_charges ch JOIN billing_customers cu ON cu.id = ch.customer_id "
            "WHERE ch.id = %s AND cu.contact_id = %s FOR UPDATE OF ch",
            (charge_id, contact_id))
        return await cur.fetchone()


async def charges_of(conn, contact_id: int, limit: int = 20) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT ch.id, ch.method, ch.value_cents, ch.description, ch.due_date, ch.status, "
            "ch.sent_at IS NOT NULL AS sent, ch.created_at "
            "FROM billing_charges ch JOIN billing_customers cu ON cu.id = ch.customer_id "
            "WHERE cu.contact_id = %s ORDER BY ch.id DESC LIMIT %s",
            (contact_id, limit))
        return list(await cur.fetchall())
