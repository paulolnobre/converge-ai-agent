"""Validated shapes for the reception assistant.

Every model result and every untrusted payload crosses one of these on the way
in, so a malformed completion becomes a validation error at the boundary rather
than a wrong appointment in someone's calendar.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from core.integrations.workspace.models import TimeSlot

Intent = Literal[
    "greeting",
    "faq",           # a question the organization has an answer for
    "booking",       # wants an appointment
    "reschedule",
    "cancel",
    "document",      # wants or is sending a file
    "message",       # wants to leave a message for someone
    "escalation",    # needs a person
    "appointments",  # asks what they already have booked
    "proposal",      # asks for a proposal or a quote, or wants to hire
    "smalltalk",     # thanks, "tudo bem?", goodbye, a reaction, a question about the bot
    "other",
]


class Contact(BaseModel):
    id: int | None = None
    chat_id: str | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    organization: str | None = None
    notes: str | None = None
    # A booking or handoff waiting on the person's e-mail and phone.
    pending_action: str | None = None
    details_requested_at: datetime | None = None
    # The channel this person talks through (contacts.source): telegram today;
    # whatsapp, instagram, messenger and email once those exist.
    channel: str = "telegram"
    # A team member being served as a customer: 'novo' or 'antigo', else None.
    test_mode: str | None = None
    # Blocked in the panel: the bot answers nothing and tells nobody.
    blocked_at: datetime | None = None
    # The team ended the conversation: the next message starts a new one.
    ended_at: datetime | None = None


class HandoffSession(BaseModel):
    """Who owns a conversation right now: the bot, or a person. Open means the
    team was told and nobody has taken it yet; claimed means someone took it in
    the panel's Inbox (or, before the Inbox, by replying in Telegram)."""

    id: int | None = None
    contact_id: int | None = None
    chat_id: str
    team_chat_id: str
    reason: str
    status: Literal["open", "claimed", "closed"] = "open"
    # A Telegram user id, for claims made before the Inbox, by replying in the
    # team group. New claims leave it empty and fill claimed_by_user_id.
    claimed_by_id: int | None = None
    claimed_by_name: str | None = None
    # The /painel account that holds it. Claims made the old way, by replying
    # in Telegram, have only claimed_by_id.
    claimed_by_user_id: int | None = None
    # The team group. It routed the Telegram relay, which the Inbox replaced;
    # kept because the column is NOT NULL.
    relay_chat_id: str


class StaffMember(BaseModel):
    id: int
    chat_id: str | None = None
    name: str
    email: str | None = None
    role: Literal["staff", "manager", "admin"] = "staff"
    calendar_id: str | None = None
    active: bool = True


class Triage(BaseModel):
    """What the caller wants, decided once per message.

    `confidence` is the model's own. Below the threshold the agent asks rather
    than acts — a misread "cancel" cancels a real appointment.
    """

    intent: Intent = "other"
    summary: str = ""
    person_asked_for: str | None = None
    preferred_day: date | None = None
    preferred_period: Literal["morning", "afternoon", "any"] | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("summary")
    @classmethod
    def _bounded(cls, value: str) -> str:
        return value.strip()[:500]


class Appointment(BaseModel):
    id: int | None = None
    contact_id: int | None = None
    staff_id: int | None = None
    calendar_id: str
    provider: Literal["google", "microsoft"]
    provider_event_id: str | None = None
    title: str
    starts_at: datetime
    ends_at: datetime
    status: Literal["confirmed", "cancelled", "no_show", "done"] = "confirmed"
    # The video meeting's join link, when the event was created with one.
    meeting_url: str | None = None
    # Booked in /teste: how the test mode finds its own appointments once the
    # title is encrypted and can no longer be matched in SQL.
    test_mode: bool = False
    # The channel the person booked through (telegram, whatsapp).
    booked_via: str = "telegram"
    # Following the calendar: the provider's version of the event, the first
    # pass that did not find it, a problem that suspends the reminder, and
    # when the sync last moved it.
    provider_etag: str | None = None
    provider_missing_at: datetime | None = None
    sync_problem: Literal["all_day", "not_organizer", "start_collision"] | None = None
    moved_at: datetime | None = None

    @field_validator("ends_at")
    @classmethod
    def _after_start(cls, value: datetime, info) -> datetime:
        start = info.data.get("starts_at")
        if start and value <= start:
            raise ValueError("An appointment must end after it starts.")
        return value


class Offer(BaseModel):
    """Times put in front of someone, waiting for them to pick one.

    An offer is not a hold: the slots stay bookable by anyone until one of them
    is taken. What this records is what "a terca" refers to in the next message.
    """

    id: int | None = None
    contact_id: int | None = None
    chat_id: str
    slots: list[TimeSlot] = Field(default_factory=list)
    calendar_id: str
    staff_id: int | None = None
    # The period the day's times were filtered by ("morning", "afternoon"),
    # so a taken slot sends the person back to the same filter.
    period: str | None = None
    replaces_id: int | None = None
    # The time of the meeting being replaced when the offer was made: a meeting
    # moved since (by the calendar sync) makes the offer stale.
    replaces_starts_at: datetime | None = None
    status: Literal["pending", "taken", "superseded", "expired"] = "pending"
    expires_at: datetime | None = None


class BookingResult(BaseModel):
    """Why a booking did or did not happen.

    Three outcomes rather than an optional appointment, because the desk says
    something different for each: confirmed, "that one just went, here are
    others", and "I could not do it, someone will call you".
    """

    # limit: the contact already holds MAX_UPCOMING_APPOINTMENTS; stale_reschedule:
    # the appointment being moved is no longer theirs to move (moved already,
    # cancelled, past).
    outcome: Literal["booked", "taken", "failed", "limit", "stale_reschedule"]
    appointment: Appointment | None = None


class MailTriage(BaseModel):
    """How one inbound e-mail should be handled.

    `priority` is 1 (most urgent) to 5. It is a number rather than a label
    because the queue is sorted by it, and "urgent"/"very urgent" is not.
    """

    category: str = "other"
    priority: int = Field(default=3, ge=1, le=5)
    needs_human: bool = True
    suggested_reply: str | None = None
    summary: str = ""


class Task(BaseModel):
    id: int | None = None
    title: str
    detail: str | None = None
    due_on: date | None = None
    contact_id: int | None = None
    thread_id: int | None = None
    status: Literal["open", "done", "cancelled"] = "open"


class MeetingNotes(BaseModel):
    appointment_id: int | None = None
    summary: str
    decisions: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)


class AgentReply(BaseModel):
    """What the assistant says back, plus what it decided while saying it."""

    text: str
    intent: Intent = "other"
    needs_human: bool = False
    ends_conversation: bool = False


class ProposalQuote(BaseModel):
    """The preliminary quote of a proposal request (proposal_quotes), for the
    team only. calc_* is what the table gave; setup/monthly the current values,
    in cents."""

    briefing_id: int
    inputs: dict
    scope: dict
    detail: dict
    premises: list[str]
    table_digest: str
    calc_setup_cents: int
    calc_setup_low_cents: int
    calc_setup_high_cents: int
    calc_monthly_cents: int
    setup_cents: int
    monthly_cents: int
    # How the setup is paid (pricing.SETUP_TERMS), chosen by the team.
    forma_implantacao: str = "metade"
    note: str | None = None
    edited_by: int | None = None
    edited_at: datetime | None = None
    created_at: datetime


class ProposalBriefing(BaseModel):
    """A "proposal by e-mail" questionnaire (proposal_briefings). `step` is the
    question being asked; `prefilled` the answers that came from what we already
    knew. The two notice stamps record what the team was told."""

    id: int
    contact_id: int
    status: Literal["open", "sent", "incomplete_sent", "cancelled", "proposal_sent"]
    step: str | None = None
    answers: dict[str, str] = Field(default_factory=dict)
    prefilled: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None = None
    notified_at: datetime | None = None
    incomplete_notified_at: datetime | None = None
    proposal_sent_at: datetime | None = None
    proposal_sent_by: int | None = None
