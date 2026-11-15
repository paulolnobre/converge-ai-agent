"""Real Postgres, real panel sessions and synthetic push. No live provider calls."""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import LiteralString, cast

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from psycopg import sql

from cli import monitor
from core import crypto
from core.config import settings
from core.repositories import admin_room
from core.services import availability, panel_auth, push
from core.web import panel, panel_admin

URL = os.environ.get('TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not URL, reason='TEST_DATABASE_URL not set')


class Database:
    def __init__(self):
        self.schema = 'admintest_' + uuid.uuid4().hex[:12]

    @asynccontextmanager
    async def connection(self):
        conn = await psycopg.AsyncConnection.connect(URL or '')
        try:
            await conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
            await conn.commit()
            yield conn
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            await conn.close()


@pytest.fixture
async def database(monkeypatch):
    db = Database()
    async with db.connection() as conn:
        await conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(db.schema)))
        await conn.execute(cast(LiteralString, Path('infra/init.sql').read_text()))
        for path in sorted(Path('infra/migrations').glob('*.sql')):
            await conn.execute(cast(LiteralString, path.read_text()))
        await conn.execute("INSERT INTO panel_users (id, email, name) VALUES "
                           "(1, 'paulo@example.test', 'Paulo'), "
                           "(2, 'victor@example.test', 'Victor'), "
                           "(3, 'other@example.test', 'Outro')")
        await conn.execute('INSERT INTO admin_room_members (user_id) VALUES (1), (2)')
        for n in (1, 2, 3):
            await conn.execute("INSERT INTO panel_sessions "
                "(token_hash, user_id, mfa, expires_at, kind) VALUES (%s, %s, TRUE, NOW() + interval '1 hour', 'device')",
                (panel_auth.digest(f'synthetic-token-{n}'), n))

    async def pool():
        return db

    monkeypatch.setattr(panel, 'get_pool', pool)
    monkeypatch.setattr(monitor, 'get_pool', pool)
    monkeypatch.setattr(push, 'get_pool', pool)
    monkeypatch.setattr(settings, 'panel_secret_key', Fernet.generate_key().decode())
    panel_admin._limiter._events.clear()
    panel_admin._limiter._notified.clear()
    try:
        yield db
    finally:
        async with db.connection() as conn:
            await conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(db.schema)))


@pytest.fixture
async def client(database):
    app = FastAPI()
    panel.install(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://panel.test') as client:
        client.cookies.set(panel.SESSION_COOKIE, 'synthetic-token-1')
        yield client


async def send(client, text='Olá, equipe', nonce=None, **extra):
    return await client.post('/painel/api/admin', json={
        'text': text, 'nonce': str(nonce or uuid.uuid4()), **extra})


async def test_only_explicit_active_members_can_read_or_post(client, database):
    assert (await client.get('/painel/api/admin')).status_code == 200
    client.cookies.set(panel.SESSION_COOKIE, 'synthetic-token-3')
    assert (await client.get('/painel/api/admin/access')).json() == {'allowed': False}
    assert (await client.get('/painel/api/admin')).status_code == 403
    assert (await send(client)).status_code == 403
    client.cookies.clear()
    assert (await client.get('/painel/api/admin')).status_code == 401
    assert (await send(client)).status_code == 401


async def test_revoked_membership_is_checked_before_decryption(client, database, monkeypatch):
    assert (await send(client)).status_code == 200
    async with database.connection() as conn:
        await conn.execute('DELETE FROM admin_room_members WHERE user_id = 1')
    def refused(*args):
        pytest.fail('a refused caller must not decrypt a message')
    monkeypatch.setattr(crypto, 'open_sealed', refused)
    assert (await client.get('/painel/api/admin')).status_code == 403
    assert (await send(client)).status_code == 403


@pytest.mark.parametrize('change', ["UPDATE panel_users SET active = FALSE WHERE id = 1",
                                    "UPDATE panel_sessions SET mfa = FALSE WHERE user_id = 1",
                                    "DELETE FROM panel_sessions WHERE user_id = 1"])
async def test_deactivation_logout_or_missing_second_factor_refuses_access(client, database, change):
    async with database.connection() as conn:
        await conn.execute(cast(LiteralString, change))
    assert (await send(client)).status_code == 401
    assert (await client.get('/painel/api/admin')).status_code == 401


async def test_origin_length_and_sender_are_enforced(client, database):
    response = await client.post('/painel/api/admin', headers={'Origin': 'https://evil.test'},
                                json={'text': 'forged', 'nonce': str(uuid.uuid4())})
    assert response.status_code == 403
    assert (await send(client, ' ')).status_code == 422
    assert (await send(client, 'x' * 4001)).status_code == 422
    assert (await send(client, sender_id=2, user_id=2)).status_code == 200
    data = (await client.get('/painel/api/admin')).json()
    assert data['messages'][0]['sender_id'] == 1
    assert data['messages'][0]['sender'] == 'Paulo'


async def test_message_nonce_survives_retry_and_text_is_never_stored_in_clear(client, database):
    nonce = uuid.uuid4()
    assert (await send(client, '<img src=x onerror=alert(1)> segredo', nonce)).json()['duplicate'] is False
    assert (await send(client, '<img src=x onerror=alert(1)> segredo', nonce)).json()['duplicate'] is True
    data = (await client.get('/painel/api/admin')).json()
    assert len(data['messages']) == 1
    assert data['messages'][0]['text'] == '<img src=x onerror=alert(1)> segredo'
    async with database.connection() as conn:
        cur = await conn.execute('SELECT body_enc FROM admin_room_messages')
        [(sealed,)] = await cur.fetchall()
        assert b'segredo' not in bytes(sealed)


async def test_status_command_replies_once_and_uses_the_independent_observation(client, database):
    async with database.connection() as conn:
        await availability.observe(conn, healthy=True, now=datetime.now(UTC))
    nonce = uuid.uuid4()
    await send(client, '/status', nonce)
    await send(client, '/status', nonce)
    rows = (await client.get('/painel/api/admin')).json()['messages']
    assert len(rows) == 2
    assert rows[0]['kind'] == 'person' and rows[1]['kind'] == 'bot'
    assert 'pronto para atender' in rows[1]['text']


async def test_pagination_keeps_order_and_never_fetches_the_whole_history(client, database):
    async with database.connection() as conn:
        for n in range(112):
            await admin_room.insert(conn, f'message {n}')
    rows = (await client.get('/painel/api/admin')).json()['messages']
    assert len(rows) == 50 and rows[0]['text'] == 'message 62'
    older = (await client.get('/painel/api/admin', params={'before': rows[0]['id']})).json()['messages']
    assert len(older) == 50 and older[-1]['text'] == 'message 61'
    async with database.connection() as conn:
        await admin_room.insert(conn, 'latest')
    recent = (await client.get('/painel/api/admin', params={'after': rows[-1]['id']})).json()['messages']
    assert [row['text'] for row in recent] == ['latest']
    assert (await client.get('/painel/api/admin?after=999999999999999999999999999')).status_code == 422


async def test_membership_revocation_waits_for_a_post_to_commit(database):
    async with database.connection() as writer, database.connection() as revoker:
        await admin_room.insert(writer, 'last allowed', sender_id=1, kind='person', nonce=uuid.uuid4())
        task = asyncio.create_task(revoker.execute('DELETE FROM admin_room_members WHERE user_id = 1'))
        try:
            await asyncio.sleep(.05)
            assert not task.done()
            await writer.commit()
            await asyncio.wait_for(task, 2)
            await revoker.commit()
            with pytest.raises(admin_room.NotMember):
                await admin_room.insert(writer, 'refused', sender_id=1, kind='person', nonce=uuid.uuid4())
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_three_minutes_down_then_one_recovery_persist_across_restarts(database):
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    async def observe(second, ready):
        async with database.connection() as conn:
            return await availability.observe(conn, healthy=ready, now=now + timedelta(seconds=second))
    assert await observe(0, False) is None
    assert await observe(60, False) is None
    assert await observe(120, False) is None
    assert await observe(180, False) == availability.DOWN
    assert await observe(240, False) is None
    assert await observe(300, True) == availability.UP
    assert await observe(360, True) is None
    async with database.connection() as conn:
        cur = await conn.execute('SELECT title FROM admin_alert_outbox ORDER BY message_id')
        assert await cur.fetchall() == [(availability.DOWN,), (availability.UP,)]
        assert len(await admin_room.messages(conn, 1)) == 2


async def test_a_failed_sample_gap_is_not_claimed_to_be_three_observed_minutes(database):
    now = datetime.now(UTC)
    async with database.connection() as conn:
        await availability.observe(conn, healthy=False, now=now)
        assert await availability.observe(conn, healthy=False, now=now + timedelta(minutes=20)) is None


async def test_recovery_before_threshold_sends_nothing_and_a_new_incident_has_its_own_key(database):
    now = datetime.now(UTC)
    async with database.connection() as conn:
        for offset, healthy in [(0, False), (60, True), (120, False), (180, False),
                                (240, False), (300, False), (360, True), (420, False),
                                (480, False), (540, False), (600, False)]:
            await availability.observe(conn, healthy=healthy, now=now+timedelta(seconds=offset))
        cur = await conn.execute('SELECT event_key FROM admin_room_messages ORDER BY id')
        assert await cur.fetchall() == [('availability:1:down',), ('availability:1:up',),
                                       ('availability:2:down',)]


@pytest.mark.parametrize('response,expected', [
    (httpx.Response(200, json={'status': 'ready'}), True),
    (httpx.Response(503), False), (httpx.Response(302, headers={'location':'https://evil.test'}), False),
    (httpx.Response(200, json={'status': 'unavailable'}), False),
    (httpx.Response(200, json=None), False), (httpx.Response(200, text='broken'), False),
    (httpx.Response(200, content=b'x' * 70000), False),
])
async def test_readiness_probe_checks_the_status_and_refuses_redirects(response, expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        assert await monitor.probe(client) is expected


async def test_probe_treats_unreachable_bot_as_down():
    def down(request):
        raise httpx.ConnectError('synthetic outage')
    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
        assert await monitor.probe(client) is False


async def _subscription(conn, user_id, key):
    cur = await conn.execute('SELECT id FROM panel_sessions WHERE user_id = %s', (user_id,))
    session = (await cur.fetchone())[0]
    cur = await conn.execute("INSERT INTO push_subscriptions (session_id, user_id, endpoint, p256dh, auth) "
        "VALUES (%s, %s, %s, 'synthetic', 'synthetic') RETURNING id",
        (session, user_id, f'https://fcm.googleapis.com/fcm/send/{key}'))
    return (await cur.fetchone())[0]


async def test_only_group_members_get_push_and_a_revoked_session_is_not_reused(database):
    async with database.connection() as conn:
        mine = await _subscription(conn, 1, 'a')
        await _subscription(conn, 3, 'c')
        assert [sub['id'] for sub in await admin_room.subscriptions(conn)] == [mine]
        assert await admin_room.subscriptions(conn, except_user=1) == []
        await conn.execute('DELETE FROM panel_sessions WHERE user_id = 1')
        assert await admin_room.subscriptions(conn) == []


async def test_monitor_delivers_without_a_bot_and_retries_only_failed_recipients(database, monkeypatch):
    async with database.connection() as conn:
        first = await _subscription(conn, 1, 'a')
        second = await _subscription(conn, 2, 'b')
        await _subscription(conn, 3, 'c')
        n = await admin_room.insert(conn, availability.DOWN, kind='alert', event_key='test')
        await conn.execute('INSERT INTO admin_alert_outbox (message_id,title) VALUES (%s,%s)',
                           (n, availability.DOWN))
    calls = []
    async def send_now(sub, **kwargs):
        calls.append(sub['id'])
        assert kwargs['url'] == '/painel/#admin'
        return sub['id'] != second or calls.count(second) > 1
    monkeypatch.setattr(push, 'send_now', send_now)
    monkeypatch.setattr(push.push_endpoint, 'push_configured', lambda: True)
    await monitor.deliver()
    assert calls == [first, second]
    await monitor.deliver()
    assert calls == [first, second, second]
    await monitor.deliver()
    assert calls == [first, second, second]


async def test_rate_limit_refuses_an_administrative_message_flood(client):
    for _ in range(30):
        assert (await send(client)).status_code == 200
    assert (await send(client)).status_code == 429


async def test_key_rotation_keeps_the_room_readable_without_the_old_key(database, monkeypatch):
    import base64

    from core.services import sealing

    async with database.connection() as conn:
        await admin_room.insert(conn, 'a message that survives rotation')
        old = settings.data_keys
        new = '2:' + base64.b64encode(bytes(range(32, 64))).decode()
        monkeypatch.setattr(settings, 'data_keys', new + ',' + old)
        crypto.reset_keys()
        assert await sealing.rekey(conn) == 1
        monkeypatch.setattr(settings, 'data_keys', new)
        crypto.reset_keys()
        assert (await admin_room.messages(conn, 1))[0]['text'] == 'a message that survives rotation'
        problems = await sealing.check(conn, key_version=2, include_locks=False)
        assert not [p for p in problems if p.table == 'admin_room_messages']
        await conn.execute("UPDATE admin_room_messages SET body_enc = decode('010203', 'hex')")
        problems = await sealing.check(conn, key_version=2, include_locks=False)
        assert [p.reason for p in problems if p.table == 'admin_room_messages'] == ['envelope não abre']


async def test_failure_to_commit_observation_leaves_no_half_published_alert(database):
    now = datetime.now(UTC)
    async with database.connection() as conn:
        for n in (0, 60, 120):
            await availability.observe(conn, healthy=False, now=now + timedelta(seconds=n))
    with pytest.raises(RuntimeError):
        async with database.connection() as conn:
            assert await availability.observe(conn, healthy=False, now=now + timedelta(seconds=180))
            raise RuntimeError('simulated rollback')
    async with database.connection() as conn:
        assert await admin_room.messages(conn, 1) == []
        assert await availability.observe(conn, healthy=False, now=now + timedelta(seconds=180))
        cur = await conn.execute('SELECT count(*) FROM admin_alert_outbox')
        assert await cur.fetchone() == (1,)


async def test_two_monitors_record_one_incident(database):
    now = datetime.now(UTC)
    for n in (0, 60, 120):
        async with database.connection() as conn:
            await availability.observe(conn, healthy=False, now=now + timedelta(seconds=n))
    async def observe():
        async with database.connection() as conn:
            return await availability.observe(conn, healthy=False, now=now + timedelta(seconds=180))
    events = await asyncio.gather(observe(), observe())
    assert events.count(availability.DOWN) == 1
    async with database.connection() as conn:
        cur = await conn.execute('SELECT count(*) FROM admin_alert_outbox')
        assert await cur.fetchone() == (1,)


async def test_full_monitor_tick_sends_an_encrypted_push_with_the_bot_unreachable(database, monkeypatch):
    from tests.test_push_send import Device, _vapid_keys

    public, private = _vapid_keys()
    monkeypatch.setattr(settings, 'push_vapid_public_key', public)
    monkeypatch.setattr(settings, 'push_vapid_private_key', private)
    monkeypatch.setattr(settings, 'push_vapid_subject', 'mailto:synthetic@example.test')
    device = Device()
    async with database.connection() as conn:
        subscription = await _subscription(conn, 1, 'full-flow')
        await conn.execute('UPDATE push_subscriptions SET p256dh=%s, auth=%s WHERE id=%s',
                           (device.sub['p256dh'], device.sub['auth'], subscription))
    delivered = []
    def accept(request):
        delivered.append(device.read(request.content))
        assert request.headers['authorization'].startswith('vapid t=')
        return httpx.Response(201)
    monkeypatch.setattr(push, '_transport', httpx.MockTransport(accept))
    now = datetime.now(UTC)
    class Clock:
        @staticmethod
        def now(zone):
            return now
    monkeypatch.setattr(monitor, 'datetime', Clock)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        for second in (0, 60, 120, 180):
            now = now.replace(microsecond=0) if second == 0 else now + timedelta(minutes=1)
            await monitor.tick(client)
    assert [row['title'] for row in delivered] == [availability.DOWN]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'status':'ready'}))) as client:
        now += timedelta(minutes=1)
        await monitor.tick(client)
    assert [row['title'] for row in delivered] == [availability.DOWN, availability.UP]
    assert all(row['url'] == '/painel/#admin' for row in delivered)
    assert len({row['tag'] for row in delivered}) == 2
    async with database.connection() as conn:
        cur = await conn.execute('SELECT push_done, telegram_done FROM admin_alert_outbox')
        assert await cur.fetchall() == [(True, True), (True, True)]


async def test_slow_push_never_holds_the_next_readiness_observation(database, monkeypatch):
    async def slow():
        await asyncio.sleep(10)
    monkeypatch.setattr(monitor, 'deliver', slow)
    monkeypatch.setattr(monitor, 'DELIVERY_SECONDS', .01)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'status':'ready'}))) as client:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(monitor.tick(client), .5)
    async with database.connection() as conn:
        state = await admin_room.health(conn)
        assert state is not None and state['healthy'] is True


async def test_telegram_failure_retries_without_resending_successful_push(database, monkeypatch):
    async with database.connection() as conn:
        n = await admin_room.insert(conn, availability.DOWN, kind='alert')
        await conn.execute('INSERT INTO admin_alert_outbox (message_id,title) VALUES (%s,%s)',
                           (n, availability.DOWN))
    sent = []
    async def send(chat_id, text, **kwargs):
        sent.append((chat_id, text))
        return len(sent) > 1
    monkeypatch.setattr(settings, 'team_chat_id', '-10000000001')
    monkeypatch.setattr(settings, 'telegram_bot_token', 'synthetic-test-token')
    monkeypatch.setattr(monitor.telegram, 'send_message', send)
    await monitor.deliver()
    await monitor.deliver()
    await monitor.deliver()
    assert sent == [('-10000000001', availability.DOWN)] * 2


async def test_system_alerts_go_to_the_admin_chat_when_there_is_one(database, monkeypatch):
    async with database.connection() as conn:
        n = await admin_room.insert(conn, availability.DOWN, kind='alert')
        await conn.execute('INSERT INTO admin_alert_outbox (message_id,title) VALUES (%s,%s)',
                           (n, availability.DOWN))
    sent = []
    async def send(chat_id, text, **kwargs):
        sent.append(chat_id)
        return True
    monkeypatch.setattr(settings, 'team_chat_id', '-10000000001')
    monkeypatch.setattr(settings, 'admin_alert_chat_id', '-10000000002')
    monkeypatch.setattr(settings, 'telegram_bot_token', 'synthetic-test-token')
    monkeypatch.setattr(monitor.telegram, 'send_message', send)
    await monitor.deliver()
    assert sent == ['-10000000002'], 'the admins, not the team group'


async def test_old_delivery_attempts_do_not_replace_the_current_status(database, monkeypatch):
    async with database.connection() as conn:
        n = await admin_room.insert(conn, availability.DOWN, kind='alert')
        await conn.execute("INSERT INTO admin_alert_outbox (message_id,title,created_at) "
                           "VALUES (%s,%s,NOW()-interval '16 minutes')", (n, availability.DOWN))
    async def refused(*args, **kwargs):
        pytest.fail('old incident must not be pushed as a fresh outage')
    monkeypatch.setattr(push, 'send_now', refused)
    monkeypatch.setattr(monitor.telegram, 'send_message', refused)
    await monitor.deliver()
    async with database.connection() as conn:
        cur = await conn.execute('SELECT push_done, telegram_done FROM admin_alert_outbox')
        assert await cur.fetchall() == [(False, False)], 'an old outbox item must not be processed'
