"""Automations, who they pick, and the queue of what they send.

Every client-facing pick applies the same exclusions (see _CLIENT_OK): not the
team, not someone who said SAIR, not someone a person is already talking to,
not someone with a meeting ahead, and nobody more than once every 30 days
across all automations. On top of that, one message per automation per
silence: whoever did not answer the last one is not asked again until they
write back.
"""
from __future__ import annotations

from psycopg.rows import dict_row

from core.repositories.contacts import left_out_of_metrics_sql
from core.sealed_fields import field, open_dict, pair_sql, write

_NAME = field("contacts", "name")
_NAME_SQL = pair_sql("c", _NAME)
_REASON = field("automation_queue", "reason")


def _open(rows) -> list[dict]:
    """The contact's name, on the rows that carry it."""
    opened = []
    for row in rows:
        if "name_sealed" in row:
            open_dict(row, ((_NAME, "name"),))
        if "reason_sealed" in row:
            open_dict(row, ((_REASON, "reason"),))
        opened.append(row)
    return opened

def client_ok(whatsapp: bool = False) -> str:
    """The exclusions every client-facing pick shares. WhatsApp contacts are
    left out unless the caller can reach them (an approved template)."""
    return f"""
    c.chat_id IS NOT NULL
    {"" if whatsapp else "AND c.source <> 'whatsapp'"}
    AND NOT c.automation_opt_out
    AND c.blocked_at IS NULL
    AND (c.last_automation_at IS NULL OR c.last_automation_at < NOW() - INTERVAL '30 days')
    AND NOT {left_out_of_metrics_sql('c.chat_id')}
    AND NOT EXISTS (SELECT 1 FROM handoff_sessions h WHERE h.contact_id = c.id
                     AND h.status IN ('open', 'claimed'))
    AND NOT EXISTS (SELECT 1 FROM appointments up WHERE up.contact_id = c.id
                     AND up.status = 'confirmed' AND up.starts_at > NOW())
"""


_CLIENT_OK = client_ok()

# The person's last message (not the bot's): an automation's own message must
# not count as the silence having ended.
_LAST_USER = """
    LEFT JOIN LATERAL (
        SELECT MAX(created_at) AS at FROM sessions su
         WHERE su.contact_id = c.id AND su.role = 'user'
    ) last_user ON true
"""

_NOT_ASKED_THIS_SILENCE = """
    AND NOT EXISTS (
        SELECT 1 FROM automation_queue q
         WHERE q.automation_id = %(automation_id)s AND q.contact_id = c.id
           AND (q.status IN ('pending', 'approved')
                OR q.created_at > COALESCE(last_user.at, '-infinity'::timestamptz))
    )
"""


async def list_automations(conn) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT a.id, a.kind, a.enabled, a.mode, a.days, a.message,
                   a.updated_at, a.updated_by,
                   (SELECT COUNT(*) FROM automation_queue q
                     WHERE q.automation_id = a.id AND q.status = 'pending') AS pending,
                   (SELECT COUNT(*) FROM automation_queue q
                     WHERE q.automation_id = a.id AND q.status = 'sent'
                       AND q.sent_at > NOW() - INTERVAL '30 days') AS sent_30d,
                   (SELECT COUNT(*) FROM automation_queue q
                     WHERE q.automation_id = a.id AND q.status = 'sent'
                       AND q.sent_at > NOW() - INTERVAL '30 days'
                       AND q.replied_at IS NOT NULL) AS replied_30d
              FROM automations a ORDER BY a.id
            """
        )
        return _open(await cur.fetchall())


async def get_automation(conn, automation_id: int) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, kind, enabled, mode, days, message FROM automations WHERE id = %s",
            (automation_id,),
        )
        return await cur.fetchone()


async def update_automation(conn, automation_id: int, fields: dict, by: str) -> bool:
    allowed = {"enabled", "mode", "days", "message"}
    changes = {key: value for key, value in fields.items() if key in allowed}
    if not changes:
        return True
    assignments = ", ".join(f"{column} = %s" for column in changes)
    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE automations SET {assignments}, updated_at = NOW(), updated_by = %s "
            "WHERE id = %s",
            (*changes.values(), by, automation_id),
        )
        return cur.rowcount > 0


# ── Who each automation picks ────────────────────────────────────────────────

async def reactivate_candidates(conn, automation_id: int, days: int, limit: int = 200
                                ) -> list[dict]:
    """Nobody has written in `days` — the person or the bot."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT c.id AS contact_id, {_NAME_SQL}, c.chat_id, NULL::bigint AS appointment_id,
                   last.created_at AS since
              FROM contacts c
              JOIN LATERAL (SELECT created_at FROM sessions s WHERE s.contact_id = c.id
                             ORDER BY s.id DESC LIMIT 1) last ON true
              {_LAST_USER}
             WHERE last.created_at < NOW() - make_interval(days => %(days)s)
               AND {_CLIENT_OK}
               {_NOT_ASKED_THIS_SILENCE}
             ORDER BY last.created_at
             LIMIT %(limit)s
            """,
            {"automation_id": automation_id, "days": days, "limit": limit},
        )
        return _open(await cur.fetchall())


async def stalled_candidates(conn, automation_id: int, days: int, limit: int = 200,
                             *, whatsapp: bool = False) -> list[dict]:
    """The bot answered last, `days` to 30 days ago, and the person stopped
    there — mid-way through the funnel, before booking or after a proposal.
    `whatsapp`: include contacts who can be reached by the resume template."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT c.id AS contact_id, {_NAME_SQL}, c.chat_id, NULL::bigint AS appointment_id,
                   c.stage, last.created_at AS since
              FROM contacts c
              JOIN LATERAL (SELECT role, created_at FROM sessions s WHERE s.contact_id = c.id
                             ORDER BY s.id DESC LIMIT 1) last ON true
              {_LAST_USER}
             WHERE c.stage IN ('respondido', 'proposta')
               AND last.role = 'assistant'
               AND last.created_at < NOW() - make_interval(days => %(days)s)
               AND last.created_at > NOW() - INTERVAL '30 days'
               AND {client_ok(whatsapp)}
               {_NOT_ASKED_THIS_SILENCE}
             ORDER BY last.created_at
             LIMIT %(limit)s
            """,
            {"automation_id": automation_id, "days": days, "limit": limit},
        )
        return _open(await cur.fetchall())


async def no_show_candidates(conn, automation_id: int, days: int, limit: int = 200
                             ) -> list[dict]:
    """Marked "não compareceu" by the team `days` ago (up to two weeks), and
    not booked again since."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT c.id AS contact_id, {_NAME_SQL}, c.chat_id, a.id AS appointment_id,
                   a.starts_at AS since
              FROM appointments a JOIN contacts c ON c.id = a.contact_id
             WHERE a.status = 'no_show'
               AND a.ends_at < NOW() - make_interval(days => %(days)s)
               AND a.ends_at > NOW() - make_interval(days => %(days)s + 14)
               AND {_CLIENT_OK}
               AND NOT EXISTS (SELECT 1 FROM automation_queue q
                                WHERE q.automation_id = %(automation_id)s
                                  AND q.appointment_id = a.id)
             ORDER BY a.ends_at
             LIMIT %(limit)s
            """,
            {"automation_id": automation_id, "days": days, "limit": limit},
        )
        return _open(await cur.fetchall())


async def no_proposal_candidates(conn, automation_id: int, days: int, limit: int = 200
                                 ) -> list[dict]:
    """A meeting that happened `days` ago (up to a month) whose lead is still
    at "agendado" — nobody moved it to a proposal, a win or a loss. Never a
    test (/teste): it is not announced in the team group."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT c.id AS contact_id, {_NAME_SQL}, c.chat_id, a.id AS appointment_id,
                   a.starts_at AS since
              FROM appointments a JOIN contacts c ON c.id = a.contact_id
             WHERE a.status = 'done'
               AND NOT a.test_mode
               AND c.test_mode IS NULL
               AND a.ends_at < NOW() - make_interval(days => %(days)s)
               AND a.ends_at > NOW() - make_interval(days => %(days)s + 30)
               AND c.stage = 'agendado'
               AND NOT EXISTS (SELECT 1 FROM automation_queue q
                                WHERE q.automation_id = %(automation_id)s
                                  AND q.appointment_id = a.id)
             ORDER BY a.ends_at
             LIMIT %(limit)s
            """,
            {"automation_id": automation_id, "days": days, "limit": limit},
        )
        return _open(await cur.fetchall())


# ── The queue ────────────────────────────────────────────────────────────────

async def enqueue(conn, automation_id: int, contact_id: int, appointment_id: int | None,
                  status: str, reason: str) -> int | None:
    """Queue one pick, or nothing if it is already queued (the unique indexes)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO automation_queue "
            "(automation_id, contact_id, appointment_id, status, reason, reason_sealed, sent_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, CASE WHEN %s = 'sent' THEN NOW() END) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (automation_id, contact_id, appointment_id, status,
             *write(_REASON, reason).values(), status),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def pending(conn) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT q.id, q.reason, q.reason_sealed, q.created_at, a.kind,
                   c.id AS contact_id, {_NAME_SQL}, c.stage
              FROM automation_queue q
              JOIN automations a ON a.id = q.automation_id
              JOIN contacts c ON c.id = q.contact_id
             WHERE q.status = 'pending'
             ORDER BY q.created_at
            """
        )
        return _open(await cur.fetchall())


async def count_pending(conn) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM automation_queue WHERE status = 'pending'")
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def decide(conn, ids: list[int], approve: bool, by: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE automation_queue SET status = %s, decided_at = NOW(), decided_by = %s "
            "WHERE id = ANY(%s) AND status = 'pending'",
            ("approved" if approve else "discarded", by, ids),
        )
        return cur.rowcount


_APPROVED_SELECT = f"""
    SELECT q.id, q.contact_id, q.appointment_id, q.created_at AS queued_at, a.kind, a.message,
           {_NAME_SQL}, c.chat_id,
           (c.automation_opt_out OR c.blocked_at IS NOT NULL) AS automation_opt_out
      FROM automation_queue q
      JOIN automations a ON a.id = q.automation_id
      JOIN contacts c ON c.id = q.contact_id
     WHERE q.status = 'approved'
"""


async def approved(conn, limit: int = 20) -> list[dict]:
    """What is cleared to go out."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _APPROVED_SELECT + " ORDER BY q.created_at LIMIT %s", (limit,)
        )
        return _open(await cur.fetchall())


async def approved_for_send(conn, queue_id: int) -> dict | None:
    """Recheck approval and opt-out under the conversation lock."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_APPROVED_SELECT + " AND q.id = %s", (queue_id,))
        row = await cur.fetchone()
    return _open([row])[0] if row else None


async def wrote_since(conn, contact_id: int, since) -> bool:
    """Whether the customer (not the bot, not the team) wrote after `since`."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT EXISTS (SELECT 1 FROM sessions "
            "WHERE contact_id = %s AND role = 'user' AND created_at > %s)",
            (contact_id, since))
        return bool((await cur.fetchone())[0])


async def mark(conn, queue_id: int, status: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE automation_queue SET status = %s, "
            "sent_at = CASE WHEN %s = 'sent' THEN NOW() ELSE sent_at END WHERE id = %s",
            (status, status, queue_id),
        )


async def discard_for_contact(conn, contact_id: int) -> None:
    """Nothing queued for this contact goes out (an ended or blocked conversation)."""
    await conn.execute(
        "UPDATE automation_queue SET status = 'discarded' "
        "WHERE contact_id = %s AND status IN ('pending', 'approved')", (contact_id,))


async def touch_contact(conn, contact_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE contacts SET last_automation_at = NOW() WHERE id = %s", (contact_id,)
        )


async def mark_replied(conn, contact_id: int) -> None:
    """The person wrote back after an automated message: it counts as answered."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE automation_queue SET replied_at = NOW() "
            "WHERE contact_id = %s AND status = 'sent' AND replied_at IS NULL "
            "AND sent_at > NOW() - INTERVAL '14 days'",
            (contact_id,),
        )


async def record_reason(conn, queue_id: int, contact_id: int, reason: str) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE automation_queue SET reply_reason = %s, "
            "replied_at = COALESCE(replied_at, NOW()) "
            "WHERE id = %s AND contact_id = %s AND status = 'sent'",
            (reason, queue_id, contact_id),
        )
        return cur.rowcount > 0


async def reasons(conn, days: int = 90) -> dict[str, int]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT reply_reason, COUNT(*) FROM automation_queue "
            "WHERE reply_reason IS NOT NULL AND replied_at > NOW() - make_interval(days => %s) "
            "GROUP BY reply_reason",
            (days,),
        )
        rows = await cur.fetchall()
    return {row[0]: int(row[1]) for row in rows}


async def set_opt_out(conn, contact_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE contacts SET automation_opt_out = TRUE WHERE id = %s", (contact_id,)
        )
        # Anything already queued for them is dropped, not sent a minute later.
        await cur.execute(
            "UPDATE automation_queue SET status = 'discarded', decided_by = 'SAIR' "
            "WHERE contact_id = %s AND status IN ('pending', 'approved')",
            (contact_id,),
        )
