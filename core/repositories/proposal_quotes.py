"""The preliminary quotes of proposal requests (migration 0026). One row per
request, written once; the team edits only the current values and the note,
which is sealed at rest."""
from __future__ import annotations

import dataclasses

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from core.models import ProposalQuote
from core.sealed_fields import field, read, write
from core.services.proposal_quote import Computed

_NOTE = field("proposal_quotes", "note")
_COLUMNS = ("briefing_id, inputs, scope, detail, premises, table_digest, calc_setup_cents, "
            "calc_setup_low_cents, calc_setup_high_cents, calc_monthly_cents, setup_cents, "
            "monthly_cents, forma_implantacao, note, note_sealed, edited_by, edited_at, created_at")


async def insert_once(conn, briefing_id: int, computed: Computed) -> bool:
    """The calculated quote, unless the request has one already (maybe edited).
    True when written. Inside a savepoint: the update that sends the request
    to the team runs in the same transaction, and a failure here must not
    abort it."""
    quote = computed.quote
    async with conn.transaction():
        return await _insert(conn, briefing_id, computed, quote)


async def _insert(conn, briefing_id: int, computed: Computed, quote) -> bool:
    cur = await conn.execute(
        "INSERT INTO proposal_quotes (briefing_id, inputs, scope, detail, premises, "
        "table_digest, calc_setup_cents, calc_setup_low_cents, calc_setup_high_cents, "
        "calc_monthly_cents, setup_cents, monthly_cents) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (briefing_id) DO NOTHING",
        (briefing_id, Jsonb(dataclasses.asdict(computed.answers)),
         Jsonb(dataclasses.asdict(computed.scope)), Jsonb(quote.detail),
         Jsonb(list(computed.premissas)), quote.table_digest, quote.setup_cents,
         quote.setup_low_cents, quote.setup_high_cents, quote.monthly_cents,
         quote.setup_cents, quote.monthly_cents))
    return cur.rowcount == 1


async def get(conn, briefing_id: int) -> ProposalQuote | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {_COLUMNS} FROM proposal_quotes WHERE briefing_id = %s",
                          (briefing_id,))
        row = await cur.fetchone()
    if row is None:
        return None
    note = read(_NOTE, row.pop("note"), row.pop("note_sealed"), briefing_id)
    return ProposalQuote(**row, note=note)


async def edit(conn, briefing_id: int, *, setup_cents: int, monthly_cents: int,
               note: str | None, user_id: int, forma: str | None = None) -> bool:
    """The team's values and note, with who and when; the setup's terms kept
    when `forma` is None. False without a quote."""
    sealed = write(_NOTE, note)
    cur = await conn.execute(
        "UPDATE proposal_quotes SET setup_cents = %s, monthly_cents = %s, "
        "forma_implantacao = COALESCE(%s, forma_implantacao), note = %s, note_sealed = %s, "
        "edited_by = %s, edited_at = NOW() WHERE briefing_id = %s",
        (setup_cents, monthly_cents, forma, sealed["note"], sealed["note_sealed"],
         user_id, briefing_id))
    return cur.rowcount == 1
