"""Contracts sent to be signed (migration 0029): one row per proposal request."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb

from core.services.pricing import SetupPlan


@dataclass(frozen=True)
class Sent:
    briefing_id: int
    status: str
    sandbox: bool
    document_id: str | None
    customer_signature: str | None
    sent_by: int | None
    sent_at: datetime | None
    created_at: datetime | None = None
    customer_signed_at: datetime | None = None
    converge_signed_at: datetime | None = None
    signed_at: datetime | None = None
    signed_file: str | None = None
    # The setup's payments the contract states (migration 0032); None when it
    # was sent before they were kept.
    terms: SetupPlan | None = None


async def claim(conn, briefing_id: int, user_id: int, *, sandbox: bool,
                terms: SetupPlan | None = None) -> int | None:
    """This attempt's id, the request marked "sending" by this person; None
    when it was sent (or is being sent) already."""
    cur = await conn.execute(
        # The clock now, not the transaction's start: waiting on the locks
        # before this must not make a fresh attempt look stale.
        "INSERT INTO contract_signatures (briefing_id, sandbox, sent_by, created_at, "
        "setup_terms) VALUES (%s, %s, %s, clock_timestamp(), %s) "
        "ON CONFLICT (briefing_id) DO NOTHING RETURNING id",
        (briefing_id, sandbox, user_id,
         Jsonb(dataclasses.asdict(terms)) if terms is not None else None))
    row = await cur.fetchone()
    return row[0] if row else None


async def release(conn, attempt: int) -> None:
    """Autentique refused this attempt: the request can be sent again."""
    await conn.execute("DELETE FROM contract_signatures WHERE id = %s AND status = 'sending'",
                       (attempt,))


async def release_stale(conn, briefing_id: int, minutes: int) -> bool:
    """A send never confirmed, freed by the team once they checked Autentique
    has no such document. Only past `minutes`: one still under way stays."""
    cur = await conn.execute(
        "DELETE FROM contract_signatures WHERE briefing_id = %s AND status = 'sending' "
        "AND created_at < NOW() - make_interval(mins => %s)", (briefing_id, minutes))
    return cur.rowcount == 1


async def mark_sent(conn, attempt: int, document_id: str, customer_signature: str | None,
                    converge_signature: str | None) -> bool:
    """False when this attempt was freed meanwhile ("Liberar para reenviar"):
    then a newer attempt owns the request, and this one is not written over it."""
    cur = await conn.execute(
        "UPDATE contract_signatures SET status = 'sent', document_id = %s, "
        "customer_signature = %s, converge_signature = %s, sent_at = NOW() "
        "WHERE id = %s AND status = 'sending'",
        (document_id, customer_signature, converge_signature, attempt))
    return cur.rowcount == 1


async def get(conn, briefing_id: int) -> Sent | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT briefing_id, status, sandbox, document_id, customer_signature, sent_by, "
            "sent_at, created_at, customer_signed_at, converge_signed_at, signed_at, "
            "signed_file, setup_terms FROM contract_signatures WHERE briefing_id = %s",
            (briefing_id,))
        row = await cur.fetchone()
    if row is None:
        return None
    terms = row[-1]
    return Sent(*row[:-1], terms=_terms(terms))


def _terms(value: object) -> SetupPlan | None:
    if not isinstance(value, dict):
        return None
    try:
        return SetupPlan(**value)
    except TypeError:
        return None


@dataclass(frozen=True)
class Document:
    """A sent contract, as its webhook finds it."""
    briefing_id: int
    contact_id: int
    status: str
    customer_signature: str | None
    converge_signature: str | None


async def by_document(conn, document_id: str) -> Document | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT s.briefing_id, b.contact_id, s.status, s.customer_signature, "
            "s.converge_signature FROM contract_signatures s "
            "JOIN proposal_briefings b ON b.id = s.briefing_id WHERE s.document_id = %s",
            (document_id,))
        row = await cur.fetchone()
    return Document(*row) if row else None


async def mark_party_signed(conn, briefing_id: int, party: str, at: datetime | None) -> bool:
    """One party signed, at Autentique's time (ours when it gave none); True
    only the first time (webhooks repeat, and may come after the finish)."""
    column = {"customer": "customer_signed_at", "converge": "converge_signed_at"}[party]
    cur = await conn.execute(
        f"UPDATE contract_signatures SET {column} = COALESCE(%s, NOW()) "  # noqa: S608 - fixed names
        f"WHERE briefing_id = %s AND {column} IS NULL AND status IN ('sent', 'signed')",
        (at, briefing_id))
    return cur.rowcount == 1


async def mark_signed(conn, briefing_id: int, signed_file: str | None,
                      at: datetime | None) -> bool:
    """Every party signed; True only the first time, so the team hears once.
    Each party's own time comes with its signature event, in any order."""
    cur = await conn.execute(
        "UPDATE contract_signatures SET status = 'signed', signed_at = COALESCE(%s, NOW()), "
        "signed_file = %s WHERE briefing_id = %s AND status = 'sent'",
        (at, signed_file, briefing_id))
    return cur.rowcount == 1


async def sending_any(conn) -> bool:
    """Some contract is still being sent: its document id is not written yet."""
    cur = await conn.execute("SELECT 1 FROM contract_signatures WHERE status = 'sending' LIMIT 1")
    return await cur.fetchone() is not None

