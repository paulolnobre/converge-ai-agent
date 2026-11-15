"""The actor-bound, single-use confirmation ledger for room commands."""
from __future__ import annotations

from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


async def prepare(conn, *, message_id: int, user_id: int, action: str,
                  contact_id: int | None = None, expected: dict | None = None) -> UUID:
    token = uuid4()
    await conn.execute(
        "INSERT INTO admin_room_commands (id, message_id, user_id, action, contact_id, expected) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (token, message_id, user_id, action, contact_id, Jsonb(expected or {})))
    return token


async def lock_own(conn, token: UUID, user_id: int) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, user_id, action, contact_id, expected, status, "
            "expires_at <= clock_timestamp() AS expired FROM admin_room_commands "
            "WHERE id = %s AND user_id = %s FOR UPDATE", (token, user_id))
        return await cur.fetchone()


async def finish(conn, token: UUID, status: str) -> None:
    await conn.execute("UPDATE admin_room_commands SET status = %s, "
                       "finished_at = clock_timestamp() WHERE id = %s AND status = 'pending'",
                       (status, token))


async def still_current(conn, token: UUID) -> bool:
    cur = await conn.execute("SELECT 1 FROM admin_room_commands WHERE id = %s "
                             "AND status = 'pending' AND expires_at > clock_timestamp()", (token,))
    return await cur.fetchone() is not None
