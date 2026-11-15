"""Mail threads the desk is working, and the replies drafted for them.

Not a copy of the mailbox: only threads the agent has looked at, with the
decision it made about each. The message bodies stay in Gmail or Outlook.
"""
from __future__ import annotations

import logging

from core.sealed_fields import field, read, read_lenient, write

logger = logging.getLogger(__name__)

_SUBJECT = field("mail_threads", "subject")
_BODY = field("mail_drafts", "body_text")


async def upsert_thread(conn, *, mailbox: str, provider: str, provider_thread_id: str,
                        subject: str | None, last_message_at, contact_id: int | None = None
                        ) -> tuple[int, bool]:
    """Register a thread. Returns (id, is_new).

    `is_new` is what stops the agent from re-triaging — and possibly re-answering
    — a thread it has already handled, every time the poller runs.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO mail_threads
                (mailbox, provider, provider_thread_id, subject, subject_sealed, contact_id,
                 last_message_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (mailbox, provider_thread_id) DO UPDATE SET
                last_message_at = GREATEST(
                    mail_threads.last_message_at, EXCLUDED.last_message_at
                ),
                -- The first subject known stays. A thread not converted yet
                -- keeps its old one, which seal_data seals.
                subject = mail_threads.subject,
                subject_sealed = CASE WHEN mail_threads.subject IS NULL
                    THEN COALESCE(mail_threads.subject_sealed, EXCLUDED.subject_sealed)
                    ELSE mail_threads.subject_sealed END
            RETURNING id, (xmax = 0) AS inserted
            """,
            (mailbox, provider, provider_thread_id, *write(_SUBJECT, subject).values(),
             contact_id, last_message_at),
        )
        row = await cur.fetchone()
    return row[0], bool(row[1])


async def set_triage(conn, thread_id: int, *, category: str, priority: int,
                     status: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE mail_threads SET category = %s, priority = %s, status = %s, "
            "triaged_at = NOW() WHERE id = %s",
            (category, priority, status, thread_id),
        )


async def open_threads(conn, limit: int = 50) -> list[tuple]:
    """What still needs a person, most urgent first."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, subject, subject_sealed, category, priority, status, last_message_at "
            "FROM mail_threads WHERE status IN ('new', 'triaged') "
            "ORDER BY priority, last_message_at DESC LIMIT %s",
            (limit,),
        )
        rows = await cur.fetchall()
    return [(row[0], read_lenient(_SUBJECT, row[1], row[2], row_id=row[0]), *row[3:])
            for row in rows]


async def save_draft(conn, thread_id: int, provider_message_id: str | None,
                     body_text: str, status: str = "draft") -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO mail_drafts (thread_id, provider_message_id, body_text, "
            "body_text_sealed, status) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (thread_id, provider_message_id, *write(_BODY, body_text).values(), status),
        )
        row = await cur.fetchone()
    return row[0]


async def approve_draft(conn, draft_id: int, staff_id: int | None) -> str | None:
    """Mark a draft approved and hand back its body, once.

    The status guard is the point: two people approving the same draft must not
    send the same e-mail twice.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE mail_drafts SET status = 'approved', approved_by = %s, "
            "approved_at = NOW() WHERE id = %s AND status = 'draft' "
            "RETURNING body_text, body_text_sealed",
            (staff_id, draft_id),
        )
        row = await cur.fetchone()
    return read(_BODY, row[0], row[1]) if row else None


async def mark_sent(conn, draft_id: int) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE mail_drafts SET status = 'sent', sent_at = NOW() WHERE id = %s",
            (draft_id,),
        )
