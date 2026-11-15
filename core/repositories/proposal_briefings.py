"""The "proposal by e-mail" questionnaires (migration 0023).

The answers are one JSON object, sealed at rest. A contact has at most one
active questionnaire: status 'open' or 'incomplete_sent'.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Literal

from psycopg.rows import dict_row

from core.models import ProposalBriefing
from core.repositories.contacts import hidden_from_customer_lists_sql
from core.sealed_fields import field, read, write

_ANSWERS = field("proposal_briefings", "answers")
_COLUMNS = ("id, contact_id, status, step, answers, answers_sealed, prefilled, created_at, "
            "updated_at, submitted_at, notified_at, incomplete_notified_at, "
            "proposal_sent_at, proposal_sent_by")
_ACTIVE = "('open', 'incomplete_sent')"
_NOTICE_COLUMN = {"sent": "notified_at", "incomplete": "incomplete_notified_at"}


def _model(row: dict) -> ProposalBriefing:
    text = read(_ANSWERS, row.pop("answers"), row.pop("answers_sealed"), row["id"])
    return ProposalBriefing(**row, answers=json.loads(text) if text else {})


def _sealed(answers: dict[str, str]) -> dict[str, object]:
    return write(_ANSWERS, json.dumps(answers, ensure_ascii=False))


async def _one(conn, where: str, params: tuple) -> ProposalBriefing | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {_COLUMNS} FROM proposal_briefings WHERE {where}", params)
        row = await cur.fetchone()
    return _model(row) if row else None


async def _many(conn, where: str, params: tuple, order: str) -> list[ProposalBriefing]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM proposal_briefings WHERE {where} ORDER BY {order}", params)
        return [_model(row) for row in await cur.fetchall()]


async def open_for(conn, contact_id: int) -> ProposalBriefing | None:
    return await _one(conn, f"contact_id = %s AND status IN {_ACTIVE}", (contact_id,))


async def proposal_sent_for(conn, contact_id: int) -> ProposalBriefing | None:
    """This person's request whose proposal went out most recently: the one an
    "Aceitar proposta" answers."""
    rows = await _many(conn, "contact_id = %s AND status = 'proposal_sent'", (contact_id,),
                       "proposal_sent_at DESC NULLS LAST, id DESC LIMIT 1")
    return rows[0] if rows else None


async def get(conn, briefing_id: int) -> ProposalBriefing | None:
    return await _one(conn, "id = %s", (briefing_id,))


async def create(conn, contact_id: int, answers: dict[str, str], prefilled: list[str],
                 step: str | None) -> ProposalBriefing:
    sealed = _sealed(answers)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO proposal_briefings (contact_id, status, step, answers, answers_sealed, "
            "prefilled) VALUES (%s, 'open', %s, %s, %s, %s) RETURNING id",
            (contact_id, step, sealed["answers"], sealed["answers_sealed"], prefilled))
        briefing_id = (await cur.fetchone())[0]
    briefing = await get(conn, briefing_id)
    assert briefing is not None
    return briefing


async def save(conn, briefing_id: int, *, answers: dict[str, str], step: str | None,
               prefilled: list[str] | None = None) -> None:
    """Store the answers and the step; `prefilled` too when given."""
    sealed = _sealed(answers)
    await conn.execute(
        "UPDATE proposal_briefings SET answers = %s, answers_sealed = %s, step = %s, "
        "prefilled = COALESCE(%s, prefilled), updated_at = NOW() WHERE id = %s",
        (sealed["answers"], sealed["answers_sealed"], step, prefilled, briefing_id))


async def set_status(conn, briefing_id: int, status: str) -> None:
    await conn.execute(
        "UPDATE proposal_briefings SET status = %s, updated_at = NOW(), "
        "submitted_at = CASE WHEN %s = 'sent' THEN NOW() ELSE submitted_at END WHERE id = %s",
        (status, status, briefing_id))


async def close_abandoned(conn, briefing_id: int, older_than: timedelta) -> bool:
    """Move a still-abandoned questionnaire to incomplete_sent. The conditions
    are re-checked here, so a row answered or submitted since it was listed is
    left alone (False). updated_at stays: it is the person's last activity."""
    cur = await conn.execute(
        "UPDATE proposal_briefings SET status = 'incomplete_sent' "
        "WHERE id = %s AND status = 'open' AND updated_at < NOW() - %s "
        "AND incomplete_notified_at IS NULL RETURNING id", (briefing_id, older_than))
    return await cur.fetchone() is not None


async def mark_notified(conn, briefing_id: int, kind: Literal["sent", "incomplete"]) -> bool:
    """Stamp the team's notice once; False when that notice was already marked."""
    column = _NOTICE_COLUMN[kind]
    cur = await conn.execute(
        f"UPDATE proposal_briefings SET {column} = NOW() "
        f"WHERE id = %s AND {column} IS NULL RETURNING id", (briefing_id,))
    return await cur.fetchone() is not None


async def abandoned(conn, older_than: timedelta) -> list[ProposalBriefing]:
    return await _many(
        conn, "status = 'open' AND updated_at < NOW() - %s AND incomplete_notified_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM contacts c WHERE c.id = proposal_briefings.contact_id "
        "AND c.blocked_at IS NOT NULL)",
        (older_than,), "id")


async def requests(conn) -> list[ProposalBriefing]:
    """The panel's "Pedidos de proposta" (and its badge). The team's own chats
    are left out, as in the funnel and the Inbox, unless in /teste."""
    hidden = hidden_from_customer_lists_sql("c.chat_id")
    return await _many(
        conn, "status IN ('sent', 'incomplete_sent', 'proposal_sent') AND NOT EXISTS "
        f"(SELECT 1 FROM contacts c WHERE c.id = proposal_briefings.contact_id AND {hidden})",
        (), "COALESCE(submitted_at, updated_at) DESC, id DESC")


async def mark_proposal_sent(conn, briefing_id: int, user_id: int) -> bool:
    cur = await conn.execute(
        "UPDATE proposal_briefings SET status = 'proposal_sent', proposal_sent_at = NOW(), "
        "proposal_sent_by = %s, updated_at = NOW() "
        "WHERE id = %s AND status IN ('sent', 'incomplete_sent') RETURNING id",
        (user_id, briefing_id))
    return await cur.fetchone() is not None


async def unmark_proposal_sent(conn, briefing_id: int, status: str) -> None:
    """Undo mark_proposal_sent when the channel surely refused the proposal:
    the request waits again, as it was."""
    await conn.execute(
        "UPDATE proposal_briefings SET status = %s, proposal_sent_at = NULL, "
        "proposal_sent_by = NULL, updated_at = NOW() "
        "WHERE id = %s AND status = 'proposal_sent'",
        (status, briefing_id))
