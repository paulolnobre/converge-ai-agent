"""One private administrative room. Membership is explicit, never inferred from a name."""
from __future__ import annotations

from uuid import UUID

from psycopg.rows import dict_row

from core import crypto
from core.repositories import media as media_repo
from core.repositories import push as push_repo


class NotMember(Exception):
    pass


async def require_member(conn, user_id: int) -> None:
    # Held until the caller commits: revocation and a post take turns.
    cur = await conn.execute(
        "SELECT u.id FROM panel_users u JOIN admin_room_members m ON m.user_id = u.id "
        "WHERE u.id = %s AND u.active FOR SHARE OF u, m", (user_id,))
    if await cur.fetchone() is None:
        raise NotMember


async def is_member(conn, user_id: int) -> bool:
    cur = await conn.execute(
        "SELECT 1 FROM panel_users u JOIN admin_room_members m ON m.user_id = u.id "
        "WHERE u.id = %s AND u.active", (user_id,))
    return await cur.fetchone() is not None


async def members(conn) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT u.id, u.name FROM panel_users u "
                          "JOIN admin_room_members m ON m.user_id = u.id "
                          "WHERE u.active ORDER BY u.name, u.id")
        return list(await cur.fetchall())


async def messages(conn, user_id: int, *, after: int = 0, before: int | None = None) -> list[dict]:
    await require_member(conn, user_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        if before is not None:
            await cur.execute(
                "SELECT m.*, u.name AS sender FROM admin_room_messages m "
                "LEFT JOIN panel_users u ON u.id = m.sender_id "
                "WHERE m.id < %s ORDER BY m.id DESC LIMIT 50", (before,))
        elif after:
            await cur.execute(
                "SELECT m.*, u.name AS sender FROM admin_room_messages m "
                "LEFT JOIN panel_users u ON u.id = m.sender_id "
                "WHERE m.id > %s ORDER BY m.id LIMIT 50", (after,))
        else:
            await cur.execute(
                "SELECT m.*, u.name AS sender FROM admin_room_messages m "
                "LEFT JOIN panel_users u ON u.id = m.sender_id ORDER BY m.id DESC LIMIT 50")
        rows = list(await cur.fetchall())
    attachments = await media_repo.for_admin_messages(conn, [row['id'] for row in rows])
    rows.sort(key=lambda row: row['id'])
    for row in rows:
        if row['id'] in attachments:
            row['media'] = attachments[row['id']]
        row['text'] = crypto.open_sealed(f"admin_room_messages.body:{row['id']}", row.pop('body_enc'))
        row['sender'] = row['sender'] or ('Converge' if row['kind'] != 'person' else 'Conta removida')
        row.pop('nonce')
        row.pop('event_key')
    return rows


async def insert(conn, text: str, *, sender_id: int | None = None, kind: str = 'bot',
                 nonce: UUID | None = None, event_key: str | None = None) -> int | None:
    if kind == 'person':
        if sender_id is None or nonce is None:
            raise ValueError('A person and a nonce are required')
        await require_member(conn, sender_id)
    if not text.strip() or len(text) > 4000:
        raise ValueError('Message length must be 1 to 4000')
    cur = await conn.execute("SELECT nextval(pg_get_serial_sequence('admin_room_messages', 'id'))")
    row = await cur.fetchone()
    assert row is not None
    message_id = row[0]
    sealed = crypto.seal(f'admin_room_messages.body:{message_id}', text)
    cur = await conn.execute(
        "INSERT INTO admin_room_messages (id, sender_id, kind, body_enc, nonce, event_key) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
        (message_id, sender_id, kind, sealed, nonce, event_key))
    inserted = await cur.fetchone()
    return inserted[0] if inserted else None


async def subscriptions(conn, *, except_user: int | None = None) -> list[dict]:
    from core.config import settings

    # Use the same session and revocation rules as every other push notice.
    live = await push_repo.active_for_all(conn, idle_minutes=settings.panel_idle_minutes,
                                         device_idle_days=settings.panel_device_idle_days)
    async with conn.cursor() as cur:
        await cur.execute("SELECT p.id FROM push_subscriptions p "
                          "JOIN admin_room_members m ON m.user_id = p.user_id "
                          "WHERE (%s::integer IS NULL OR p.user_id <> %s)",
                          (except_user, except_user))
        allowed = {row[0] for row in await cur.fetchall()}
    return [sub for sub in live if sub['id'] in allowed]


async def health(conn) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT checked_at, healthy, failed_since, announced "
                          "FROM admin_monitor_state WHERE name = 'bot'")
        return await cur.fetchone()
