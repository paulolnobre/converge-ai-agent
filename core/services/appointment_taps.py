"""Buttons about an appointment carry its time, so a tap acts only on the
meeting as it was when the button was sent (agenda no painel, increment 2).

`appt2:<action>:<id>:<stamp>` and `outcome2:<result>:<id>:<stamp>`, the stamp
being the start in whole UTC minutes. The prefix is new on purpose: an older
image does not recognise it and ignores the tap, instead of failing to read an
id. Buttons sent before this (`appt:`, `outcome:`) still act while the meeting
was never moved by the calendar sync.
"""
from __future__ import annotations

from datetime import datetime

from core.models import Appointment

APPOINTMENT_PREFIXES = ("appt:", "appt2:")
OUTCOME_PREFIXES = ("outcome:", "outcome2:")


def stamp(starts_at: datetime) -> int:
    return int(starts_at.timestamp() // 60)


def tap(action: str, appointment: Appointment) -> str:
    return f"appt2:{action}:{appointment.id}:{stamp(appointment.starts_at)}"


def outcome_tap(result: str, appointment: Appointment) -> str:
    return f"outcome2:{result}:{appointment.id}:{stamp(appointment.starts_at)}"


def parse(data: str) -> tuple[str, int, int | None]:
    """(action, appointment id, stamp); stamp None for an old button.
    ValueError for anything else."""
    parts = data.split(":")
    if parts[0] in ("appt2", "outcome2") and len(parts) == 4:
        return parts[1], int(parts[2]), int(parts[3])
    if parts[0] in ("appt", "outcome") and len(parts) == 3:
        return parts[1], int(parts[2]), None
    raise ValueError("not an appointment button")


def still_valid(appointment: Appointment, tapped_stamp: int | None) -> bool:
    """The meeting is still at the time the button was sent with; an old
    button, only if the meeting was never moved."""
    if tapped_stamp is None:
        return appointment.moved_at is None
    return stamp(appointment.starts_at) == tapped_stamp
