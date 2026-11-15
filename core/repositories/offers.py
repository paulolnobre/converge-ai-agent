"""The times the desk offered, until someone picks one.

This is the only conversational state the agent keeps. Everything else it needs
it re-reads from the calendar, but "which times did I just offer this person?"
exists nowhere else — and without it the answer "a terca" has nothing to attach
to, so the desk offers the same three times forever.
"""
from __future__ import annotations

import json
import logging

from core.integrations.workspace.models import TimeSlot
from core.models import Offer

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id, contact_id, chat_id, slots, calendar_id, staff_id, replaces_id, "
    "status, expires_at, period, replaces_starts_at"
)


def _to_offer(row) -> Offer:
    slots = row[3] if isinstance(row[3], list) else json.loads(row[3])
    return Offer(
        id=row[0], contact_id=row[1], chat_id=row[2],
        slots=[TimeSlot.model_validate(slot) for slot in slots],
        calendar_id=row[4], staff_id=row[5], replaces_id=row[6],
        status=row[7], expires_at=row[8], period=row[9], replaces_starts_at=row[10],
    )


async def save(conn, offer: Offer, ttl_minutes: int) -> Offer:
    """Record an offer, superseding whatever this chat was offered before.

    Superseding rather than adding: two live offers make "a terca" ambiguous
    between two sets of times, and the desk would have to ask which offer the
    person meant — which is not a question a person would ever ask.
    """
    payload = json.dumps([slot.model_dump(mode="json") for slot in offer.slots])
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE offers SET status = 'superseded' "
            "WHERE chat_id = %s AND status = 'pending'",
            (offer.chat_id,),
        )
        await cur.execute(
            f"""
            INSERT INTO offers
                (contact_id, chat_id, slots, calendar_id, staff_id, replaces_id,
                 expires_at, period, replaces_starts_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW() + make_interval(mins => %s), %s, %s)
            RETURNING {_COLUMNS}
            """,
            (
                offer.contact_id, offer.chat_id, payload, offer.calendar_id,
                offer.staff_id, offer.replaces_id, ttl_minutes, offer.period,
                offer.replaces_starts_at,
            ),
        )
        row = await cur.fetchone()
    return _to_offer(row)


async def pending_for_chat(conn, chat_id) -> Offer | None:
    """The live offer for this chat, if it has not expired.

    An expired offer is not returned and not deleted: the times in it are stale,
    but the row still answers "what did the desk say at 16:40 yesterday?".
    """
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM offers "
            "WHERE chat_id = %s AND status = 'pending' AND expires_at > NOW()",
            (str(chat_id),),
        )
        row = await cur.fetchone()
    return _to_offer(row) if row else None


async def claim(conn, offer_id: int) -> bool:
    """Take the offer off the table before booking anything against it.

    Claimed first, exactly like a reminder is: two messages arriving together
    ("a terca" sent twice because the first looked slow) must produce one
    booking attempt, not two appointments half a second apart.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE offers SET status = 'taken' "
            "WHERE id = %s AND status = 'pending'",
            (offer_id,),
        )
        return cur.rowcount > 0


async def release(conn, offer_id: int) -> None:
    """Put a claimed offer back, when the booking it was claimed for failed.

    Without this a calendar error costs the person their offer as well as their
    appointment: they answer "a terca", something breaks, and the next thing
    they say has nothing to match against.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE offers SET status = 'pending' WHERE id = %s AND status = 'taken'",
            (offer_id,),
        )


async def expire(conn, chat_id) -> None:
    """Drop this chat's live offer, when the person clearly meant none of them."""
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE offers SET status = 'expired' "
            "WHERE chat_id = %s AND status = 'pending'",
            (str(chat_id),),
        )
