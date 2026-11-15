"""Where the mailbox watcher stopped reading."""
from __future__ import annotations


async def try_lock(conn, mailbox: str) -> bool:
    """Only one replica looks at a mailbox at a time: held until this
    transaction ends, refused at once when another holds it."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                          (f"mail_alerts:{mailbox}",))
        row = await cur.fetchone()
    return bool(row and row[0])


async def get_state(conn, mailbox: str) -> tuple[int, int] | None:
    """(uidvalidity, last_uid) of the mailbox, None before the first pass."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT uidvalidity, last_uid FROM mail_alert_state "
                          "WHERE mailbox = %s", (mailbox,))
        row = await cur.fetchone()
    return (row[0], row[1]) if row else None


async def set_state(conn, mailbox: str, uidvalidity: int, last_uid: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO mail_alert_state (mailbox, uidvalidity, last_uid) "
            "VALUES (%s, %s, %s) ON CONFLICT (mailbox) DO UPDATE SET "
            "uidvalidity = EXCLUDED.uidvalidity, last_uid = EXCLUDED.last_uid, "
            "updated_at = NOW()", (mailbox, uidvalidity, last_uid))
