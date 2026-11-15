"""Who owns a conversation right now: the bot, or a person in the panel's Inbox."""
from __future__ import annotations

from dataclasses import dataclass

from core.models import HandoffSession
from core.sealed_fields import field, read, read_lenient, write

_NAME = field("contacts", "name")
_REOPEN_REPLY = field("handoff_sessions", "reopen_reply")

_COLUMNS = (
    "id, contact_id, chat_id, team_chat_id, reason, status, "
    "claimed_by_id, claimed_by_name, relay_chat_id, claimed_by_user_id"
)
# The same columns, prefixed for a query that joins handoff_sessions as "s".
_S_COLUMNS = ", ".join("s." + column.strip() for column in _COLUMNS.split(","))


def _to_session(row) -> HandoffSession:
    return HandoffSession(
        id=row[0], contact_id=row[1], chat_id=row[2], team_chat_id=row[3],
        reason=row[4], status=row[5], claimed_by_id=row[6], claimed_by_name=row[7],
        relay_chat_id=row[8], claimed_by_user_id=row[9],
    )


async def _active_row(cur, chat_id):
    await cur.execute(
        f"SELECT {_COLUMNS} FROM handoff_sessions "
        "WHERE chat_id = %s AND status IN ('open', 'claimed')",
        (str(chat_id),),
    )
    return await cur.fetchone()


async def open_session(conn, *, contact_id, chat_id, team_chat_id, reason) -> HandoffSession:
    """The active session for this chat, creating one if there is none.

    A second escalation while one is already open must join it, not spawn a
    second notice racing the first for the team's attention. relay_chat_id is
    the team group; it only mattered to the relay through Telegram, which the
    Inbox replaced, and stays because the column is NOT NULL.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO handoff_sessions "
            "(contact_id, chat_id, team_chat_id, reason, relay_chat_id, last_activity_at) "
            "VALUES (%s, %s, %s, %s, %s, clock_timestamp()) "
            "ON CONFLICT (chat_id) WHERE status IN ('open', 'claimed') DO NOTHING "
            f"RETURNING {_COLUMNS}",
            (contact_id, str(chat_id), str(team_chat_id), reason, str(team_chat_id)),
        )
        row = await cur.fetchone()
        if row is None:
            row = await _active_row(cur, chat_id)
    return _to_session(row)


async def active_session(conn, chat_id) -> HandoffSession | None:
    async with conn.cursor() as cur:
        row = await _active_row(cur, chat_id)
    return _to_session(row) if row else None










async def claim_by_user(conn, session_id: int, user_id: int, name: str,
                        expected_owner: int | None) -> bool:
    """Take a session for someone signed in to /painel — only while it is
    still with whoever the panel showed (`expected_owner`: None for nobody,
    or for a claim made the old way, in Telegram). Two people clicking
    "Assumir" at once: the first gets it, the second is told it changed hands."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE handoff_sessions SET status = 'claimed', claimed_by_user_id = %s, "
            "claimed_by_name = %s, claimed_by_id = NULL, "
            "claimed_at = COALESCE(claimed_at, NOW()), last_activity_at = clock_timestamp() "
            "WHERE id = %s AND status IN ('open', 'claimed') "
            "  AND claimed_by_user_id IS NOT DISTINCT FROM %s",
            (user_id, name, session_id, expected_owner),
        )
        return cur.rowcount > 0




async def lock_chat(conn, chat_id) -> None:
    """Serialize everything that decides who answers this chat, until the
    caller's transaction ends: a bot turn (one transaction per update, see
    core/agent.py) and a person taking the conversation in the panel. A claim
    that arrives mid-turn waits for the turn — its reply included — to finish,
    so the bot never acts after someone took over. An advisory lock, because
    a chat nobody asked a person for has no handoff row to lock."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"chat:{chat_id}",)
        )


async def lock_for_reply(conn, chat_id, user_id: int) -> int | None:
    """The session this person holds for this chat, locked until the caller's
    transaction ends — or None if they do not hold it. Taken before a reply
    goes out: while the lock stands, claim_by_user() and close_by() on the
    same row wait, so nobody can take the conversation mid-send."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM handoff_sessions "
            "WHERE chat_id = %s AND status = 'claimed' AND claimed_by_user_id = %s "
            "FOR UPDATE",
            (str(chat_id), user_id),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def close_by(conn, session_id: int, user_id: int) -> bool:
    """Give a session back to the bot from the panel: one nobody has taken
    yet, or one this person holds — never someone else's."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE handoff_sessions SET status = 'closed', closed_at = NOW() "
            "WHERE id = %s AND (status = 'open' "
            "  OR (status = 'claimed' AND claimed_by_user_id = %s))",
            (session_id, user_id),
        )
        return cur.rowcount > 0


async def touch(conn, session_id: int) -> None:
    """Record activity, for the inactivity sweep to measure against.

    clock_timestamp(), not NOW(): NOW() is when the update's transaction began,
    which can be minutes earlier when a model or Telegram call is slow — and
    the sweep would then close the session that much before its time.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE handoff_sessions SET last_activity_at = clock_timestamp() WHERE id = %s",
            (session_id,),
        )


async def close(conn, session_id: int) -> bool:
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE handoff_sessions SET status = 'closed', closed_at = NOW() "
            "WHERE id = %s AND status != 'closed'",
            (session_id,),
        )
        return cur.rowcount > 0


# A reopen went out and the customer has not answered yet: the sweep leaves the
# session with its owner, for the reply to go on the tap, for up to a week.
REOPEN_HOLD_DAYS = 7
_NOT_AWAITING_TAP = ("(reopen_pending_at IS NULL OR "
                     f"reopen_pending_at < NOW() - make_interval(days => {REOPEN_HOLD_DAYS}))")


async def close_if_idle(conn, session_id: int, minutes: int) -> bool:
    """Close only if the session is still inactive after the chat is locked."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE handoff_sessions SET status = 'closed', closed_at = clock_timestamp() "
            "WHERE id = %s AND status IN ('open', 'claimed') "
            "AND last_activity_at < clock_timestamp() - make_interval(mins => %s) "
            f"AND {_NOT_AWAITING_TAP}",
            (session_id, minutes),
        )
        return cur.rowcount > 0


@dataclass(frozen=True)
class StaleSession:
    session: HandoffSession
    contact_name: str | None


async def stale_sessions(conn, minutes: int) -> list[StaleSession]:
    """Open or claimed sessions nobody has touched in `minutes` — closed by
    the inactivity sweep, never silently: the customer is told, and so is the
    team, in both cases."""
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_S_COLUMNS}, c.name, c.name_sealed "
            "FROM handoff_sessions s LEFT JOIN contacts c ON c.id = s.contact_id "
            "WHERE s.status IN ('open', 'claimed') "
            "  AND s.last_activity_at < NOW() - make_interval(mins => %s) "
            f"  AND {_NOT_AWAITING_TAP.replace('reopen_pending_at', 's.reopen_pending_at')}",
            (minutes,),
        )
        rows = await cur.fetchall()
    return [StaleSession(session=_to_session(row[:10]),
                         contact_name=read_lenient(_NAME, row[10], row[11], row_id=row[0]))
            for row in rows]


async def stage_of(conn, contact_id: int) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT stage FROM contacts WHERE id = %s", (contact_id,))
        row = await cur.fetchone()
    return row[0] if row else None


@dataclass(frozen=True)
class ReopenPending:
    session_id: int
    owner_user_id: int | None
    owner_name: str | None
    reply: str | None       # the message as it goes out, or None: only the notice


async def set_reopen_pending(conn, session_id: int, reply: str | None) -> None:
    """A reopen template went out: the customer's next message takes `reply`
    (or, without one, the "só um instante" notice). A new reopen replaces the last."""
    sealed = write(_REOPEN_REPLY, reply)
    await conn.execute(
        "UPDATE handoff_sessions SET reopen_pending_at = clock_timestamp(), "
        "reopen_reply = %s, reopen_reply_sealed = %s WHERE id = %s",
        (sealed["reopen_reply"], sealed["reopen_reply_sealed"], session_id),
    )


async def take_reopen_pending(conn, chat_id) -> ReopenPending | None:
    """What a reopen left for this chat, cleared as it is read: two messages
    racing in, only one gets it. Only while a person still holds the chat."""
    async with conn.cursor() as cur:
        await cur.execute(
            "WITH old AS (SELECT id, reopen_reply, reopen_reply_sealed FROM handoff_sessions "
            "  WHERE chat_id = %s AND status = 'claimed' AND reopen_pending_at IS NOT NULL "
            "  FOR UPDATE) "
            "UPDATE handoff_sessions s SET reopen_pending_at = NULL, reopen_reply = NULL, "
            "  reopen_reply_sealed = NULL FROM old WHERE s.id = old.id "
            "RETURNING s.id, s.claimed_by_user_id, s.claimed_by_name, "
            "  old.reopen_reply, old.reopen_reply_sealed",
            (str(chat_id),),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return ReopenPending(session_id=row[0], owner_user_id=row[1], owner_name=row[2],
                         reply=read(_REOPEN_REPLY, row[3], row[4], row[0]))
