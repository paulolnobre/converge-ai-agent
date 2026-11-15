"""Independent availability monitor. Runs in its own container, never in the bot.

Checks /ready each minute; persists incidents and queues delivery in Postgres.
No Docker socket, customer data, OpenAI key or bot process is needed for push.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx
from psycopg.rows import dict_row

from core.config import settings
from core.integrations import telegram
from core.repositories import admin_room
from core.repositories.connection import close_pool, get_pool
from core.services import availability, push, push_endpoint

logger = logging.getLogger(__name__)
READY_URL = 'http://reception-bot:8000/ready'
INTERVAL = 60
DELIVERY_SECONDS = 40


async def probe(client: httpx.AsyncClient) -> bool:
    try:
        async with asyncio.timeout(10):
            async with client.stream('GET', READY_URL, follow_redirects=False) as response:
                if response.status_code != 200:
                    return False
                data = bytearray()
                async for block in response.aiter_bytes():
                    data.extend(block)
                    if len(data) > 64 * 1024:
                        return False
                import json
                value = json.loads(data)
                return isinstance(value, dict) and value.get('status') == 'ready'
    except (httpx.HTTPError, TimeoutError, ValueError):
        return False


async def deliver() -> None:
    pool = await get_pool()
    # One monitor dispatcher at a time, across restarts or accidental replicas.
    async with pool.connection() as lease:
        cur = await lease.execute('SELECT pg_try_advisory_lock(7411204)')
        locked = await cur.fetchone()
        if not locked or not locked[0]:
            return
        await lease.commit()
        try:
            async with lease.cursor(row_factory=dict_row) as cur:
                await cur.execute('SELECT message_id, title, push_done, telegram_done '
                                  'FROM admin_alert_outbox '
                                  "WHERE (NOT push_done OR NOT telegram_done) AND created_at > NOW() - INTERVAL '15 minutes' "
                                  'ORDER BY message_id LIMIT 10')
                alerts = list(await cur.fetchall())
            await lease.commit()
            for alert in alerts:
                async with pool.connection() as conn:
                    subs = await admin_room.subscriptions(conn)
                    cur = await conn.execute('SELECT subscription_id FROM admin_alert_deliveries '
                                             'WHERE message_id = %s', (alert['message_id'],))
                    done = {row[0] for row in await cur.fetchall()}
                push_ok = alert['push_done'] or not push_endpoint.push_configured()
                if not push_ok and push_endpoint.push_configured():
                    push_ok = True
                    for sub in subs:
                        if sub['id'] in done:
                            continue
                        accepted = await push.send_now(sub, title=alert['title'],
                            url='/painel/#admin', tag=f"alert:{alert['message_id']}")
                        if accepted:
                            async with pool.connection() as conn:
                                # A revoked subscription can disappear during delivery.
                                await conn.execute('INSERT INTO admin_alert_deliveries '
                                    '(message_id, subscription_id) SELECT %s, id FROM push_subscriptions '
                                    'WHERE id = %s ON CONFLICT DO NOTHING',
                                    (alert['message_id'], sub['id']))
                        else:
                            push_ok = False
                telegram_ok = alert['telegram_done']
                if not telegram_ok:
                    chat = settings.admin_alert_chat_id or settings.team_chat_id
                    if chat and settings.telegram_bot_token:
                        telegram_ok = bool(await telegram.send_message(chat, alert['title'],
                                                                       parse_mode=None))
                    else:
                        telegram_ok = True
                async with pool.connection() as conn:
                    await conn.execute('UPDATE admin_alert_outbox SET push_done = %s, telegram_done = %s '
                                       'WHERE message_id = %s', (push_ok, telegram_ok, alert['message_id']))
        finally:
            await lease.execute('SELECT pg_advisory_unlock(7411204)')
            await lease.commit()


async def tick(client: httpx.AsyncClient) -> None:
    healthy = await probe(client)
    pool = await get_pool()
    async with pool.connection() as conn:
        await availability.observe(conn, healthy=healthy, now=datetime.now(UTC))
    async with asyncio.timeout(DELIVERY_SECONDS):
        await deliver()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
            while True:
                started = asyncio.get_running_loop().time()
                try:
                    await tick(client)
                except Exception as exc:
                    # No URLs, tokens, response bodies or private messages in logs.
                    logger.error('Availability check failed (%s)', type(exc).__name__)
                await asyncio.sleep(max(1, INTERVAL - (asyncio.get_running_loop().time() - started)))
    finally:
        await close_pool()
        await telegram.close()


if __name__ == '__main__':
    asyncio.run(main())
