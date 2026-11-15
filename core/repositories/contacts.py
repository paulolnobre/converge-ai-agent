"""The people the desk talks to, and the staff it books them with."""
from __future__ import annotations

import logging
from datetime import datetime

from core import channels
from core.channels import channel_of
from core.models import Contact, StaffMember
from core.sealed_fields import field, fingerprints, read, write

logger = logging.getLogger(__name__)

# The encrypted fields of a contact, in the order _COLUMNS lists their pairs.
_SEALED = tuple(field("contacts", c) for c in ("name", "email", "phone", "organization", "notes"))
_MESSAGE = field("sessions", "message")
_COLUMNS = (
    "id, chat_id, "
    + ", ".join(f"{f.column}, {f.sealed}" for f in _SEALED)
    + ", pending_action, details_requested_at, source, test_mode, blocked_at, ended_at"
)


def _to_contact(row) -> Contact:
    name, email, phone, organization, notes = (
        read(f, row[2 + 2 * i], row[3 + 2 * i]) for i, f in enumerate(_SEALED)
    )
    return Contact(
        id=row[0], chat_id=row[1], name=name, email=email, phone=phone,
        organization=organization, notes=notes, pending_action=row[12],
        details_requested_at=row[13], channel=row[14], test_mode=row[15],
        blocked_at=row[16], ended_at=row[17],
    )


def sealed_assignments(values: dict[str, str | None]) -> tuple[list[str], list[object]]:
    """`column = %s` pairs and their parameters for contact fields: each
    encrypted field as its two columns, and the phone and e-mail with their
    fingerprints. Keys that are not encrypted fields pass through as they are."""
    columns: dict[str, object] = {}
    for key, value in values.items():
        try:
            columns.update(write(field("contacts", key), value))
        except KeyError:
            columns[key] = value
    if "phone" in values:
        columns["phone_fp"] = fingerprints(values["phone"], None)["phone_fp"]
    if "email" in values:
        columns["email_fp"] = fingerprints(None, values["email"])["email_fp"]
    return [f"{column} = %s" for column in columns], list(columns.values())


def is_staff_sql(column: str) -> str:
    """SQL that is true when `column`, a chat id from either channel, belongs
    to a team member. One definition, so no list of customers can drift from
    the others and show the team as clients."""
    return (f"EXISTS (SELECT 1 FROM staff st WHERE st.chat_id = {column} "
            f"OR {column} = ANY(st.whatsapp_ids))")


def _in_test_sql(column: str) -> str:
    return (f"EXISTS (SELECT 1 FROM contacts tm WHERE tm.chat_id = {column} "
            f"AND tm.test_mode IS NOT NULL)")


def hidden_from_customer_lists_sql(column: str) -> str:
    """True for the team's own chats — unless that chat is in /teste, which is
    exactly when the team wants to see it in the Inbox and the funnel."""
    return f"({is_staff_sql(column)} AND NOT {_in_test_sql(column)})"


def left_out_of_metrics_sql(column: str) -> str:
    """True for the team's chats and for any chat in /teste: neither is a
    customer, so neither counts in the summary or gets an automation."""
    return f"({is_staff_sql(column)} OR {_in_test_sql(column)})"


async def never_deferred_chats(conn) -> set[str]:
    """Every chat that is answered after hours too: the team's (Telegram and
    WhatsApp) and any chat in /teste."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT chat_id FROM staff WHERE chat_id IS NOT NULL "
            "UNION SELECT unnest(whatsapp_ids) FROM staff "
            "UNION SELECT chat_id FROM contacts WHERE test_mode IS NOT NULL"
        )
        rows = await cur.fetchall()
    return {str(row[0]) for row in rows if row[0]}


async def get_or_create(conn, chat_id, name: str | None = None,
                        inbound_at: datetime | None = None) -> Contact:
    """Find this chat's contact, creating a bare row on first contact.

    The name is only ever filled in, never overwritten: the person told us their
    real name once, and a later Telegram display name of "🔥Zé🔥" must not
    replace it. `inbound_at` is when the person wrote, by the channel's clock:
    the latest one is kept, since WhatsApp's 24h window counts from it.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM contacts WHERE chat_id = %s",
            (str(chat_id),),
        )
        row = await cur.fetchone()
        if row is None and inbound_at is not None and channels.is_whatsapp(chat_id):
            row = await _adopt_added_lead(cur, str(chat_id))
        if row is None:
            sealed_name = write(_SEALED[0], name)
            # Conflict-safe: a lead added in the panel and the person's first
            # message may create the same chat at once; the second one reads it.
            await cur.execute(
                "INSERT INTO contacts (chat_id, name, name_sealed, source) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (chat_id) DO NOTHING "
                f"RETURNING {_COLUMNS}",
                (str(chat_id), sealed_name["name"], sealed_name["name_sealed"],
                 channel_of(chat_id)),
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute(f"SELECT {_COLUMNS} FROM contacts WHERE chat_id = %s",
                                  (str(chat_id),))
                row = await cur.fetchone()
        elif name and not _to_contact(row).name:
            sealed_name = write(_SEALED[0], name)
            await cur.execute(
                "UPDATE contacts SET name = %s, name_sealed = %s, updated_at = NOW() "
                f"WHERE id = %s RETURNING {_COLUMNS}",
                (sealed_name["name"], sealed_name["name_sealed"], row[0]),
            )
            row = await cur.fetchone()
        if inbound_at is not None:
            await cur.execute(
                "UPDATE contacts SET last_inbound_at = "
                "GREATEST(COALESCE(last_inbound_at, %s), %s) WHERE id = %s",
                (inbound_at, inbound_at, row[0]),
            )
    return _to_contact(row)


async def find_by_chat_ids(conn, chat_ids: list[str]) -> Contact | None:
    if not chat_ids:
        return None
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM contacts WHERE chat_id = ANY(%s) ORDER BY id LIMIT 1",
            (chat_ids,),
        )
        row = await cur.fetchone()
    return _to_contact(row) if row else None


# Fixed names only; every table that keeps a contact's chat id beside its id.
_CHAT_TABLES = ("sessions", "handoff_sessions", "offers", "media")


async def _adopt_added_lead(cur, chat_id: str):
    """A lead the team added under the other form of the same Brazilian
    mobile (with or without the ninth digit) and who never wrote: the form
    Meta writes from becomes theirs, history included."""
    others = [form for form in channels.whatsapp_ids("+" + chat_id.removeprefix(channels.WA_PREFIX))
              if form != chat_id]
    if not others:
        return None
    # The lock a claim in the panel takes on the old form: a claim either
    # finishes first (its row moves with the rest) or sees the new chat id.
    for other in sorted(others):
        await cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                          (f"chat:{other}",))
    await cur.execute(
        "UPDATE contacts SET chat_id = %s, updated_at = NOW() "
        "WHERE chat_id = ANY(%s) AND last_inbound_at IS NULL AND origin_set_by IS NOT NULL "
        "RETURNING id",
        (chat_id, others),
    )
    found = await cur.fetchone()
    if found is None:
        return None
    # Everything kept by chat moves with the contact: history, a claim made in
    # the panel before they wrote (its owner keeps it), offers and files.
    for table in _CHAT_TABLES:
        await cur.execute(f"UPDATE {table} SET chat_id = %s WHERE contact_id = %s",
                          (chat_id, found[0]))
    await cur.execute(f"SELECT {_COLUMNS} FROM contacts WHERE id = %s", (found[0],))
    return await cur.fetchone()


async def get_by_id(conn, contact_id: int) -> Contact | None:
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM contacts WHERE id = %s",
            (contact_id,),
        )
        row = await cur.fetchone()
    return _to_contact(row) if row else None


async def set_pending_action(conn, contact_id: int, action: str | None) -> None:
    """Remember what the person asked for while the desk waits for their
    details, or clear it. Setting one also records that the details were asked
    for — the first time only, so the desk asks once and never nags."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE contacts SET pending_action = %s, "
            "details_requested_at = CASE WHEN %s::text IS NULL THEN details_requested_at "
            "  ELSE COALESCE(details_requested_at, NOW()) END, "
            "updated_at = NOW() WHERE id = %s",
            (action, action, contact_id),
        )


async def set_waiting(conn, contact_id: int, action: str | None) -> None:
    """Remember what the desk is waiting to hear that is not contact details —
    today, the day for a booking ("day:<appointment being moved>"). Unlike
    set_pending_action, never marks the details as asked for."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE contacts SET pending_action = %s, updated_at = NOW() WHERE id = %s",
            (action, contact_id),
        )


async def update_details(conn, contact_id: int, **fields) -> None:
    """Set only the columns given. Unknown keys are ignored, never interpolated."""
    allowed = {"name", "email", "phone", "organization", "notes"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    assignments, params = sealed_assignments(updates)
    async with conn.cursor() as cur:
        await cur.execute(
            f"UPDATE contacts SET {', '.join(assignments)}, updated_at = NOW() WHERE id = %s",
            (*params, contact_id),
        )


async def find_by_phone(conn, phone: str) -> Contact | None:
    """The contact with this phone, in any format, by its fingerprint."""
    return await _find_by(conn, "phone_fp", fingerprints(phone, None)["phone_fp"])


async def find_by_email(conn, email: str) -> Contact | None:
    return await _find_by(conn, "email_fp", fingerprints(None, email)["email_fp"])


async def _find_by(conn, column: str, value: bytes | None) -> Contact | None:
    if value is None:
        return None
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM contacts WHERE {column} = %s ORDER BY id LIMIT 1",
            (value,),
        )
        row = await cur.fetchone()
    return _to_contact(row) if row else None


async def active_staff(conn) -> list[StaffMember]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, chat_id, name, email, role, calendar_id, active "
            "FROM staff WHERE active ORDER BY name"
        )
        rows = await cur.fetchall()
    return [
        StaffMember(
            id=row[0], chat_id=row[1], name=row[2], email=row[3],
            role=row[4], calendar_id=row[5], active=row[6],
        )
        for row in rows
    ]


async def find_staff_by_chat(conn, chat_id) -> StaffMember | None:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, chat_id, name, email, role, calendar_id, active "
            "FROM staff WHERE chat_id = %s OR %s = ANY(whatsapp_ids)",
            (str(chat_id), str(chat_id)),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return StaffMember(
        id=row[0], chat_id=row[1], name=row[2], email=row[3],
        role=row[4], calendar_id=row[5], active=row[6],
    )


async def upsert_staff(conn, member: dict) -> int:
    """Load one person from the organization's staff file.

    Matched on e-mail, which is the one identifier the organization already
    treats as unique. Re-running the seed updates people rather than duplicating
    them, so the file stays the source and this table stays a copy of it.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO staff (name, email, calendar_id, chat_id, role, specialties,
                               whatsapp_ids)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (email) DO UPDATE SET
                name         = EXCLUDED.name,
                calendar_id  = EXCLUDED.calendar_id,
                chat_id      = EXCLUDED.chat_id,
                whatsapp_ids = EXCLUDED.whatsapp_ids,
                role         = EXCLUDED.role,
                specialties  = EXCLUDED.specialties
            RETURNING id
            """,
            (member["name"], member["email"], member.get("calendar_id"),
             member.get("chat_id"), member.get("role", "staff"),
             member.get("specialties", []), channels.whatsapp_ids(member.get("whatsapp"))),
        )
        row = await cur.fetchone()
    return row[0]


async def set_escalation(conn, email: str, escalates_to_email: str | None) -> None:
    """Point one person at whoever covers for them.

    Done in a second pass: the person somebody escalates to may not have been
    inserted yet when their row was written.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE staff SET escalates_to = "
            "(SELECT id FROM staff WHERE email = %s) WHERE email = %s",
            (escalates_to_email, email),
        )


async def record_message(conn, contact_id: int | None, chat_id, role: str,
                         message: str, intent: str | None = None,
                         author_user_id: int | None = None) -> int:
    """One line of the conversation; returns its id. author_user_id is the
    /painel account that wrote it, for what the team sends from the Inbox."""
    sealed = write(_MESSAGE, message)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO sessions (contact_id, chat_id, role, message, message_sealed, intent, "
            "author_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (contact_id, str(chat_id), role, sealed["message"], sealed["message_sealed"],
             intent, author_user_id),
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def is_blocked(conn, chat_id) -> bool:
    async with conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM contacts WHERE chat_id = %s AND blocked_at IS NOT NULL",
                          (str(chat_id),))
        return await cur.fetchone() is not None


async def is_blocked_now(chat_id) -> bool:
    """is_blocked on a connection of its own, for the webhook's after-hours
    notice (no turn, no lock: at worst one notice goes out during a block)."""
    from core.repositories.connection import get_pool

    pool = await get_pool()
    async with pool.connection() as conn:
        return await is_blocked(conn, chat_id)


async def set_blocked(conn, contact_id: int, user_id: int | None) -> None:
    """Blocked by this panel user; None unblocks."""
    await conn.execute(
        "UPDATE contacts SET blocked_at = CASE WHEN %(user)s::BIGINT IS NULL THEN NULL "
        "ELSE COALESCE(blocked_at, NOW()) END, blocked_by_user_id = %(user)s WHERE id = %(id)s",
        {"user": user_id, "id": contact_id})


async def set_ended(conn, contact_id: int) -> None:
    """NOW() is when this transaction began, before the chat lock it waited
    for: a message whose turn waited behind it lands after, and brings the
    conversation back. The notice is reset: the next message is a new one."""
    await conn.execute("UPDATE contacts SET ended_at = NOW(), pending_action = NULL, "
                       "conversation_noticed_at = NULL WHERE id = %s", (contact_id,))


async def last_message_at(conn, chat_id) -> datetime | None:
    """When the last message, either way, was recorded; None if never."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT MAX(created_at) FROM sessions WHERE chat_id = %s",
                          (str(chat_id),))
        row = await cur.fetchone()
    return row[0] if row else None


async def recent_history(conn, chat_id, limit: int = 10) -> list[tuple[str, str]]:
    """The last few turns, oldest first — the order a model should read them in."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT role, message, message_sealed FROM sessions WHERE chat_id = %s "
            "AND NOT EXISTS (SELECT 1 FROM media m WHERE m.session_id=sessions.id "
            "AND m.delivery_state IN ('sending','failed','uncertain')) "
            # id breaks the tie: a question and its answer are written in the
            # same transaction, so NOW() gives both the same created_at.
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (str(chat_id), limit),
        )
        rows = await cur.fetchall()
    # Strict: a model reading an unreadable line as "[ilegível]" would answer it.
    return [(row[0], read(_MESSAGE, row[1], row[2]) or "") for row in reversed(rows)]
