"""Commands through real panel auth and Postgres, with only transports stubbed."""
# ruff: noqa: F811
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from core.config import settings
from core.repositories import contacts, handoffs
from core.services import admin_commands, calendar_sync, inbox
from tests.test_admin_room_pg import URL, client, database, send  # noqa: F401

pytestmark = pytest.mark.skipif(not URL, reason='TEST_DATABASE_URL not set')


@pytest.fixture
async def operations(database, monkeypatch):
    sent, notified = [], []
    async def outbound(channel, chat_id, text):
        sent.append((channel, chat_id, text))
        return 'ok'
    async def notify(text, contact_id):
        notified.append(contact_id)
        return True
    monkeypatch.setattr(inbox, 'send', outbound)
    monkeypatch.setattr(inbox, 'notify_team', notify)
    async with database.connection() as conn:
        contact = await contacts.get_or_create(conn, '70000', 'Cliente de teste')
        assert contact.id is not None
    return contact, sent, notified


async def latest(client):
    response = await client.get('/painel/api/admin')
    assert response.status_code == 200
    return response.json()['messages'][-1]['text']


async def token_for(client, text):
    response = await send(client, text)
    assert response.status_code == 200
    text = await latest(client)
    match = re.search(r'/confirmar ([a-f0-9-]{36})', text)
    assert match, text
    return match.group(1)


async def state(database, chat_id):
    async with database.connection() as conn:
        return await handoffs.active_session(conn, chat_id)


async def claim(client, contact):
    token = await token_for(client, f'/assumir {contact.id}')
    assert (await send(client, '/confirmar ' + token)).status_code == 200
    return token


async def test_help_and_read_commands_run_without_telegram_or_a_model(client, database):
    for command, expected in [('/ajuda', '/assumir ID'), ('@bot', '/resumo'),
                              ('/resumo', 'Nenhuma'), ('/pendencias', 'Nenhuma conversa'),
                              ('/agenda 2026-10-29', '29/10/2026'), ('/painel', '/painel/')]:
        assert (await send(client, command)).status_code == 200
        assert expected.casefold() in (await latest(client)).casefold()
    async with database.connection() as conn:
        cur = await conn.execute('SELECT count(*) FROM admin_room_commands')
        assert await cur.fetchone() == (0,)


@pytest.mark.parametrize('text', ['/assumir 0', '/assumir 99999999999999999',
                                 '/assumir 1;drop', '/devolver', '/devolver 1 2',
                                 '/agenda 2026-99-99', '/agenda hoje amanha', '/agenda 9999-12-31',
                                 '/confirmar invalid', '/cancelar invalid', '/deploy'])
async def test_invalid_or_unknown_commands_do_not_create_an_operation(client, database, text):
    assert (await send(client, text)).status_code == 200
    async with database.connection() as conn:
        cur = await conn.execute('SELECT count(*) FROM admin_room_commands')
        assert await cur.fetchone() == (0,)


async def test_a_mention_inside_regular_conversation_does_not_execute_a_command(client, database):
    for text in ['você pode /assumir 7?', 'ontem usei /sincronizar', 'o cliente pediu /devolver 2']:
        await send(client, text)
    rows = (await client.get('/painel/api/admin')).json()['messages']
    assert len(rows) == 3 and all(row['kind'] == 'person' for row in rows)


async def test_preparing_claim_has_no_effect_and_a_lost_response_keeps_one_request(client, database, operations):
    contact, sent, notified = operations
    nonce = uuid.uuid4()
    await send(client, f'/assumir {contact.id}', nonce=nonce)
    await send(client, f'/assumir {contact.id}', nonce=nonce)
    assert await state(database, contact.chat_id) is None
    assert sent == [] and notified == []
    async with database.connection() as conn:
        cur = await conn.execute('SELECT user_id, action, contact_id, status FROM admin_room_commands')
        assert await cur.fetchall() == [(1, 'inbox.claim', contact.id, 'pending')]


async def test_only_the_requesting_account_can_confirm_and_a_request_runs_once(client, database, operations):
    contact, sent, notified = operations
    token = await token_for(client, f'/assumir {contact.id}')
    client.cookies.set('painel_sessao', 'synthetic-token-2')
    await send(client, '/confirmar ' + token)
    assert 'Só quem pediu' in await latest(client)
    assert await state(database, contact.chat_id) is None
    client.cookies.set('painel_sessao', 'synthetic-token-1')
    await send(client, '/confirmar ' + token)
    await send(client, '/confirmar ' + token)
    session = await state(database, contact.chat_id)
    assert session and session.claimed_by_user_id == 1
    assert len(notified) == 1 and sent == []
    assert 'já foi tratado' in await latest(client)


async def test_release_uses_the_existing_service_and_only_the_owner_can_request_it(client, database, operations):
    contact, sent, notified = operations
    await claim(client, contact)
    client.cookies.set('painel_sessao', 'synthetic-token-2')
    await send(client, f'/devolver {contact.id}')
    assert 'Somente quem assumiu' in await latest(client)
    client.cookies.set('painel_sessao', 'synthetic-token-1')
    token = await token_for(client, f'/devolver {contact.id}')
    assert sent == []
    await send(client, '/confirmar ' + token)
    assert await state(database, contact.chat_id) is None
    assert len(sent) == 1 and len(notified) == 2
    await send(client, '/confirmar ' + token)
    assert len(sent) == 1
    async with database.connection() as conn:
        cur = await conn.execute("SELECT action,status FROM admin_room_commands ORDER BY created_at")
        assert await cur.fetchall() == [('inbox.claim','done'),('inbox.release','done')]
        cur = await conn.execute("SELECT count(*) FROM sessions WHERE contact_id=%s AND intent='handoff'",(contact.id,))
        assert await cur.fetchone() == (1,)


async def test_a_conversation_owned_by_someone_else_is_never_taken_over(client, database, operations):
    contact, _, notified = operations
    token = await token_for(client, f'/assumir {contact.id}')
    async with database.connection() as conn:
        await inbox.claim(conn, contact.id, {'user_id':2, 'name':'Victor'}, seen_state='bot', seen_owner=None)
    await send(client, '/confirmar ' + token)
    assert 'mudou desde o pedido' in await latest(client)
    session = await state(database, contact.chat_id)
    assert session and session.claimed_by_user_id == 2 and len(notified) == 1
    await send(client, f'/assumir {contact.id}')
    assert 'Não posso assumir no lugar' in await latest(client)


async def test_expired_and_cancelled_requests_cannot_execute(client, database, operations):
    contact, sent, notified = operations
    old = await token_for(client, f'/assumir {contact.id}')
    async with database.connection() as conn:
        await conn.execute("UPDATE admin_room_commands SET expires_at=NOW()-interval '1 second' WHERE id=%s",(old,))
    await send(client, '/confirmar ' + old)
    assert 'expirou' in await latest(client)
    cancelled = await token_for(client, f'/assumir {contact.id}')
    await send(client, '/cancelar ' + cancelled)
    await send(client, '/confirmar ' + cancelled)
    assert await state(database, contact.chat_id) is None
    assert not sent and not notified
    async with database.connection() as conn:
        cur = await conn.execute('SELECT status FROM admin_room_commands ORDER BY created_at')
        assert await cur.fetchall() == [('expired',),('cancelled',)]


async def test_another_member_cannot_cancel_the_owners_request(client, database, operations):
    contact, _, _ = operations
    token = await token_for(client, f'/assumir {contact.id}')
    client.cookies.set('painel_sessao', 'synthetic-token-2')
    await send(client, '/cancelar ' + token)
    async with database.connection() as conn:
        cur = await conn.execute('SELECT status FROM admin_room_commands WHERE id=%s',(token,))
        assert await cur.fetchone() == ('pending',)


async def test_membership_revocation_refuses_a_previously_prepared_command(client, database, operations):
    contact, sent, notified = operations
    token = await token_for(client, f'/assumir {contact.id}')
    async with database.connection() as conn:
        await conn.execute('DELETE FROM admin_room_members WHERE user_id=1')
    assert (await send(client, '/confirmar ' + token)).status_code == 403
    assert await state(database, contact.chat_id) is None
    assert not sent and not notified


async def test_a_channel_refusal_rolls_back_release_before_recording_the_refusal(client, database, operations, monkeypatch):
    contact, _, _ = operations
    await claim(client, contact)
    token = await token_for(client, f'/devolver {contact.id}')
    async def fail(*args):
        return 'failed'
    monkeypatch.setattr(inbox, 'send', fail)
    assert (await send(client, '/confirmar ' + token)).status_code == 200
    session = await state(database, contact.chat_id)
    assert session and session.claimed_by_user_id == 1
    async with database.connection() as conn:
        cur = await conn.execute('SELECT status FROM admin_room_commands WHERE id=%s',(token,))
        assert await cur.fetchone() == ('refused',)
        cur = await conn.execute("SELECT count(*) FROM sessions WHERE contact_id=%s AND intent='handoff'",(contact.id,))
        assert await cur.fetchone() == (0,)


async def test_deleted_target_keeps_an_audit_and_is_refused_at_confirmation(client, database, operations):
    contact, sent, notified = operations
    token = await token_for(client, f'/assumir {contact.id}')
    async with database.connection() as conn:
        await conn.execute('DELETE FROM contacts WHERE id=%s',(contact.id,))
    await send(client, '/confirmar ' + token)
    assert 'não encontrada' in await latest(client)
    assert not sent and not notified
    async with database.connection() as conn:
        cur = await conn.execute('SELECT status,contact_id FROM admin_room_commands WHERE id=%s',(token,))
        assert await cur.fetchone() == ('refused',contact.id)


async def test_sync_is_confirmed_once_by_its_actor(client, database, monkeypatch):
    calls = []
    monkeypatch.setattr(calendar_sync, 'calendar_ready', lambda: True)
    monkeypatch.setattr(calendar_sync, 'request_sync', lambda: calls.append('wake'))
    token = await token_for(client, '/sincronizar')
    assert calls == []
    await send(client, '/confirmar ' + token)
    await send(client, '/confirmar ' + token)
    assert calls == ['wake']


async def test_a_hidden_staff_chat_is_not_an_admin_command_target(client, database, operations):
    contact, sent, notified = operations
    async with database.connection() as conn:
        await conn.execute("INSERT INTO staff (chat_id,name) VALUES (%s,'Staff')",(contact.chat_id,))
    await send(client, f'/assumir {contact.id}')
    assert 'não encontrada' in await latest(client)
    assert not sent and not notified


async def test_a_confirmation_expiring_while_it_waits_for_the_chat_is_refused(client, database, operations):
    contact, sent, notified = operations
    token = await token_for(client, f'/assumir {contact.id}')
    async with database.connection() as holder:
        await holder.execute("UPDATE admin_room_commands SET expires_at=clock_timestamp()+interval '0.3 seconds' WHERE id=%s",(token,))
        await holder.commit()
        await handoffs.lock_chat(holder, contact.chat_id)
        task = asyncio.create_task(send(client, '/confirmar ' + token))
        await asyncio.sleep(.5)
        assert not task.done()
        await holder.commit()
        assert (await asyncio.wait_for(task, 3)).status_code == 200
    assert 'expirou' in await latest(client)
    assert await state(database, contact.chat_id) is None
    assert not sent and not notified


async def test_simultaneous_confirmations_cannot_both_run(client, database, operations):
    contact, _, notified = operations
    token = await token_for(client, f'/assumir {contact.id}')
    one, two = await asyncio.gather(send(client, '/confirmar ' + token), send(client, '/confirmar ' + token))
    assert one.status_code == two.status_code == 200
    assert len(notified) == 1
    async with database.connection() as conn:
        cur = await conn.execute('SELECT status FROM admin_room_commands WHERE id=%s',(token,))
        assert await cur.fetchone() == ('done',)


async def test_briefing_and_agenda_use_real_encrypted_customer_data(client, database, operations):
    contact, _, _ = operations
    now = datetime.now(UTC)
    async with database.connection() as conn:
        await conn.execute("INSERT INTO appointments (contact_id,calendar_id,provider,title,starts_at,ends_at) "
                           "VALUES (%s,'synthetic','google','',%s,%s)",
                           (contact.id, now, now+timedelta(minutes=30)))
    await send(client, '/resumo')
    assert 'Cliente de teste' in await latest(client)
    from zoneinfo import ZoneInfo
    day = now.astimezone(ZoneInfo(settings.office_timezone)).date().isoformat()
    await send(client, '/agenda '+day)
    assert 'Cliente de teste' in await latest(client)


async def test_authorization_uses_known_action_names_and_requires_membership(database):
    async with database.connection() as conn:
        with pytest.raises(PermissionError):
            await admin_commands.authorize(conn, {'user_id':1}, 'server.shell')
        from core.repositories.admin_room import NotMember
        with pytest.raises(NotMember):
            await admin_commands.authorize(conn, {'user_id':3}, 'inbox.claim')


async def test_replacing_the_handoff_requires_a_new_release_confirmation(client, database, operations):
    contact, sent, _ = operations
    await claim(client, contact)
    token = await token_for(client, f'/devolver {contact.id}')
    before = await state(database, contact.chat_id)
    assert before is not None
    async with database.connection() as conn:
        actor = {'user_id': 1, 'name': 'Paulo'}
        await inbox.release(conn, contact.id, actor)
        await inbox.claim(conn, contact.id, actor, seen_state='bot', seen_owner=None)
    replaced = await state(database, contact.chat_id)
    assert replaced is not None and replaced.id != before.id
    assert len(sent) == 1
    await send(client, '/confirmar ' + token)
    after = await state(database, contact.chat_id)
    assert after is not None and after.id == replaced.id and after.claimed_by_user_id == 1
    assert len(sent) == 1, 'the old confirmation must not return the replacement handoff'
    assert 'mudou desde o pedido' in await latest(client)
