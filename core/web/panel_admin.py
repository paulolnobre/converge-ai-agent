"""Authenticated room routes. The shared panel dependency enforces MFA and origin."""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.config import settings
from core.repositories import admin_room
from core.security import RateLimiter
from core.services import admin_commands, push
from core.services.availability import status_text

_limiter = RateLimiter(max_events=30, window_seconds=60)


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    nonce: UUID


def router(require_session, get_pool, private) -> APIRouter:
    routes = APIRouter(prefix='/api/admin', dependencies=[Depends(require_session)])

    @routes.get('/access')
    async def access(user: dict = Depends(require_session)):
        pool = await get_pool()
        async with pool.connection() as conn:
            allowed = await admin_room.is_member(conn, user['user_id'])
        return private({'allowed': allowed})

    @routes.get('')
    async def read(after: int = Query(0, ge=0, le=2**63 - 1), before: int | None = Query(None, gt=0, le=2**63 - 1),
                   user: dict = Depends(require_session)):
        pool = await get_pool()
        try:
            async with pool.connection() as conn:
                rows = await admin_room.messages(conn, user['user_id'], after=after, before=before)
                people = await admin_room.members(conn)
                state = await admin_room.health(conn)
        except admin_room.NotMember:
            raise HTTPException(403, 'Você não participa do grupo administrativo.') from None
        return private({'messages': rows, 'members': people,
                        'status': status_text(state, datetime.now(UTC)), 'user_id': user['user_id'], 'uploads_enabled': settings.panel_uploads_enabled})

    @routes.post('')
    async def post(body: Message, user: dict = Depends(require_session)):
        text = body.text.strip()
        if not text:
            raise HTTPException(422, 'Escreva uma mensagem antes de enviar.')
        pool = await get_pool()
        try:
            async with pool.connection() as conn:
                await admin_room.require_member(conn, user['user_id'])
                if not _limiter.check(user['user_id'])[0]:
                    raise HTTPException(429, 'Muitas mensagens. Aguarde um minuto e tente de novo.')
                message_id = await admin_room.insert(conn, text, sender_id=user['user_id'],
                                                     kind='person', nonce=body.nonce)
                if message_id:
                    await admin_commands.handle(conn, user, text, message_id)
                subs = await admin_room.subscriptions(conn, except_user=user['user_id']) if message_id else []
        except admin_room.NotMember:
            raise HTTPException(403, 'Você não participa do grupo administrativo.') from None
        if message_id:
            # No message contents or names on a device's lock screen.
            push.schedule(subs, title='💬 Nova mensagem na Administração',
                          url='/painel/#admin', tag=f'admin:{message_id}')
        return private({'ok': True, 'duplicate': message_id is None})

    return routes
