"""The four capabilities the agents actually need from a workspace suite.

These are Protocols, not base classes, on purpose: `GoogleWorkspace` and
`MicrosoftWorkspace` are written independently against the same call shapes,
and a test double only has to implement the two or three methods a test
exercises.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from core.integrations.workspace.models import (
    CalendarEvent,
    FileRef,
    Gone,
    MailMessage,
    NotFound,
    OutgoingMail,
    SheetValues,
    TimeSlot,
)


@runtime_checkable
class CalendarPort(Protocol):
    async def list_events(
        self, calendar_id: str, start: datetime, end: datetime
    ) -> list[CalendarEvent]: ...

    async def get_event(
        self, calendar_id: str, event_id: str
    ) -> CalendarEvent | Gone | NotFound: ...

    async def create_event(self, calendar_id: str, event: CalendarEvent) -> CalendarEvent: ...

    async def update_event(
        self, calendar_id: str, event_id: str, event: CalendarEvent
    ) -> CalendarEvent: ...

    async def cancel_event(self, calendar_id: str, event_id: str) -> bool: ...

    async def busy_periods(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[TimeSlot]]: ...


@runtime_checkable
class MailPort(Protocol):
    async def list_messages(
        self, mailbox: str, query: str | None = None, limit: int = 20
    ) -> list[MailMessage]: ...

    async def get_message(self, mailbox: str, message_id: str) -> MailMessage: ...

    async def send_message(self, mailbox: str, message: OutgoingMail) -> str: ...

    async def mark_read(self, mailbox: str, message_id: str) -> bool: ...


@runtime_checkable
class MailSenderPort(Protocol):
    """Just sending: what Spacemail offers, and all that reminders and
    documents need."""

    async def send_message(self, mailbox: str, message: OutgoingMail) -> str: ...


@runtime_checkable
class FilesPort(Protocol):
    async def list_files(
        self, folder_id: str | None = None, name_contains: str | None = None, limit: int = 50
    ) -> list[FileRef]: ...

    async def ensure_folder(self, name: str, parent_id: str | None = None) -> FileRef: ...

    async def upload(
        self,
        name: str,
        data: bytes,
        mime_type: str,
        folder_id: str | None = None,
    ) -> FileRef: ...

    async def download(self, file_id: str) -> bytes: ...


@runtime_checkable
class SheetsPort(Protocol):
    """Google Sheets and Excel-on-Graph, reduced to reading and appending rows.

    Deliberately narrow: the budget and the cost tables are spreadsheets the
    client already maintains, and the agent's job is to read them and append to
    them — never to restructure someone else's workbook.
    """

    async def read_range(self, file_id: str, cell_range: str) -> SheetValues: ...

    async def append_rows(
        self, file_id: str, cell_range: str, rows: list[list[str]]
    ) -> int: ...

    async def update_range(
        self, file_id: str, cell_range: str, rows: list[list[str]]
    ) -> int: ...
