"""What the morning briefing reads. All of it is already in the database — the
briefing adds no state of its own beyond the day it went out."""
from __future__ import annotations

from datetime import date, datetime

from psycopg.rows import dict_row

from core.repositories.contacts import left_out_of_metrics_sql
from core.sealed_fields import field, open_dict, pair_sql


async def claim_day(conn, day: date) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO briefings (day) VALUES (%s) ON CONFLICT (day) DO NOTHING", (day,)
        )
        return cur.rowcount > 0


async def last_sent_before(conn, day: date) -> datetime | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(sent_at) FROM briefings WHERE day < %s", (day,))
        row = await cur.fetchone()
    return row[0] if row else None


async def appointments_between(conn, start: datetime, end: datetime) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT a.starts_at, {pair_sql('c', _NAME, 'contact_name')}, "
            "       a.attendance_confirmed_at IS NOT NULL AS confirmed "
            "FROM appointments a LEFT JOIN contacts c ON c.id = a.contact_id "
            "WHERE a.status = 'confirmed' AND a.starts_at >= %s AND a.starts_at < %s "
            f"  AND NOT {left_out_of_metrics_sql('c.chat_id')} "
            "ORDER BY a.starts_at",
            (start, end),
        )
        return _open(await cur.fetchall())


async def new_contacts_since(conn, since: datetime) -> list[dict]:
    """People who wrote for the first time — the team's own test chats excluded."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT c.id, {pair_sql('c', _NAME)}, c.stage FROM contacts c "
            "WHERE c.created_at >= %s "
            f"  AND NOT {left_out_of_metrics_sql('c.chat_id')} "
            "ORDER BY c.created_at",
            (since,),
        )
        return _open(await cur.fetchall())


async def inbound_since(conn, since: datetime) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM sessions s WHERE s.role = 'user' AND s.created_at >= %s "
            f"AND NOT {left_out_of_metrics_sql('s.chat_id')}",
            (since,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def open_handoffs(conn) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT h.id, {pair_sql('c', _NAME, 'contact_name')}, h.claimed_by_name "
            "FROM handoff_sessions h LEFT JOIN contacts c ON c.id = h.contact_id "
            "WHERE h.status IN ('open', 'claimed') "
            f"  AND NOT {left_out_of_metrics_sql('c.chat_id')} "
            "ORDER BY h.created_at"
        )
        return _open(await cur.fetchall())


async def unrecorded_outcomes(conn) -> int:
    """Meetings the team was asked about and has not answered yet."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM appointments a LEFT JOIN contacts c ON c.id = a.contact_id "
            "WHERE a.status = 'confirmed' AND a.outcome_asked_at IS NOT NULL AND a.ends_at < NOW() "
            f"  AND NOT {left_out_of_metrics_sql('c.chat_id')}"
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def stale_proposals(conn, days: int) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT c.id, {pair_sql('c', _NAME)}, c.stage_changed_at FROM contacts c "
            "WHERE c.stage = 'proposta' AND c.stage_changed_at < NOW() - make_interval(days => %s) "
            f"  AND NOT {left_out_of_metrics_sql('c.chat_id')} "
            "ORDER BY stage_changed_at",
            (days,),
        )
        return _open(await cur.fetchall())


async def silent_leads(conn, days: int, max_days: int = 30) -> list[dict]:
    """Open leads where we spoke last and they went quiet, `days` to `max_days` ago.

    Left out: anyone with a meeting ahead or a person already talking to them
    (nothing to chase), and the team's own chats. Past `max_days` a lead is not
    a follow-up any more but a decision — "perdido" — for a person in the panel.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT c.id, {pair_sql('c', _NAME)}, c.stage, last.created_at AS last_at
              FROM contacts c
              JOIN LATERAL (
                   SELECT role, created_at FROM sessions s
                    WHERE s.contact_id = c.id ORDER BY s.id DESC LIMIT 1
              ) last ON true
             WHERE c.stage IN ('respondido', 'proposta')
               AND last.role = 'assistant'
               AND last.created_at < NOW() - make_interval(days => %s)
               AND last.created_at > NOW() - make_interval(days => %s)
               AND NOT {left_out_of_metrics_sql('c.chat_id')}
               AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.contact_id = c.id
                                AND a.status = 'confirmed' AND a.starts_at > NOW())
               AND NOT EXISTS (SELECT 1 FROM handoff_sessions h WHERE h.contact_id = c.id
                                AND h.status IN ('open', 'claimed'))
             ORDER BY last.created_at
            """,
            (days, max_days),
        )
        return _open(await cur.fetchall())


_NAME = field("contacts", "name")


def _open(rows) -> list[dict]:
    """Each row's contact name, by whichever key the query gave it."""
    opened = []
    for row in rows:
        key = "contact_name" if "contact_name" in row else "name"
        opened.append(open_dict(row, ((_NAME, key),)))
    return opened
