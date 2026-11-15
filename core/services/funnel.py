"""Where each person stands commercially, and what moves them.

The bot sees three things happen — it answered someone, someone booked, someone
cancelled — and moves the contact through the stages those prove. Everything
past that (a proposal sent, a deal won or lost) is a person's judgement, made
in the panel, and the bot never overwrites it: a lead marked "fechado" who
later asks the bot a question is still "fechado".

Each transition names the stages it may start from, and the database applies
it only if the contact is still in one of them. That makes every move safe to
race and safe to repeat.
"""
from __future__ import annotations

import logging
from typing import Literal

from core.repositories import funnel as funnel_repo

logger = logging.getLogger(__name__)

Stage = Literal["novo", "respondido", "agendado", "proposta", "fechado", "perdido"]
STAGES: tuple[Stage, ...] = (
    "novo", "respondido", "agendado", "proposta", "fechado", "perdido",
)
OPEN_STAGES: tuple[Stage, ...] = ("novo", "respondido", "agendado", "proposta")

Event = Literal["replied", "booked", "cancelled", "no_show"]

# event -> (the stage it moves to, the stages it may move from)
_TRANSITIONS: dict[Event, tuple[Stage, tuple[Stage, ...]]] = {
    "replied": ("respondido", ("novo",)),
    "booked": ("agendado", ("novo", "respondido")),
    # Back one step, and only from agendado: a cancellation says nothing about a
    # proposal already on the table.
    "cancelled": ("respondido", ("agendado",)),
    # Same step back: the conversation the stage promised did not happen.
    "no_show": ("respondido", ("agendado",)),
}


def transition(event: Event) -> tuple[Stage, tuple[Stage, ...]]:
    return _TRANSITIONS[event]


async def advance(conn, contact_id: int | None, event: Event) -> bool:
    """Move the contact if this event says something new about them.

    Returns whether it moved. Never raises into the conversation: the funnel is
    bookkeeping, and a person must still get their answer if it fails.
    """
    if contact_id is None:
        return False
    to, allowed_from = transition(event)
    try:
        return await funnel_repo.move_stage(conn, contact_id, to, allowed_from)
    except Exception:
        logger.exception("Could not record funnel event %s for contact %s", event, contact_id)
        return False


async def step_back_after_cancel(conn, contact_id: int | None) -> bool:
    """A cancellation steps agendado back to respondido only when nothing else
    is booked ahead for this contact: one of two meetings gone is not "no
    meeting". Call it under the contact's lock."""
    if contact_id is None:
        return False
    from core.repositories import appointments as appointments_repo  # avoid a cycle

    if await appointments_repo.count_upcoming(conn, contact_id) > 0:
        return False
    return await advance(conn, contact_id, "cancelled")
