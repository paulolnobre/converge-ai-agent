"""The funnel, as the panel reads and edits it.

Reads are joins over what the conversation already records — sessions for the
last word, appointments for the next meeting — so the panel shows the same
facts the bot acts on, not a copy that can drift from them.
"""
from __future__ import annotations

from datetime import datetime

from psycopg.rows import dict_row

from core.repositories.contacts import hidden_from_customer_lists_sql, sealed_assignments
from core.sealed_fields import field, open_dict, pair_sql

# The contact's encrypted fields, as (field, key in the row).
_CONTACT_PAIRS = tuple(
    (field("contacts", c), c)
    for c in ("name", "email", "phone", "organization", "notes", "origin_detail", "interest")
)
_MESSAGE = field("sessions", "message")
# A lead row: the contact's fields and the last message.
_LEAD_PAIRS = (*_CONTACT_PAIRS, (_MESSAGE, "last_message"))
_LEAD_COLUMNS = f"""
    c.id, c.chat_id, {", ".join(pair_sql("c", f, key) for f, key in _CONTACT_PAIRS)},
    c.stage, c.stage_changed_at, c.source, c.origin, c.owner_staff_id,
    s.name AS owner_name, c.created_at,
    last.created_at AS last_message_at, last.message AS last_message,
    last.message_sealed AS last_message_sealed,
    nxt.starts_at AS next_appointment_at, c.test_mode IS NOT NULL AS is_test
"""

_LEAD_JOINS = """
    FROM contacts c
    LEFT JOIN staff s ON s.id = c.owner_staff_id
    LEFT JOIN LATERAL (
        SELECT created_at, message, message_sealed FROM sessions ss
         WHERE ss.contact_id = c.id
         ORDER BY created_at DESC, id DESC LIMIT 1
    ) last ON TRUE
    LEFT JOIN LATERAL (
        SELECT starts_at FROM appointments a
         WHERE a.contact_id = c.id AND a.status = 'confirmed' AND a.starts_at > NOW()
         ORDER BY starts_at LIMIT 1
    ) nxt ON TRUE
"""

_NAME, _ORGANIZATION, _INTEREST = (field("contacts", c) for c in ("name", "organization", "interest"))
_TITLE, _MEETING_URL = field("appointments", "title"), field("appointments", "meeting_url")
_APPOINTMENT_CONTACT_PAIRS = (
    (_NAME, "contact_name"), (_ORGANIZATION, "organization"), (_INTEREST, "interest"),
    (_TITLE, "title"), (_MEETING_URL, "meeting_url"),
)

# What the panel may change on a contact. Anything else is the bot's or the
# person's own, and is not editable from a dashboard.
EDITABLE = ("stage", "owner_staff_id", "interest", "notes", "organization", "email", "phone")


async def move_stage(conn, contact_id: int, to: str, allowed_from: tuple[str, ...]) -> bool:
    """Move a contact only if it is still in one of `allowed_from`.

    Inside a savepoint: this runs in the middle of a conversation's
    transaction, and a failure here must not abort the reply around it.
    """
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE contacts SET stage = %s, stage_changed_at = NOW() "
                "WHERE id = %s AND stage = ANY(%s)",
                (to, contact_id, list(allowed_from)),
            )
            return cur.rowcount > 0


async def list_leads(conn, limit: int = 500) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_LEAD_COLUMNS} {_LEAD_JOINS} "
            f"WHERE NOT {hidden_from_customer_lists_sql('c.chat_id')} "
            "ORDER BY COALESCE(last.created_at, c.created_at) DESC LIMIT %s",
            (limit,),
        )
        return [open_dict(row, _LEAD_PAIRS) for row in await cur.fetchall()]


async def lead(conn, contact_id: int) -> dict | None:
    """One contact, with the conversation and the meetings behind the card."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_LEAD_COLUMNS} {_LEAD_JOINS} WHERE c.id = %s", (contact_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        open_dict(row, _LEAD_PAIRS)
        await cur.execute(
            "SELECT id, role, message, message_sealed, intent, created_at FROM sessions "
            "WHERE contact_id = %s ORDER BY created_at DESC, id DESC LIMIT 40",
            (contact_id,),
        )
        row["messages"] = [open_dict(m, ((_MESSAGE, "message"),))
                           for m in reversed(await cur.fetchall())]
        await cur.execute(
            "SELECT a.id, a.starts_at, a.ends_at, a.status, a.title, a.title_sealed, "
            "       st.name AS staff_name "
            "FROM appointments a LEFT JOIN staff st ON st.id = a.staff_id "
            "WHERE a.contact_id = %s ORDER BY a.starts_at DESC LIMIT 20",
            (contact_id,),
        )
        row["appointments"] = [open_dict(a, ((_TITLE, "title"),))
                               for a in await cur.fetchall()]
    return row


async def update_lead(conn, contact_id: int, fields: dict) -> bool:
    """Apply what a person changed in the panel. Unknown keys are ignored, not
    written: the whitelist is the contract, not the request."""
    changes = {key: value for key, value in fields.items() if key in EDITABLE}
    if not changes:
        return False
    assignments, params = sealed_assignments(changes)
    if "stage" in changes:
        assignments.append("stage_changed_at = NOW()")
    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE contacts SET {', '.join(assignments)}, updated_at = NOW() WHERE id = %s",
            (*params, contact_id),
        )
        return cur.rowcount > 0


async def appointments_between(conn, start: datetime, end: datetime) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT a.id, a.starts_at, a.ends_at, a.status, a.title, a.title_sealed, "
            "       a.staff_id, a.sync_problem, "
            "       st.name AS staff_name, a.contact_id, "
            f"      {pair_sql('c', _NAME, 'contact_name')}, "
            f"      {pair_sql('c', _ORGANIZATION, 'organization')}, "
            f"      {pair_sql('c', _INTEREST, 'interest')}, a.meeting_url, "
            "       a.meeting_url_sealed "
            "FROM appointments a "
            "LEFT JOIN contacts c ON c.id = a.contact_id "
            "LEFT JOIN staff st ON st.id = a.staff_id "
            "WHERE a.starts_at >= %s AND a.starts_at < %s "
            "ORDER BY a.starts_at",
            (start, end),
        )
        return [open_dict(row, _APPOINTMENT_CONTACT_PAIRS) for row in await cur.fetchall()]


async def staff(conn) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, name, email, role, calendar_id IS NOT NULL AS has_calendar "
            "FROM staff WHERE active ORDER BY name"
        )
        return list(await cur.fetchall())
