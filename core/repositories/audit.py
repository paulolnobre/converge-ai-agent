"""What the agent did inside someone else's calendar, mailbox or drive.

The bot acts with the organization's own credentials, which means Google and
Microsoft record its actions as the service account's, not as the agent's. This
table is the only place that distinguishes them after the fact — so it is
written on failure too, not just on success.

It is also the answer to the question a client will eventually ask: "did the bot
send that, or did someone here?"
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def record(conn, *, provider: str, capability: str, action: str,
                 target: str | None = None, contact_id: int | None = None,
                 staff_id: int | None = None, succeeded: bool = True,
                 detail: str | None = None) -> None:
    try:
        # In its own savepoint: a refused row must not leave the caller's
        # transaction aborted (every later statement would fail, and what the
        # operation had already written would be undone).
        async with conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO workspace_actions
                    (provider, capability, action, target, contact_id, staff_id,
                     succeeded, detail)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (provider, capability, action, target[:255] if target else None,
                 contact_id, staff_id, succeeded, detail),
            )
    except Exception:
        # An audit failure must not swallow the operation it was describing —
        # but it must be loud, because a silent gap here is the worst outcome.
        logger.exception("Could not record a workspace action (%s/%s)", provider, action)
