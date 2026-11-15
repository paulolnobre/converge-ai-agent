"""Panel accounts, invites, sessions and the sign-in log.

Every token arrives here already hashed (core/services/panel_auth.digest):
nothing in these tables can be replayed as-is.
"""
from __future__ import annotations

from psycopg.rows import dict_row

_COLUMNS = (
    "id", "email", "name", "password_hash", "totp_secret_enc", "enrolled_at", "active",
    "failed_attempts", "locked_until", "last_totp_step",
)


def _user_columns(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    plain = ", ".join(prefix + column for column in _COLUMNS)
    return (f"{plain}, {prefix}locked_until IS NOT NULL "
            f"AND {prefix}locked_until > NOW() AS locked")


_USER = _user_columns()


async def user_by_email(conn, email: str) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {_USER} FROM panel_users WHERE email = %s", (email.lower(),))
        return await cur.fetchone()


async def user_by_id(conn, user_id: int) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {_USER} FROM panel_users WHERE id = %s", (user_id,))
        return await cur.fetchone()


async def list_users(conn) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT email, name, enrolled_at, active, last_login_at, "
            "locked_until IS NOT NULL AND locked_until > NOW() AS locked "
            "FROM panel_users ORDER BY name"
        )
        return list(await cur.fetchall())


async def ensure_user(conn, email: str, name: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO panel_users (email, name) VALUES (%s, %s) "
            "ON CONFLICT (email) DO UPDATE SET name = panel_users.name RETURNING id",
            (email.lower(), name),
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def reset_enrollment(conn, user_id: int) -> None:
    """Back to "invited": no password, no authenticator, signed out everywhere,
    and any earlier invite link dead."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE panel_users SET password_hash = NULL, totp_secret_enc = NULL, "
            "enrolled_at = NULL, failed_attempts = 0, locked_until = NULL, "
            "last_totp_step = NULL WHERE id = %s",
            (user_id,),
        )
        await cur.execute("DELETE FROM panel_sessions WHERE user_id = %s", (user_id,))
        await cur.execute("DELETE FROM panel_recovery_codes WHERE user_id = %s", (user_id,))
        await cur.execute(
            "DELETE FROM panel_invites WHERE user_id = %s AND used_at IS NULL", (user_id,)
        )


async def set_active(conn, email: str, active: bool) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE panel_users SET active = %s WHERE email = %s RETURNING id",
            (active, email.lower()),
        )
        row = await cur.fetchone()
        if row and not active:
            await cur.execute("DELETE FROM panel_sessions WHERE user_id = %s", (row[0],))
    return row is not None


async def create_invite(conn, token_hash: str, user_id: int, hours: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO panel_invites (token_hash, user_id, expires_at) "
            "VALUES (%s, %s, NOW() + make_interval(hours => %s))",
            (token_hash, user_id, hours),
        )


async def invited_user(conn, token_hash: str) -> dict | None:
    """The account an unused, unexpired invite belongs to."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_user_columns('u')} "
            "FROM panel_invites i JOIN panel_users u ON u.id = i.user_id "
            "WHERE i.token_hash = %s AND i.used_at IS NULL AND i.expires_at > NOW() "
            "AND u.active",
            (token_hash,),
        )
        return await cur.fetchone()


async def set_pending_totp(conn, user_id: int, secret_enc: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE panel_users SET totp_secret_enc = %s WHERE id = %s AND enrolled_at IS NULL",
            (secret_enc, user_id),
        )


async def complete_enrollment(conn, token_hash: str, user_id: int, password_hash: str,
                              step: int, recovery_hashes: list[str]) -> bool:
    """Spend the invite and activate the account, or change nothing: two tabs
    finishing the same invite enroll once."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE panel_invites SET used_at = NOW() "
            "WHERE token_hash = %s AND user_id = %s AND used_at IS NULL AND expires_at > NOW()",
            (token_hash, user_id),
        )
        if cur.rowcount == 0:
            return False
        await cur.execute(
            "UPDATE panel_users SET password_hash = %s, enrolled_at = NOW(), "
            "last_totp_step = %s, failed_attempts = 0, locked_until = NULL WHERE id = %s",
            (password_hash, step, user_id),
        )
        await cur.execute("DELETE FROM panel_recovery_codes WHERE user_id = %s", (user_id,))
        for code_hash in recovery_hashes:
            await cur.execute(
                "INSERT INTO panel_recovery_codes (user_id, code_hash) VALUES (%s, %s)",
                (user_id, code_hash),
            )
    return True


async def set_password_hash(conn, user_id: int, password_hash: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE panel_users SET password_hash = %s WHERE id = %s", (password_hash, user_id)
        )


async def record_failure(conn, user_id: int, max_attempts: int, lock_minutes: int) -> bool:
    """Count a wrong password or code; lock the account at the limit. True if locked."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE panel_users SET failed_attempts = failed_attempts + 1, "
            "locked_until = CASE WHEN failed_attempts + 1 >= %s "
            "  THEN NOW() + make_interval(mins => %s) ELSE locked_until END "
            "WHERE id = %s RETURNING locked_until IS NOT NULL AND locked_until > NOW()",
            (max_attempts, lock_minutes, user_id),
        )
        row = await cur.fetchone()
    return bool(row and row[0])


async def clear_failures(conn, user_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE panel_users SET failed_attempts = 0, locked_until = NULL, "
            "last_login_at = NOW() WHERE id = %s",
            (user_id,),
        )


async def accept_totp_step(conn, user_id: int, step: int) -> bool:
    """Atomically refuse a step at or before the last one accepted."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE panel_users SET last_totp_step = %s "
            "WHERE id = %s AND (last_totp_step IS NULL OR last_totp_step < %s)",
            (step, user_id, step),
        )
        return cur.rowcount > 0


async def use_recovery_code(conn, user_id: int, code_hash: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE panel_recovery_codes SET used_at = NOW() "
            "WHERE user_id = %s AND code_hash = %s AND used_at IS NULL",
            (user_id, code_hash),
        )
        return cur.rowcount > 0


async def recovery_codes_left(conn, user_id: int) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM panel_recovery_codes WHERE user_id = %s AND used_at IS NULL",
            (user_id,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def create_session(conn, token_hash: str, user_id: int, *, mfa: bool,
                         minutes: int, ip: str | None, user_agent: str | None,
                         kind: str = "browser", device_name: str | None = None) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO panel_sessions "
            "(token_hash, user_id, mfa, expires_at, ip, user_agent, kind, device_name) "
            "VALUES (%s, %s, %s, NOW() + make_interval(mins => %s), %s, %s, %s, %s)",
            (token_hash, user_id, mfa, minutes, ip, (user_agent or "")[:300], kind, device_name),
        )


async def session(conn, token_hash: str, *, mfa: bool, idle_minutes: int,
                  device_idle_days: int = 90) -> dict | None:
    """A live session of the given kind for an active account, or None."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.user_id, u.email, u.name, s.id AS session_id, s.kind, s.device_name, "
            "       s.last_seen_at < NOW() - INTERVAL '1 minute' AS stale "
            "FROM panel_sessions s JOIN panel_users u ON u.id = s.user_id "
            "WHERE s.token_hash = %s AND s.mfa = %s AND u.active AND ("
            " (s.kind = 'device' AND s.mfa "
            "  AND s.last_seen_at > NOW() - make_interval(days => %s)) OR "
            " ((s.kind = 'browser' OR NOT s.mfa) AND s.expires_at > NOW() "
            "  AND s.last_seen_at > NOW() - make_interval(mins => %s)))",
            (token_hash, mfa, device_idle_days, idle_minutes),
        )
        return await cur.fetchone()


async def touch_session(conn, token_hash: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE panel_sessions SET last_seen_at = NOW() WHERE token_hash = %s", (token_hash,)
        )


async def delete_session(conn, token_hash: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM panel_sessions WHERE token_hash = %s", (token_hash,))


async def renew_device_cookie(conn, token_hash: str) -> bool:
    """Only one response per day renews this device's cookie, across workers."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE panel_sessions SET cookie_refreshed_at = NOW() "
            "WHERE token_hash = %s AND kind = 'device' AND mfa "
            "AND cookie_refreshed_at < NOW() - INTERVAL '1 day'",
            (token_hash,),
        )
        return cur.rowcount > 0


async def devices(conn, current_id: int | None, idle_days: int) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT s.id, u.name, s.device_name, s.created_at, s.last_seen_at, "
            "COALESCE(s.id = %s, FALSE) AS is_current FROM panel_sessions s "
            "JOIN panel_users u ON u.id = s.user_id "
            "WHERE s.kind = 'device' AND s.mfa AND u.active "
            "AND s.last_seen_at > NOW() - make_interval(days => %s) "
            "ORDER BY s.last_seen_at DESC, s.id DESC",
            (current_id, idle_days),
        )
        return list(await cur.fetchall())


async def revoke_device(conn, session_id: int) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM panel_sessions WHERE id = %s AND kind = 'device' AND mfa",
                          (session_id,))
        return cur.rowcount > 0


async def disconnect_devices(conn, email: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM panel_sessions s USING panel_users u "
            "WHERE s.user_id = u.id AND u.email = %s AND s.kind = 'device'",
            (email.strip().lower(),),
        )
        return cur.rowcount


async def purge_expired(conn, device_idle_days: int = 90) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM panel_sessions WHERE "
            "(kind = 'device' AND mfa AND last_seen_at <= NOW() - make_interval(days => %s)) "
            "OR ((kind = 'browser' OR NOT mfa) AND expires_at < NOW())",
            (device_idle_days,),
        )


async def log_event(conn, event: str, *, user_id: int | None, email: str | None,
                    ip: str | None) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO panel_auth_events (user_id, email, event, ip) VALUES (%s, %s, %s, %s)",
            (user_id, (email or "")[:160].lower() or None, event, ip),
        )
