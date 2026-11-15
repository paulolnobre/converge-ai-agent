"""Push subscriptions of the panel app (migration 0015)."""
from __future__ import annotations

from psycopg.rows import dict_row

from core.repositories.contacts import hidden_from_customer_lists_sql


async def save(conn, session_id: int, user_id: int, endpoint: str, p256dh: str, auth: str) -> None:
    """One row per endpoint: a browser that subscribes again (a new session,
    rotated keys) moves its row to the current session and starts clean."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO push_subscriptions (session_id, user_id, endpoint, p256dh, auth) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (endpoint) DO UPDATE SET session_id = EXCLUDED.session_id, "
            "user_id = EXCLUDED.user_id, p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth, "
            "failures = 0",
            (session_id, user_id, endpoint, p256dh, auth),
        )


async def delete_own(conn, session_id: int, endpoint: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM push_subscriptions WHERE session_id = %s AND endpoint = %s",
                          (session_id, endpoint))
        return cur.rowcount > 0


# Subscriptions whose session would still sign in, by the same rules as
# panel_auth.session: an expired session is not deleted at once.
_LIVE = (
    "SELECT p.id, p.endpoint, p.p256dh, p.auth FROM push_subscriptions p "
    "JOIN panel_sessions s ON s.id = p.session_id "
    "JOIN panel_users u ON u.id = s.user_id "
    "WHERE p.user_id = s.user_id AND s.mfa AND u.active AND ("
    " (s.kind = 'device' AND s.last_seen_at > NOW() - make_interval(days => %(days)s)) OR "
    " (s.kind = 'browser' AND s.expires_at > NOW() "
    "  AND s.last_seen_at > NOW() - make_interval(mins => %(mins)s))) "
)


async def active_for_user(conn, user_id: int, *, idle_minutes: int,
                          device_idle_days: int) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_LIVE + "AND p.user_id = %(user)s ORDER BY p.id",
                          {"days": device_idle_days, "mins": idle_minutes, "user": user_id})
        return list(await cur.fetchall())


async def active_for_all(conn, *, idle_minutes: int, device_idle_days: int) -> list[dict]:
    """The whole team's live subscriptions."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_LIVE + "ORDER BY p.id",
                          {"days": device_idle_days, "mins": idle_minutes})
        return list(await cur.fetchall())


async def is_team_chat(conn, chat_id: str) -> bool:
    """The team's own chat, which the Inbox hides — unless it is in /teste."""
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT {hidden_from_customer_lists_sql('%(chat)s')}", {"chat": chat_id})
        row = await cur.fetchone()
    return bool(row and row[0])


async def mark_ok(conn, sub_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE push_subscriptions SET last_ok_at = NOW(), failures = 0 "
                          "WHERE id = %s", (sub_id,))


async def mark_failed(conn, sub_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE push_subscriptions SET failures = failures + 1 WHERE id = %s",
                          (sub_id,))


async def delete(conn, sub_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM push_subscriptions WHERE id = %s", (sub_id,))


async def session_ids_with_push(conn) -> set[int]:
    async with conn.cursor() as cur:
        await cur.execute("SELECT DISTINCT session_id FROM push_subscriptions")
        return {row[0] for row in await cur.fetchall()}
