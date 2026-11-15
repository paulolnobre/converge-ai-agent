"""Session lifetime and cleanup against PostgreSQL, in a rollback-only schema."""
import os
import uuid
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg import sql

from core.repositories import panel_auth as repo
from core.services import panel_auth as auth

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
MIGRATIONS = Path(__file__).resolve().parent.parent / "infra/migrations"


@pytest.fixture
async def conn():
    async with await psycopg.AsyncConnection.connect(URL or "") as connection:
        schema = "devices_" + uuid.uuid4().hex
        await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        await connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        await connection.execute(cast(LiteralString, (MIGRATIONS / "0002_panel_auth.sql").read_text()))
        await connection.execute(cast(LiteralString, (MIGRATIONS / "0014_device_sessions.sql").read_text()))
        try:
            yield connection
        finally:
            await connection.rollback()


async def _session(conn, *, mfa=True, kind="device", email="ana@devices.test"):
    user_id = await repo.ensure_user(conn, email, "Ana")
    token = auth.digest(auth.new_token())
    await repo.create_session(conn, token, user_id, mfa=mfa, minutes=5,
                              ip=None, user_agent="", kind=kind, device_name="iPhone · Safari")
    return token


async def _get(conn, token, *, mfa=True):
    return await repo.session(conn, token, mfa=mfa, idle_minutes=120, device_idle_days=90)


async def test_device_lifetime_slides_instead_of_using_the_original_expiry(conn):
    token = await _session(conn)
    await conn.execute("UPDATE panel_sessions SET created_at = NOW() - INTERVAL '180 days', "
                       "expires_at = NOW() - INTERVAL '90 days', "
                       "last_seen_at = NOW() - INTERVAL '89 days' WHERE token_hash = %s", (token,))
    assert await _get(conn, token) is not None
    await repo.touch_session(conn, token)
    await repo.purge_expired(conn)
    assert await _get(conn, token) is not None
    await conn.execute("UPDATE panel_sessions SET last_seen_at = NOW() - INTERVAL '91 days' "
                       "WHERE token_hash = %s", (token,))
    assert await _get(conn, token) is None
    assert await repo.devices(conn, None, 90) == []
    await repo.purge_expired(conn)
    cur = await conn.execute("SELECT count(*) FROM panel_sessions")
    assert await cur.fetchone() == (0,)


@pytest.mark.parametrize("mfa,kind", [(False, "device"), (False, "browser"), (True, "browser")])
async def test_half_authenticated_and_browser_sessions_keep_their_expiry(conn, mfa, kind):
    token = await _session(conn, mfa=mfa, kind=kind)
    assert await _get(conn, token, mfa=mfa) is not None
    await conn.execute("UPDATE panel_sessions SET expires_at = NOW() - INTERVAL '1 second' "
                       "WHERE token_hash = %s", (token,))
    assert await _get(conn, token, mfa=mfa) is None
    await repo.purge_expired(conn)
    cur = await conn.execute("SELECT count(*) FROM panel_sessions")
    assert await cur.fetchone() == (0,)


async def test_browser_idle_limit_and_device_idle_boundary_are_enforced(conn):
    browser = await _session(conn, kind="browser")
    device = await _session(conn)
    await conn.execute("UPDATE panel_sessions SET last_seen_at = NOW() - INTERVAL '121 minutes' "
                       "WHERE token_hash = %s", (browser,))
    assert await _get(conn, browser) is None
    await conn.execute("UPDATE panel_sessions SET last_seen_at = NOW() - INTERVAL '90 days' "
                       "WHERE token_hash = %s", (device,))
    assert await _get(conn, device) is None


async def test_cookie_renewal_is_atomic_and_happens_at_most_daily(conn):
    token = await _session(conn)
    assert not await repo.renew_device_cookie(conn, token)
    await conn.execute("UPDATE panel_sessions SET cookie_refreshed_at = NOW() - INTERVAL '25 hours' "
                       "WHERE token_hash = %s", (token,))
    assert await repo.renew_device_cookie(conn, token)
    assert not await repo.renew_device_cookie(conn, token)


async def test_listing_revoke_and_cli_disconnect_do_not_expose_or_delete_browser_tokens(conn):
    device = await _session(conn)
    browser = await _session(conn, kind="browser")
    other = await _session(conn, email="bia@devices.test")
    view = await _get(conn, device)
    assert view is not None
    listed = await repo.devices(conn, view["session_id"], 90)
    assert sum(bool(row["is_current"]) for row in listed) == 1
    assert all("token_hash" not in row and "user_agent" not in row for row in listed)
    assert await repo.revoke_device(conn, view["session_id"])
    assert await _get(conn, device) is None
    assert await repo.disconnect_devices(conn, "BIA@devices.test") == 1
    assert await _get(conn, other) is None
    assert await _get(conn, browser) is not None


async def test_device_session_does_not_keep_a_disabled_user_in(conn):
    token = await _session(conn)
    await conn.execute("UPDATE panel_users SET active = FALSE")
    assert await _get(conn, token) is None
    assert await repo.devices(conn, None, 90) == []


async def test_migration_can_run_twice_and_existing_sessions_stay_browser(conn):
    await conn.execute((MIGRATIONS / "0014_device_sessions.sql").read_text())
    token = await _session(conn, kind="browser")
    assert (await _get(conn, token) or {})["kind"] == "browser"
