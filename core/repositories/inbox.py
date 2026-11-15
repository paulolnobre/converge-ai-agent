"""The Inbox tab: every conversation in progress, and who holds it.

Reads join what the conversation already records — sessions for the words,
handoff_sessions for who holds it — so the Inbox shows the facts the bot acts
on. The two claims are single UPDATEs: two messages arriving together, or two
workers, get one notice between them, never two.
"""
from __future__ import annotations

import re

from psycopg.rows import dict_row

from core.repositories.contacts import hidden_from_customer_lists_sql
from core.sealed_fields import field, open_dict, pair_sql, write

FILTERS = ("waiting", "mine", "bot", "all", "archived", "blocked")

# A conversation with no message for this long, and nobody handling it, is
# archived: out of the usual lists, one filter away.
ARCHIVE_DAYS = 15

_STATE = (
    "CASE h.status WHEN 'claimed' THEN 'claimed' WHEN 'open' THEN 'waiting' "
    "ELSE 'bot' END"
)

_NAME = field("contacts", "name")
_MESSAGE_FIELD = field("sessions", "message")
_CONVERSATION_PAIRS = tuple(
    (field("contacts", c), c) for c in ("name", "phone", "email", "origin_detail")
)

_LIST = f"""
    SELECT c.id AS contact_id, c.chat_id, {pair_sql("c", _NAME)}, c.source AS channel, c.origin,
           c.test_mode IS NOT NULL AS is_test,
           {_STATE} AS state,
           h.claimed_by_name AS owner_name, h.claimed_by_user_id AS owner_user_id,
           COALESCE(h.claimed_by_user_id = %(user_id)s, FALSE) AS mine,
           last.role AS last_role, last.message AS last_message,
           last.message_sealed AS last_message_sealed,
           last.created_at AS last_at
      FROM contacts c
      LEFT JOIN handoff_sessions h
             ON h.contact_id = c.id AND h.status IN ('open', 'claimed')
      LEFT JOIN LATERAL (
          SELECT role, message, message_sealed, created_at FROM sessions s
           WHERE s.contact_id = c.id
           ORDER BY s.created_at DESC, s.id DESC LIMIT 1
      ) last ON TRUE
     WHERE NOT {hidden_from_customer_lists_sql('c.chat_id')}
"""
# A lead the team added has no message until it is sent: it counts from when it
# was added, so it does not vanish from every list.
_SEEN = "COALESCE(last.created_at, CASE WHEN c.origin_set_by IS NOT NULL THEN c.created_at END)"
# Ended by the team, and nothing written since: archived. A new message brings
# it back, as a new conversation.
# Only the person writing brings it back: a reminder the bot sends does not.
_LAST_FROM_PERSON = ("(SELECT MAX(lp.created_at) FROM sessions lp "
                     "WHERE lp.contact_id = c.id AND lp.role = 'user')")
_ENDED = (f"(c.ended_at IS NOT NULL AND c.ended_at >= "
          f"COALESCE({_LAST_FROM_PERSON}, '-infinity'::timestamptz))")
_RECENT = (f" AND (h.id IS NOT NULL OR ({_SEEN} > NOW() - INTERVAL '{ARCHIVE_DAYS} days'"
           f" AND NOT {_ENDED}))")
# Blocked: only under its own filter.
_UNBLOCKED = " AND c.blocked_at IS NULL"

# Fixed fragments only — nothing from a request is ever pasted into SQL.
_WHERE = {
    "waiting": _UNBLOCKED + " AND h.status = 'open'",
    "mine": _UNBLOCKED + " AND h.status = 'claimed' AND h.claimed_by_user_id = %(user_id)s",
    "bot": _UNBLOCKED + " AND h.id IS NULL" + _RECENT,
    "all": _UNBLOCKED + _RECENT,
    "archived": _UNBLOCKED + (f" AND h.id IS NULL AND ({_SEEN} <= NOW() - INTERVAL"
                              f" '{ARCHIVE_DAYS} days' OR {_ENDED})"),
    "blocked": " AND c.blocked_at IS NOT NULL",
}


async def list_conversations(conn, *, user_id: int, state: str = "all",
                             channel: str | None = None, limit: int = 200) -> list[dict]:
    sql = _LIST + _WHERE[state]
    if channel:
        sql += " AND c.source = %(channel)s"
    sql += f" ORDER BY {_SEEN} DESC NULLS LAST, c.id DESC LIMIT %(limit)s"
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, {"user_id": user_id, "channel": channel, "limit": limit})
        rows = [open_dict(row, ((_NAME, "name"), (_MESSAGE_FIELD, "last_message")),
                          id_key="contact_id")
                for row in await cur.fetchall()]
    for row in rows:
        _display_contact(row)
        row.pop("chat_id", None)
        row["last_message"] = preview(row["last_message"])
    return rows


def preview(text: str | None, limit: int = 120) -> str | None:
    """The start of a message, for a list. Cut here, not in SQL: the database
    only holds the envelope."""
    return text[:limit] if text is not None else None


async def window_open(conn, contact_id: int) -> bool:
    """WhatsApp takes free-form messages only within 24h of the customer's
    last one — any message, a photo or a tapped button included — by the time
    the channel stamped it (contacts.last_inbound_at); after that, only
    approved templates."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COALESCE(last_inbound_at > NOW() - INTERVAL '24 hours', FALSE) "
            "FROM contacts WHERE id = %s",
            (contact_id,),
        )
        row = await cur.fetchone()
    return bool(row and row[0])


async def count_waiting(conn) -> int:
    """For the menu: conversations where someone asked for a person and
    nobody has taken it yet."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM handoff_sessions h JOIN contacts c ON c.id = h.contact_id "
            "WHERE h.status = 'open' AND c.blocked_at IS NULL "
            f"  AND NOT {hidden_from_customer_lists_sql('c.chat_id')}"
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def conversation(conn, contact_id: int, user_id: int) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT c.id AS contact_id, c.chat_id, c.stage, "
            f"       {', '.join(pair_sql('c', f, key) for f, key in _CONVERSATION_PAIRS)}, "
            f"       c.source AS channel, c.origin, h.id AS session_id, "
            f"       c.test_mode IS NOT NULL AS is_test, "
            f"       {_STATE} AS state, h.claimed_by_name AS owner_name, "
            f"       h.claimed_by_user_id AS owner_user_id, "
            f"       COALESCE(h.claimed_by_user_id = %(user_id)s, FALSE) AS mine, "
            f"       c.blocked_at IS NOT NULL AS blocked, "
            f"       COALESCE({_ENDED}, FALSE) AS ended "
            f"  FROM contacts c "
            f"  LEFT JOIN handoff_sessions h "
            f"         ON h.contact_id = c.id AND h.status IN ('open', 'claimed') "
            f" WHERE c.id = %(contact_id)s",
            {"contact_id": contact_id, "user_id": user_id},
        )
        row = await cur.fetchone()
    if row:
        open_dict(row, _CONVERSATION_PAIRS, id_key="contact_id")
        _display_contact(row)
    return row


_MESSAGE = (
    "SELECT s.id, s.role, s.message, s.message_sealed, s.intent, s.created_at, "
    "u.name AS author "
    "FROM sessions s LEFT JOIN panel_users u ON u.id = s.author_user_id "
)


async def messages(conn, contact_id: int, after_id: int | None = None,
                   limit: int = 200, late_window: int = 50) -> list[dict]:
    """The conversation, oldest first.

    With after_id: everything after it, first — and then the most recent
    rows at or before it from the last ten minutes. Ids are handed out when a
    row is written, not when its transaction commits: a bot reply written in
    a slow turn can land with a smaller id than a team reply that committed
    first, and asking only for "after the last one seen" would skip it for
    good. The panel merges by id, so the overlap costs nothing; capping it
    (late_window) keeps a busy conversation from crowding out the new rows.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        if after_id is None:
            await cur.execute(
                f"SELECT * FROM ({_MESSAGE} WHERE s.contact_id = %s "
                "ORDER BY s.id DESC LIMIT %s) m ORDER BY id",
                (contact_id, limit),
            )
            return _open_messages(await cur.fetchall())
        await cur.execute(
            f"{_MESSAGE} WHERE s.contact_id = %s AND s.id > %s ORDER BY s.id LIMIT %s",
            (contact_id, after_id, limit),
        )
        fresh = _open_messages(await cur.fetchall())
        await cur.execute(
            f"{_MESSAGE} WHERE s.contact_id = %s AND s.id <= %s "
            "  AND s.created_at > NOW() - INTERVAL '10 minutes' "
            "ORDER BY s.id DESC LIMIT %s",
            (contact_id, after_id, late_window),
        )
        late = _open_messages(await cur.fetchall())
    return sorted(late, key=lambda m: m["id"]) + fresh


async def claim_conversation_notice(conn, contact_id: int) -> bool:
    """True once per conversation: the person has not written in 24h (or
    ever, or since the team ended the conversation), was not announced in 24h
    (or since it was ended), and is not someone from the team. Call it
    before this message is recorded. Two messages at once: the second waits
    on the row lock, then finds the notice already taken."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE contacts c SET conversation_noticed_at = clock_timestamp() "
            "WHERE c.id = %s "
            "  AND (c.conversation_noticed_at IS NULL "
            "       OR c.conversation_noticed_at < NOW() - INTERVAL '24 hours' "
            # Ended by the team: the next message is a new conversation.
            "       OR c.conversation_noticed_at < c.ended_at) "
            "  AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.contact_id = c.id "
            "                   AND s.role = 'user' "
            "                   AND s.created_at > GREATEST(NOW() - INTERVAL '24 hours', "
            "                                               c.ended_at)) "
            f"  AND NOT {hidden_from_customer_lists_sql('c.chat_id')} "
            "RETURNING c.id",
            (contact_id,),
        )
        return await cur.fetchone() is not None


async def claim_owner_ping(conn, session_id: int, minutes: int) -> bool:
    """True when the person holding this conversation may be told again that
    the customer wrote — at most once every `minutes`."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE handoff_sessions SET last_ping_at = clock_timestamp() "
            "WHERE id = %s AND (last_ping_at IS NULL "
            "  OR last_ping_at < clock_timestamp() - make_interval(mins => %s)) "
            "RETURNING id",
            (session_id, minutes),
        )
        return await cur.fetchone() is not None


async def owner_chat_id(conn, user_id: int) -> str | None:
    """The Telegram chat of a /painel account: the staff row with the same
    e-mail (content/staff.json). None when there is none to write to."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT st.chat_id FROM panel_users u "
            "JOIN staff st ON lower(st.email) = lower(u.email) "
            "WHERE u.id = %s AND st.active AND st.chat_id IS NOT NULL",
            (user_id,),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def set_origin(conn, contact_id: int, origin: str | None, detail: str | None,
                     user_id: int) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE contacts SET origin = %s, origin_detail = %s, origin_detail_sealed = %s, "
            "origin_set_by = %s, updated_at = NOW() WHERE id = %s",
            (origin, *write(field("contacts", "origin_detail"), detail).values(), user_id,
             contact_id),
        )
        return cur.rowcount > 0


async def set_origin_if_empty(conn, contact_id: int, origin: str) -> bool:
    """The first origin known wins: a later link never overwrites it."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE contacts SET origin = %s, updated_at = NOW() "
            "WHERE id = %s AND origin IS NULL",
            (origin, contact_id),
        )
        return cur.rowcount > 0


def _open_messages(rows) -> list[dict]:
    return [open_dict(row, ((_MESSAGE_FIELD, "message"),)) for row in rows]


def _display_contact(row: dict) -> None:
    """A channel address is a fallback label, not a name saved on the person."""
    chat = str(row.get('chat_id') or '')
    match = re.fullmatch(r'wa:([1-9][0-9]{5,14})', chat) if row.get('channel') == 'whatsapp' else None
    number = '+' + match.group(1) if match else None
    row['display_name'] = row.get('name') or number or 'Sem nome'
    if number and not row.get('phone'):
        row['phone'] = number
