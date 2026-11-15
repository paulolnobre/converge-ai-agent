"""Availability observations, made by a process outside the bot.

Persist the transition and its outbox together. No network call holds the row
lock; restarts retain the current incident, retries retain its notification tag.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from core.repositories import admin_room

DOWN_AFTER = timedelta(minutes=3)
MAX_SAMPLE_GAP = timedelta(seconds=125)
DOWN = '🔴 O bot está fora do ar ou sem condições de atender há pelo menos 3 minutos.'
UP = '🟢 O bot voltou e está pronto para atender.'


async def observe(conn, *, healthy: bool, now: datetime) -> str | None:
    await conn.execute(
        "INSERT INTO admin_monitor_state (name, checked_at, healthy) VALUES ('bot', %s, %s) "
        "ON CONFLICT DO NOTHING", (now, healthy))
    cur = await conn.execute(
        "SELECT checked_at, failed_since, announced, generation FROM admin_monitor_state "
        "WHERE name = 'bot' FOR UPDATE")
    row = await cur.fetchone()
    assert row is not None
    checked, since, announced, generation = row
    if now < checked:
        return None
    text = None
    if healthy:
        if announced:
            text = UP
        since, announced = None, False
    else:
        if since is None or (now - checked > MAX_SAMPLE_GAP and not announced):
            since = now
        if not announced and now - since >= DOWN_AFTER:
            generation += 1
            announced = True
            text = DOWN
    await conn.execute(
        "UPDATE admin_monitor_state SET checked_at = %s, healthy = %s, failed_since = %s, "
        "announced = %s, generation = %s WHERE name = 'bot'",
        (now, healthy, since, announced, generation))
    if text:
        key = f"availability:{generation}:{'up' if healthy else 'down'}"
        message_id = await admin_room.insert(conn, text, kind='alert', event_key=key)
        if message_id is not None:
            await conn.execute("INSERT INTO admin_alert_outbox (message_id, title) VALUES (%s, %s)",
                               (message_id, text))
    return text


def status_text(state: dict | None, now: datetime) -> str:
    if state is None or now - state['checked_at'] > MAX_SAMPLE_GAP:
        return 'Ainda não tenho uma verificação recente do monitor. Confira se ele está ativo.'
    if state['healthy']:
        return '🟢 O bot está pronto para atender. Última verificação há menos de 2 minutos.'
    return '🔴 O bot está fora do ar ou sem condições de atender. O monitor continua verificando.'
