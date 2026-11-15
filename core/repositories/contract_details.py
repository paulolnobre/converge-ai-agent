"""What the contract needs from the customer (migration 0027): one sealed JSON
document per proposal request, written again when the customer sends it again."""
from __future__ import annotations

import json

from core.sealed_fields import field, read, write

_DATA = field("contract_details", "data")


async def save(conn, briefing_id: int, data: dict[str, str]) -> None:
    sealed = write(_DATA, json.dumps(data, ensure_ascii=False, sort_keys=True))
    await conn.execute(
        "INSERT INTO contract_details (briefing_id, data, data_sealed) VALUES (%s, %s, %s) "
        "ON CONFLICT (briefing_id) DO UPDATE SET data = EXCLUDED.data, "
        "data_sealed = EXCLUDED.data_sealed, updated_at = NOW()",
        (briefing_id, sealed["data"], sealed["data_sealed"]))


async def get(conn, briefing_id: int) -> dict[str, str] | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT id, data, data_sealed FROM contract_details "
                          "WHERE briefing_id = %s", (briefing_id,))
        row = await cur.fetchone()
    if row is None:
        return None
    text = read(_DATA, row[1], row[2], row[0])
    return json.loads(text) if text else None


async def latest_of_contact(conn, contact_id: int, but: int) -> dict[str, str] | None:
    """What this contact sent for an earlier contract (not request `but`): a
    returning customer is never asked again for the same address and CNPJ."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT d.id, d.data, d.data_sealed FROM contract_details d "
            "JOIN proposal_briefings b ON b.id = d.briefing_id "
            "WHERE b.contact_id = %s AND d.briefing_id <> %s "
            "ORDER BY d.updated_at DESC, d.id DESC LIMIT 1", (contact_id, but))
        row = await cur.fetchone()
    if row is None:
        return None
    text = read(_DATA, row[1], row[2], row[0])
    return json.loads(text) if text else None
