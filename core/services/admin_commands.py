"""Explicit room commands. Reuse panel services; never interpret chat as code.

Only an authenticated room member can ask. Changes are previewed and confirmed
by the same account within five minutes, against the state it actually saw.
The source message nonce and the confirmation row each prevent a retry from
executing an operation again.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from core.config import settings
from core.repositories import admin_commands as ledger
from core.repositories import admin_room, briefing, handoffs
from core.repositories import inbox as inbox_repo
from core.repositories.contacts import hidden_from_customer_lists_sql
from core.services import calendar_sync, inbox, secretarial
from core.services.availability import status_text

logger = logging.getLogger(__name__)

# Authorization names describe operations, independent of their chat spelling.
_COMMANDS = {
    '/status': ('operation.status', 0), '/ajuda': ('command.help', 0),
    '/comandos': ('command.help', 0), '/painel': ('panel.link', 0),
    '/resumo': ('operation.briefing', 0), '/pendencias': ('inbox.waiting', 0),
    '/agenda': ('agenda.read', None), '/sincronizar': ('agenda.sync', 0),
    '/assumir': ('inbox.claim', 1), '/devolver': ('inbox.release', 1),
    '/confirmar': ('command.confirm', 1), '/cancelar': ('command.cancel', 1),
}
_ACTIONS = frozenset(action for action, _ in _COMMANDS.values())
_CHANGES = frozenset({'agenda.sync', 'inbox.claim', 'inbox.release'})
HELP = (
    'Comandos da Administração:\n'
    '/status: disponibilidade do bot\n'
    '/resumo: resumo da operação, como no grupo do Telegram\n'
    '/agenda [AAAA-MM-DD]: agendamentos do bot no dia\n'
    '/pendencias: conversas esperando uma pessoa, com seus IDs\n'
    '/assumir ID: assumir uma conversa livre\n'
    '/devolver ID: devolver ao bot uma conversa que está com você\n'
    '/sincronizar: solicitar atualização dos calendários\n'
    '/painel: endereço do app\n\n'
    'Alterações pedem /confirmar CÓDIGO. Use /cancelar CÓDIGO para desistir. '
    'Só quem pediu pode confirmar ou cancelar; o pedido vale por 5 minutos.'
)


async def authorize(conn, user: dict, action: str) -> None:
    if action not in _ACTIONS:
        raise PermissionError('Unknown administrative action')
    await admin_room.require_member(conn, user['user_id'])


def _number(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 10:
        raise ValueError
    result = int(value)
    if not 0 < result <= 2**31 - 1:
        raise ValueError
    return result


async def _snapshot(conn, contact_id: int, user: dict, *, lock: bool = False) -> dict:
    cur = await conn.execute(
        f"SELECT c.chat_id FROM contacts c WHERE c.id = %s AND NOT {hidden_from_customer_lists_sql('c.chat_id')}",
        (contact_id,))
    row = await cur.fetchone()
    if row is None:
        raise inbox.InboxError(404, 'Conversa não encontrada.')
    if lock:
        await handoffs.lock_chat(conn, row[0])
    found = await inbox_repo.conversation(conn, contact_id, user['user_id'])
    if found is None:
        raise inbox.InboxError(404, 'Conversa não encontrada.')
    return found


def _expected(found: dict) -> dict:
    return {key: found[key] for key in ('state', 'owner_user_id', 'session_id')}


async def _prepare(conn, user: dict, action: str, args: list[str], message_id: int) -> str:
    contact_id, expected = None, {}
    if action == 'agenda.sync':
        if not calendar_sync.calendar_ready():
            return 'O calendário ainda não está configurado. Confira a integração antes de sincronizar.'
        preview = 'Solicitar uma atualização dos calendários no app.'
    else:
        contact_id = _number(args[0])
        found = await _snapshot(conn, contact_id, user)
        if action == 'inbox.claim':
            if found['state'] == 'claimed':
                return ('Esta conversa já está com você.' if found['mine']
                        else 'Esta conversa está com outra pessoa. Não posso assumir no lugar dela.')
            preview = f"Assumir a conversa #{contact_id} ({found['name'] or 'Sem nome'}) para você."
        else:
            if found['state'] != 'claimed' or not found['mine']:
                return 'Somente quem assumiu a conversa pode devolvê-la ao bot.'
            preview = (f"Devolver a conversa #{contact_id} ({found['name'] or 'Sem nome'}) ao bot. "
                       'O cliente receberá o aviso de retorno conforme as regras do canal.')
        expected = _expected(found)
    token = await ledger.prepare(conn, message_id=message_id, user_id=user['user_id'],
                                action=action, contact_id=contact_id, expected=expected)
    return f'{preview}\n\nPara confirmar: /confirmar {token}\nPara desistir: /cancelar {token}'


class ConfirmationExpired(Exception):
    pass


async def _confirm(conn, user: dict, token: UUID, *, cancel: bool) -> str:
    request = await ledger.lock_own(conn, token, user['user_id'])
    if request is None:
        return 'Pedido não encontrado para a sua conta. Só quem pediu pode confirmar ou cancelar.'
    if request['status'] != 'pending':
        return 'Esse pedido já foi tratado. Para outra alteração, faça um novo pedido.'
    if request['expired']:
        await ledger.finish(conn, token, 'expired')
        return 'A confirmação expirou. Faça o pedido de novo para conferir o estado atual.'
    if cancel:
        await ledger.finish(conn, token, 'cancelled')
        return 'Pedido cancelado. Nenhuma alteração foi executada.'
    action = request['action']
    if action not in _CHANGES:
        raise PermissionError('Unrecognized pending action')
    await authorize(conn, user, action)
    try:
        # The existing service can reject after a partial DB write (a channel
        # failure, for example). Roll that write back before recording refusal.
        async with conn.transaction():
            if action == 'agenda.sync':
                if not calendar_sync.calendar_ready():
                    raise inbox.InboxError(409, 'O calendário não está configurado para sincronizar.')
                if not await ledger.still_current(conn, token):
                    raise ConfirmationExpired
                calendar_sync.request_sync()
                result = 'Solicitei a atualização dos calendários. Confira o resultado na aba Agenda.'
            else:
                contact_id = request['contact_id']
                found = await _snapshot(conn, contact_id, user, lock=True)
                if not await ledger.still_current(conn, token):
                    raise ConfirmationExpired
                if _expected(found) != request['expected']:
                    raise inbox.InboxError(409, 'Esta conversa mudou desde o pedido. Confira o Inbox e faça um novo pedido.')
                if action == 'inbox.claim':
                    await inbox.claim(conn, contact_id, user, seen_state=found['state'],
                                      seen_owner=found['owner_user_id'])
                    result = f'Conversa #{contact_id} assumida por você.'
                else:
                    await inbox.release(conn, contact_id, user)
                    result = f'Conversa #{contact_id} devolvida ao bot.'
    except ConfirmationExpired:
        await ledger.finish(conn, token, 'expired')
        return 'A confirmação expirou enquanto a conversa estava ocupada. Faça um novo pedido.'
    except inbox.InboxError as exc:
        await ledger.finish(conn, token, 'refused')
        return exc.detail
    await ledger.finish(conn, token, 'done')
    return result


async def _agenda(conn, args: list[str]) -> str:
    zone = ZoneInfo(settings.office_timezone)
    day = date.fromisoformat(args[0]) if args else datetime.now(zone).date()
    start = datetime.combine(day, time.min, tzinfo=zone)
    # Match /resumo: this lists the bot's confirmed bookings, not a claim to
    # have read every external calendar. The Agenda tab carries the full view.
    rows = await briefing.appointments_between(conn, start, start + timedelta(days=1))
    if not rows:
        return f'Nenhum agendamento do bot em {day:%d/%m/%Y}. A agenda completa fica na aba Agenda.'
    lines = [f'Agendamentos do bot em {day:%d/%m/%Y}:']
    for row in rows[:15]:
        when = row['starts_at'].astimezone(zone).strftime('%H:%M')
        lines.append(f"• {when}: {row['contact_name'] or 'Sem nome'}")
    if len(rows) > 15:
        lines.append(f'… e mais {len(rows) - 15}. Confira a aba Agenda.')
    return '\n'.join(lines)


async def _waiting(conn, user: dict) -> str:
    rows = await inbox_repo.list_conversations(conn, user_id=user['user_id'], state='waiting', limit=16)
    if not rows:
        return 'Nenhuma conversa está esperando uma pessoa agora.'
    lines = ['Conversas esperando uma pessoa:']
    lines.extend(f"• #{row['contact_id']}: {row['name'] or 'Sem nome'}" for row in rows[:15])
    if len(rows) > 15:
        lines.append('Há mais conversas no Inbox.')
    lines.append('Use /assumir ID para assumir uma delas.')
    return '\n'.join(lines)


async def handle(conn, user: dict, text: str, message_id: int) -> None:
    words = text.split()
    if words and words[0].casefold() == '@bot':
        words = words[1:]
        if not words:
            words = ['/ajuda']
    if not words or not words[0].startswith('/'):
        return
    head = words[0].casefold()
    entry = _COMMANDS.get(head)
    if entry is None:
        reply = 'Comando não disponível. Use /ajuda para ver os comandos da Administração.'
    else:
        action, count = entry
        await authorize(conn, user, action)
        args = words[1:]
        if (count is not None and len(args) != count) or (head == '/agenda' and len(args) > 1):
            reply = 'Os argumentos desse comando não estão corretos. Use /ajuda para conferir.'
        else:
            try:
                if action in _CHANGES:
                    reply = await _prepare(conn, user, action, args, message_id)
                elif action in ('command.confirm', 'command.cancel'):
                    reply = await _confirm(conn, user, UUID(args[0]), cancel=action == 'command.cancel')
                elif action == 'operation.status':
                    reply = status_text(await admin_room.health(conn), datetime.now(UTC))
                elif action == 'operation.briefing':
                    reply = await secretarial.daily_briefing(conn)
                elif action == 'agenda.read':
                    reply = await _agenda(conn, args)
                elif action == 'inbox.waiting':
                    reply = await _waiting(conn, user)
                elif action == 'panel.link':
                    reply = (settings.public_base_url or '').rstrip('/') + '/painel/'
                else:
                    reply = HELP
            except (ValueError, OverflowError):
                reply = 'Argumento inválido. Use um ID numérico, data AAAA-MM-DD ou o código mostrado no pedido.'
            except inbox.InboxError as exc:
                reply = exc.detail
    if len(reply) > 4000:
        reply = reply[:3900] + '\n\nResumo cortado. Confira os detalhes nas abas do painel.'
    await admin_room.insert(conn, reply)
