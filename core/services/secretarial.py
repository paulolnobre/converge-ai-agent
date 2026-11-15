"""The work a secretary does after the meeting: notes, follow-ups, the briefing.

Phase 4. Each of these produces something a person reads and acts on, so each of
them is wrong in a way that costs someone their morning — which is why none of
them sends anything on its own.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from core import content
from core.config import settings
from core.integrations import telegram
from core.integrations.workspace.models import TimeSlot
from core.models import MeetingNotes
from core.repositories import briefing as briefing_repo
from core.repositories.connection import get_pool
from core.services import scheduling
from core.services.handoff import STAGE_LABELS

logger = logging.getLogger(__name__)

_SWEEP_SECONDS = 300


async def write_minutes(conn, appointment_id: int, transcript: str) -> MeetingNotes:
    """Summarize a meeting into decisions and action items.

    Phase 4, step 1 in docs/ROADMAP.md.
    """
    raise NotImplementedError(
        "Meeting minutes are phase 4, step 1 — see docs/ROADMAP.md."
    )


@dataclass
class Briefing:
    today: list[dict] = field(default_factory=list)
    new_contacts: list[dict] = field(default_factory=list)
    inbound: int = 0
    handoffs: list[dict] = field(default_factory=list)
    unrecorded: int = 0
    stale_proposals: list[dict] = field(default_factory=list)
    followups: list[dict] = field(default_factory=list)


_MAX_LISTED = 15


def _zone() -> ZoneInfo:
    return ZoneInfo(settings.office_timezone)


def _capped(lines: list[str]) -> list[str]:
    if len(lines) <= _MAX_LISTED:
        return lines
    return [*lines[:_MAX_LISTED], f"… e mais {len(lines) - _MAX_LISTED}"]


async def gather(conn, now: datetime, *, scheduled: bool) -> Briefing:
    """Everything the briefing says, read in one place so compose() stays pure.

    "Since the last briefing" starts at the previous day's send. The scheduled
    run has already claimed today by the time it gets here, so it looks strictly
    before today; /resumo later in the day counts from this morning's.
    """
    local = now.astimezone(_zone())
    day = local.date()
    before = day if scheduled else day + timedelta(days=1)
    since = await briefing_repo.last_sent_before(conn, before) or now - timedelta(hours=24)
    start = datetime.combine(day, time.min, tzinfo=_zone())
    stale = await briefing_repo.stale_proposals(conn, settings.followup_after_days)
    listed = {item["id"] for item in stale}
    return Briefing(
        today=await briefing_repo.appointments_between(conn, start, start + timedelta(days=1)),
        new_contacts=await briefing_repo.new_contacts_since(conn, since),
        inbound=await briefing_repo.inbound_since(conn, since),
        handoffs=await briefing_repo.open_handoffs(conn),
        unrecorded=await briefing_repo.unrecorded_outcomes(conn),
        stale_proposals=stale,
        # A proposal already listed as idle is not named a second time below.
        followups=[item for item in await pending_followups(conn) if item["id"] not in listed],
    )


def compose(data: Briefing, now: datetime) -> str:
    """The briefing as one message. Built from what the database says, never
    by a model: a number in it that is wrong is worse than no briefing."""
    local = now.astimezone(_zone())
    parts = scheduling.when_parts(TimeSlot(start=now, end=now + timedelta(minutes=1)))
    greeting = ("briefing.good_morning" if local.hour < 12
                else "briefing.good_afternoon" if local.hour < 18 else "briefing.good_evening")
    blocks = [content.render("briefing.header", saudacao=content.text(greeting),
                             dia_semana=parts["dia_semana"],
                             data=local.strftime("%d/%m"))]

    if data.today:
        lines = []
        for item in data.today:
            hour = item["starts_at"].astimezone(_zone()).strftime("%H:%M")
            state = content.text(
                "briefing.confirmed" if item["confirmed"] else "briefing.unconfirmed"
            )
            lines.append(f"• {hour} · {item['contact_name'] or 'Sem nome'} — {state}")
        blocks.append("\n".join([content.text("briefing.today"), *lines]))
    else:
        blocks.append(content.text("briefing.today_empty"))

    if data.new_contacts:
        head = content.render(
            "briefing.new_contacts", total=len(data.new_contacts), mensagens=data.inbound
        )
        lines = [
            f"• {item['name'] or 'Sem nome'} ({STAGE_LABELS.get(item['stage'], item['stage'])})"
            for item in data.new_contacts
        ]
        blocks.append("\n".join([head, *_capped(lines)]))
    else:
        blocks.append(content.render("briefing.no_new_contacts", mensagens=data.inbound))

    if data.handoffs:
        lines = []
        for item in data.handoffs:
            owner = (
                content.render("briefing.claimed", atendente=item["claimed_by_name"])
                if item["claimed_by_name"] else content.text("briefing.unclaimed")
            )
            lines.append(f"• {item['contact_name'] or 'Sem nome'} — {owner}")
        blocks.append("\n".join([content.text("briefing.handoffs"), *_capped(lines)]))

    if data.unrecorded:
        blocks.append(content.render("briefing.unrecorded", total=data.unrecorded))

    if data.stale_proposals:
        lines = [
            f"• {item['name'] or 'Sem nome'} (desde "
            f"{item['stage_changed_at'].astimezone(_zone()).strftime('%d/%m')})"
            for item in data.stale_proposals
        ]
        blocks.append("\n".join([
            content.render("briefing.stale_proposals", dias=settings.followup_after_days),
            *_capped(lines),
        ]))

    if data.followups:
        lines = [
            f"• {item['name'] or 'Sem nome'} "
            f"({STAGE_LABELS.get(item['stage'], item['stage'])}) — última mensagem nossa em "
            f"{item['last_at'].astimezone(_zone()).strftime('%d/%m')}"
            for item in data.followups
        ]
        blocks.append("\n".join([
            content.render("briefing.followups", dias=settings.followup_after_days),
            *_capped(lines),
        ]))

    return "\n\n".join(blocks)


async def daily_briefing(conn, now: datetime | None = None, *, scheduled: bool = False) -> str:
    """This morning: appointments, what came in since the last one, what is pending.

    Phase 4, step 2 in docs/ROADMAP.md.
    """
    now = now or datetime.now(UTC)
    return compose(await gather(conn, now, scheduled=scheduled), now)


def _in_window(now: datetime) -> bool:
    """A workday, from DAILY_BRIEFING_HOUR for three hours. A service that was
    down all morning says nothing at 15:00 rather than a late "Bom dia"."""
    local = now.astimezone(_zone())
    start = settings.daily_briefing_hour
    return local.isoweekday() in settings.workdays() and start <= local.hour < start + 3


async def send_if_due(conn, now: datetime | None = None) -> bool:
    team = settings.team_chat_id.strip()
    now = now or datetime.now(UTC)
    if not team or not _in_window(now):
        return False
    if not await briefing_repo.claim_day(conn, now.astimezone(_zone()).date()):
        return False  # already went out today
    text = await daily_briefing(conn, now, scheduled=True)
    return await telegram.post_message(team, text) is not None


async def run_briefing_worker(stop_event: asyncio.Event) -> None:
    pool = await get_pool()
    while not stop_event.is_set():
        try:
            async with pool.connection() as conn:
                if await send_if_due(conn):
                    logger.info("Posted the morning briefing")
        except Exception:
            logger.exception("Morning briefing failed")
        await asyncio.sleep(_SWEEP_SECONDS)


async def pending_followups(conn) -> list[dict]:
    """Leads we answered who have not written back in FOLLOWUP_AFTER_DAYS.

    Surfaced in the briefing for a person to decide on, never chased by the
    bot: a follow-up a bot sends to someone who is simply thinking it over is
    worse than none. Phase 4, step 3 in docs/ROADMAP.md.
    """
    return await briefing_repo.silent_leads(conn, settings.followup_after_days)
