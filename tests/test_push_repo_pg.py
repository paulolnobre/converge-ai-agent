"""Push subscriptions belong to a device's session, against a real Postgres
(TEST_DATABASE_URL): they go with the session, and only a session that would
still sign in gets notifications — an expired one is not deleted at once."""
import os
import uuid

import psycopg
import pytest

from core.repositories import push as push_repo

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
LIMITS = {"idle_minutes": 120, "device_idle_days": 90}


@pytest.fixture
async def conn():
    connection = await psycopg.AsyncConnection.connect(URL or "")
    await connection.execute("SELECT 1")
    try:
        yield connection
    finally:
        await connection.rollback()
        await connection.close()


async def _user(conn, active: bool = True) -> int:
    cur = await conn.execute(
        "INSERT INTO panel_users (email, name, active) VALUES (%s, 'Ana', %s) RETURNING id",
        (f"push-{uuid.uuid4().hex[:8]}@example.com", active))
    return (await cur.fetchone())[0]


async def _session(conn, user_id: int, *, kind: str = "device", mfa: bool = True,
                   expires: str = "1 day", seen: str = "0 minutes") -> int:
    cur = await conn.execute(
        "INSERT INTO panel_sessions (token_hash, user_id, mfa, expires_at, last_seen_at, kind) "
        "VALUES (%s, %s, %s, NOW() + %s::interval, NOW() - %s::interval, %s) RETURNING id",
        (uuid.uuid4().hex + uuid.uuid4().hex, user_id, mfa, expires, seen, kind))
    return (await cur.fetchone())[0]


def _endpoint() -> str:
    return f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}"


async def _endpoints(conn, user_id: int) -> list[str]:
    return [s["endpoint"] for s in await push_repo.active_for_user(conn, user_id, **LIMITS)]


async def test_the_same_endpoint_moves_to_the_new_session(conn):
    user = await _user(conn)
    old, new = await _session(conn, user), await _session(conn, user)
    endpoint = _endpoint()
    await push_repo.save(conn, old, user, endpoint, "k", "a")
    await conn.execute("UPDATE push_subscriptions SET failures = 3 WHERE endpoint = %s", (endpoint,))
    await push_repo.save(conn, new, user, endpoint, "k2", "a2")
    cur = await conn.execute(
        "SELECT session_id, p256dh, failures FROM push_subscriptions WHERE endpoint = %s",
        (endpoint,))
    assert await cur.fetchall() == [(new, "k2", 0)]


async def test_a_deleted_session_takes_its_subscription(conn):
    user = await _user(conn)
    session = await _session(conn, user)
    await push_repo.save(conn, session, user, _endpoint(), "k", "a")
    await conn.execute("DELETE FROM panel_sessions WHERE id = %s", (session,))
    assert await _endpoints(conn, user) == []
    cur = await conn.execute("SELECT count(*) FROM push_subscriptions WHERE session_id = %s",
                             (session,))
    assert (await cur.fetchone())[0] == 0


@pytest.mark.parametrize("dead", [
    {"kind": "browser", "expires": "-1 minute"},
    {"kind": "browser", "seen": "3 hours"},
    {"kind": "device", "seen": "91 days"},
    {"mfa": False},
])
async def test_only_a_session_that_would_still_sign_in_gets_notifications(conn, dead):
    user = await _user(conn)
    live, gone = _endpoint(), _endpoint()
    await push_repo.save(conn, await _session(conn, user), user, live, "k", "a")
    await push_repo.save(conn, await _session(conn, user, **dead), user, gone, "k", "a")
    assert await _endpoints(conn, user) == [live]


async def test_an_inactive_account_gets_nothing(conn):
    user = await _user(conn, active=False)
    await push_repo.save(conn, await _session(conn, user), user, _endpoint(), "k", "a")
    assert await _endpoints(conn, user) == []


async def test_a_device_removes_only_its_own_subscription(conn):
    user = await _user(conn)
    mine, other = await _session(conn, user), await _session(conn, user)
    endpoint = _endpoint()
    await push_repo.save(conn, other, user, endpoint, "k", "a")
    assert await push_repo.delete_own(conn, mine, endpoint) is False
    assert await push_repo.delete_own(conn, other, endpoint) is True
    assert await _endpoints(conn, user) == []


async def test_results_and_which_sessions_have_push(conn):
    user = await _user(conn)
    session = await _session(conn, user)
    await push_repo.save(conn, session, user, _endpoint(), "k", "a")
    (sub,) = await push_repo.active_for_user(conn, user, **LIMITS)
    await push_repo.mark_failed(conn, sub["id"])
    await push_repo.mark_failed(conn, sub["id"])
    cur = await conn.execute("SELECT failures, last_ok_at FROM push_subscriptions WHERE id = %s",
                             (sub["id"],))
    assert (await cur.fetchone()) == (2, None)
    await push_repo.mark_ok(conn, sub["id"])
    cur = await conn.execute("SELECT failures, last_ok_at IS NOT NULL FROM push_subscriptions "
                             "WHERE id = %s", (sub["id"],))
    assert (await cur.fetchone()) == (0, True)
    assert session in await push_repo.session_ids_with_push(conn)
    await push_repo.delete(conn, sub["id"])
    assert session not in await push_repo.session_ids_with_push(conn)


async def test_a_failing_lookup_never_breaks_the_customers_transaction(conn, monkeypatch):
    """The turn's own work in the same transaction survives a push lookup that
    fails (a savepoint), and the turn goes on without devices."""
    from core.config import settings
    from core.services import push

    monkeypatch.setattr(settings, "push_vapid_public_key", "pub")
    monkeypatch.setattr(settings, "push_vapid_private_key", "priv")
    monkeypatch.setattr(settings, "push_vapid_subject", "mailto:team@getconverge.me")
    user = await _user(conn)

    async def broken(conn, user_id, **limits):
        await conn.execute("SELECT 1/0")

    monkeypatch.setattr(push_repo, "active_for_user", broken)
    assert await push.recipients(conn, user) == []
    cur = await conn.execute("SELECT count(*) FROM panel_users WHERE id = %s", (user,))
    assert (await cur.fetchone())[0] == 1


async def test_the_whole_team_with_a_live_session(conn):
    ana, bia, off = await _user(conn), await _user(conn), await _user(conn, active=False)
    live_a, live_b, dead, inactive = _endpoint(), _endpoint(), _endpoint(), _endpoint()
    await push_repo.save(conn, await _session(conn, ana), ana, live_a, "k", "a")
    await push_repo.save(conn, await _session(conn, bia), bia, live_b, "k", "a")
    await push_repo.save(conn, await _session(conn, bia, kind="browser", expires="-1 minute"), bia,
                         dead, "k", "a")
    await push_repo.save(conn, await _session(conn, off), off, inactive, "k", "a")
    found = {s["endpoint"] for s in await push_repo.active_for_all(conn, **LIMITS)}
    assert {live_a, live_b} <= found and not {dead, inactive} & found


async def test_the_teams_own_chat_is_known_unless_in_test(conn):
    chat = str(9 * 10**9 + uuid.uuid4().int % 10**8)
    await conn.execute("INSERT INTO staff (chat_id, name) VALUES (%s, 'Equipe')", (chat,))
    assert await push_repo.is_team_chat(conn, chat) is True
    assert await push_repo.is_team_chat(conn, chat + "1") is False
    await conn.execute("INSERT INTO contacts (chat_id, test_mode) VALUES (%s, 'novo')", (chat,))
    assert await push_repo.is_team_chat(conn, chat) is False
