"""Provider-neutral shapes exchanged with Google Workspace and Microsoft 365.

Everything above this module speaks these models. Swapping a tenant from Google
to Microsoft changes which implementation is constructed and nothing else.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Provider = Literal["google", "microsoft"]
# Google calls it responseStatus and Graph calls it status.response;
# both fold into these four values.
AttendeeResponse = Literal["accepted", "declined", "tentative", "needs_action"]


class Attendee(BaseModel):
    email: str
    name: str | None = None
    response: AttendeeResponse = "needs_action"


class CalendarEvent(BaseModel):
    """One appointment, in the tenant's own time zone.

    `start`/`end` are always timezone-aware: a naive datetime here is the bug
    that books a site meeting an hour off after a DST change.
    """

    id: str | None = None
    calendar_id: str | None = None
    title: str
    description: str | None = None
    location: str | None = None
    start: datetime
    end: datetime
    attendees: list[Attendee] = Field(default_factory=list)
    organizer: str | None = None
    web_link: str | None = None
    # Ask the provider for a video meeting when creating the event (Google
    # Meet on a Google calendar), and the join link it returned.
    online_meeting: bool = False
    meeting_link: str | None = None
    provider: Provider | None = None
    # What the panel's agenda needs to mirror a calendar: an all-day event is a
    # civil date in the office's zone (end exclusive); a tentative one still
    # takes the time; the etag is the provider's version of the event; iCalUID
    # (with the original start of an occurrence) names the same meeting across
    # the calendars it was sent to; and only the organizer's copy is ours.
    all_day: bool = False
    status: Literal["confirmed", "tentative"] = "confirmed"
    etag: str | None = None
    ical_uid: str | None = None
    original_start: datetime | None = None
    organizer_is_self: bool = False


class Gone:
    """`get_event`: the provider says the event was cancelled or deleted."""


class NotFound:
    """`get_event`: 404 — no such event, or no longer allowed to see it.
    Ambiguous on purpose: never enough, alone, to call a meeting cancelled."""


class TimeSlot(BaseModel):
    start: datetime
    end: datetime


class MailAddress(BaseModel):
    email: str
    name: str | None = None


class MailMessage(BaseModel):
    id: str
    thread_id: str | None = None
    sender: MailAddress | None = None
    to: list[MailAddress] = Field(default_factory=list)
    subject: str = ""
    # Plain text only. HTML bodies are converted on the way in, so an LLM never
    # reads markup it might repeat back as instructions.
    body_text: str = ""
    received_at: datetime | None = None
    has_attachments: bool = False
    is_unread: bool = True
    provider: Provider | None = None


class OutgoingMail(BaseModel):
    to: list[str]
    subject: str
    body_text: str
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    reply_to_message_id: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)


class Attachment(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    data: bytes


class FileRef(BaseModel):
    """A file in Google Drive or in a OneDrive/SharePoint document library."""

    id: str
    name: str
    mime_type: str | None = None
    size_bytes: int | None = None
    parent_id: str | None = None
    web_link: str | None = None
    modified_at: datetime | None = None
    provider: Provider | None = None


class SheetValues(BaseModel):
    """A rectangle of cells, as strings — no provider-specific cell objects."""

    range: str
    values: list[list[str]] = Field(default_factory=list)


OutgoingMail.model_rebuild()
