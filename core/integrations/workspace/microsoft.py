"""Microsoft 365 connector: Outlook calendar and mail, OneDrive/SharePoint, Excel.

One Graph app registration with application permissions reaches every mailbox
and drive in the tenant, so the narrowing happens in configuration: the agents
only ever address the UPNs and the drive named in `.env`.

Time zones are pinned on the way in and out. Graph answers in UTC unless it is
asked otherwise, and a site meeting rendered three hours early is the kind of
bug nobody reports as a bug — they just stop trusting the bot.
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from core.config import settings
from core.integrations.workspace.errors import (
    WorkspaceAuthError,
    WorkspaceError,
    WorkspaceRateLimited,
    WorkspaceTransient,
)
from core.integrations.workspace.models import (
    Attendee,
    AttendeeResponse,
    CalendarEvent,
    FileRef,
    Gone,
    MailAddress,
    MailMessage,
    NotFound,
    OutgoingMail,
    SheetValues,
    TimeSlot,
)
from core.integrations.workspace.oauth import microsoft_access_token

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.microsoft.com/v1.0"
_TAGS = re.compile(r"<[^>]+>")

_RESPONSE_STATUS: dict[str, AttendeeResponse] = {
    "accepted": "accepted",
    "organizer": "accepted",
    "declined": "declined",
    "tentativelyAccepted": "tentative",
    "notResponded": "needs_action",
    "none": "needs_action",
}

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=60.0)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _strip_html(html: str) -> str:
    return _TAGS.sub(" ", html).replace("&nbsp;", " ").strip()


def _graph_datetime(value: dict | None) -> datetime:
    """Parse Graph's {"dateTime": "...", "timeZone": "..."} pair.

    Graph emits fractional seconds with 7 digits, which `fromisoformat` rejects
    before Python 3.11 and still rejects when a trailing 'Z' is present.
    """
    raw = (value or {}).get("dateTime") or "1970-01-01T00:00:00"
    raw = raw.replace("Z", "")
    if "." in raw:
        head, _, fraction = raw.partition(".")
        raw = f"{head}.{fraction[:6]}"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        from zoneinfo import ZoneInfo

        zone = (value or {}).get("timeZone") or "UTC"
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(zone))
        except Exception:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed


def _split_sheet_range(cell_range: str) -> tuple[str, str]:
    """Split "Plan1!A1:D10" into a worksheet name and an address.

    Without a sheet prefix the first worksheet is used, which is what a caller
    passing plain "A1:D10" means.
    """
    if "!" in cell_range:
        sheet, _, address = cell_range.partition("!")
        return sheet.strip("'"), address
    return "", cell_range


class MicrosoftWorkspace:
    """One Microsoft 365 tenant, reached with app-only Graph credentials."""

    provider = "microsoft"

    def __init__(self, default_upn: str | None = None):
        self.default_upn = default_upn or settings.ms_default_upn or ""

    # ── transport ────────────────────────────────────────────────────────────
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any | None = None,
        content: bytes | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        token = await microsoft_access_token()
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Prefer": f'outlook.timezone="{settings.office_timezone}"',
        }
        request_headers.update(headers or {})
        try:
            response = await _http().request(
                method,
                f"{_GRAPH}{path}",
                params=params,
                json=json,
                content=content,
                headers=request_headers,
            )
        except httpx.HTTPError as exc:
            # The httpx error carries the URL (and the path holds the mailbox):
            # neither its text nor itself as a cause may reach a log.
            raise WorkspaceTransient(f"Graph {method} falhou ({type(exc).__name__})") from None

        status = response.status_code
        if status in (401, 403):
            raise WorkspaceAuthError(f"Graph recusou {method} ({status})", status=status)
        if status in (429, 503):
            retry_after = response.headers.get("retry-after")
            raise WorkspaceRateLimited(
                "Graph asked us to back off",
                float(retry_after) if retry_after else None,
                status=status,
            )
        if status >= 500:
            raise WorkspaceTransient(f"Graph {method} falhou ({status})", status=status)
        if status >= 400:
            raise WorkspaceError(f"Graph {method} falhou ({status})", status=status)
        return response

    def _user(self, upn: str | None) -> str:
        target = upn or self.default_upn
        if not target:
            raise WorkspaceError(
                "No Microsoft 365 user configured. Set MS_DEFAULT_UPN or pass one."
            )
        return target

    def _drive_root(self, upn: str | None = None) -> str:
        """The drive to address: an explicit library, or the user's OneDrive."""
        if settings.ms_drive_id:
            return f"/drives/{settings.ms_drive_id}"
        return f"/users/{self._user(upn)}/drive"

    # ── calendar ─────────────────────────────────────────────────────────────
    def _to_event(self, raw: dict, calendar_id: str) -> CalendarEvent:
        body = raw.get("body") or {}
        all_day = bool(raw.get("isAllDay"))

        def moment(value: dict | None) -> datetime:
            parsed = _graph_datetime(value)
            if not all_day:
                return parsed
            # An all-day event is a civil date: midnight in the office's zone.
            return datetime.combine(parsed.date(), time(0), ZoneInfo(settings.office_timezone))

        description = body.get("content") or ""
        if body.get("contentType") == "html":
            description = _strip_html(description)
        return CalendarEvent(
            id=raw.get("id"),
            calendar_id=calendar_id,
            title=raw.get("subject") or "(sem título)",
            description=description or None,
            location=((raw.get("location") or {}).get("displayName")) or None,
            start=moment(raw.get("start")),
            end=moment(raw.get("end")),
            attendees=[
                Attendee(
                    email=(person.get("emailAddress") or {}).get("address", ""),
                    name=(person.get("emailAddress") or {}).get("name"),
                    response=_RESPONSE_STATUS.get(
                        (person.get("status") or {}).get("response", "none"), "needs_action"
                    ),
                )
                for person in raw.get("attendees", [])
                if (person.get("emailAddress") or {}).get("address")
            ],
            organizer=((raw.get("organizer") or {}).get("emailAddress") or {}).get("address"),
            web_link=raw.get("webLink"),
            provider="microsoft",
            all_day=all_day,
            status="tentative" if raw.get("showAs") == "tentative" else "confirmed",
            etag=raw.get("@odata.etag"),
            ical_uid=raw.get("iCalUId"),
            # A plain DateTimeOffset string, unlike start/end.
            original_start=(datetime.fromisoformat(raw["originalStart"].replace("Z", "+00:00"))
                            if raw.get("originalStart") else None),
            organizer_is_self=bool(raw.get("isOrganizer")),
        )

    def _event_body(self, event: CalendarEvent) -> dict:
        timezone = settings.office_timezone
        return {
            "subject": event.title,
            "body": {"contentType": "text", "content": event.description or ""},
            "location": {"displayName": event.location or ""},
            "start": {"dateTime": event.start.isoformat(), "timeZone": timezone},
            "end": {"dateTime": event.end.isoformat(), "timeZone": timezone},
            "attendees": [
                {
                    "emailAddress": {"address": attendee.email, "name": attendee.name},
                    "type": "required",
                }
                for attendee in event.attendees
            ],
        }

    async def list_events(
        self, calendar_id: str, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        # calendarView expands recurrences; /events would return the series master.
        # Every page or nothing: the next link already carries the query.
        path = f"/users/{self._user(calendar_id)}/calendarView"
        params: dict | None = {
            "startDateTime": start.isoformat(),
            "endDateTime": end.isoformat(),
            "$orderby": "start/dateTime",
            "$top": 250,
        }
        events: list[CalendarEvent] = []
        while True:
            body = (await self._request("GET", path, params=params)).json()
            events.extend(self._to_event(item, calendar_id) for item in body.get("value", []))
            link = body.get("@odata.nextLink")
            if not link:
                return events
            path, params = link.removeprefix(_GRAPH), None

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent | Gone | NotFound:
        """One event. Graph deletes a cancelled event, so 404 is NotFound and
        there is no Gone short of 410."""
        try:
            response = await self._request(
                "GET", f"/users/{self._user(calendar_id)}/events/{event_id}")
        except WorkspaceError as exc:
            if exc.status == 410:
                return Gone()
            if exc.status == 404:
                return NotFound()
            raise
        return self._to_event(response.json(), calendar_id)

    async def create_event(self, calendar_id: str, event: CalendarEvent) -> CalendarEvent:
        response = await self._request(
            "POST",
            f"/users/{self._user(calendar_id)}/events",
            json=self._event_body(event),
        )
        return self._to_event(response.json(), calendar_id)

    async def update_event(
        self, calendar_id: str, event_id: str, event: CalendarEvent
    ) -> CalendarEvent:
        response = await self._request(
            "PATCH",
            f"/users/{self._user(calendar_id)}/events/{event_id}",
            json=self._event_body(event),
        )
        return self._to_event(response.json(), calendar_id)

    async def cancel_event(self, calendar_id: str, event_id: str) -> bool:
        # /cancel rather than DELETE: it notifies the attendees, which is the
        # difference between a cancelled meeting and one that silently vanishes.
        await self._request(
            "POST",
            f"/users/{self._user(calendar_id)}/events/{event_id}/cancel",
            json={"Comment": "Cancelado pelo assistente."},
        )
        return True

    async def busy_periods(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[TimeSlot]]:
        response = await self._request(
            "POST",
            f"/users/{self._user(calendar_ids[0] if calendar_ids else None)}"
            "/calendar/getSchedule",
            json={
                "schedules": calendar_ids,
                "startTime": {"dateTime": start.isoformat(), "timeZone": settings.office_timezone},
                "endTime": {"dateTime": end.isoformat(), "timeZone": settings.office_timezone},
                "availabilityViewInterval": 30,
            },
        )
        periods: dict[str, list[TimeSlot]] = {}
        for schedule in response.json().get("value", []):
            identifier = schedule.get("scheduleId", "")
            periods[identifier] = [
                TimeSlot(
                    start=_graph_datetime(item.get("start")),
                    end=_graph_datetime(item.get("end")),
                )
                for item in schedule.get("scheduleItems", [])
                if item.get("status") in ("busy", "oof", "tentative")
            ]
        return periods

    # ── mail ─────────────────────────────────────────────────────────────────
    def _to_message(self, raw: dict) -> MailMessage:
        body = raw.get("body") or {}
        text = body.get("content") or raw.get("bodyPreview") or ""
        if body.get("contentType") == "html":
            text = _strip_html(text)
        sender = (raw.get("from") or {}).get("emailAddress") or {}
        return MailMessage(
            id=raw.get("id", ""),
            thread_id=raw.get("conversationId"),
            sender=MailAddress(email=sender.get("address", ""), name=sender.get("name"))
            if sender.get("address")
            else None,
            to=[
                MailAddress(
                    email=(person.get("emailAddress") or {}).get("address", ""),
                    name=(person.get("emailAddress") or {}).get("name"),
                )
                for person in raw.get("toRecipients", [])
                if (person.get("emailAddress") or {}).get("address")
            ],
            subject=raw.get("subject") or "",
            body_text=text.strip(),
            received_at=(
                datetime.fromisoformat(raw["receivedDateTime"].replace("Z", "+00:00"))
                if raw.get("receivedDateTime")
                else None
            ),
            has_attachments=bool(raw.get("hasAttachments")),
            is_unread=not raw.get("isRead", False),
            provider="microsoft",
        )

    _MESSAGE_FIELDS = (
        "id,conversationId,subject,bodyPreview,body,from,toRecipients,"
        "receivedDateTime,hasAttachments,isRead"
    )

    async def list_messages(
        self, mailbox: str, query: str | None = None, limit: int = 20
    ) -> list[MailMessage]:
        params: dict[str, Any] = {
            "$top": max(1, min(limit, 100)),
            "$select": self._MESSAGE_FIELDS,
        }
        headers = {}
        if query:
            # $search and $orderby are mutually exclusive on Graph; searching
            # already returns by relevance, so the sort is dropped when set.
            params["$search"] = f'"{query}"'
            headers["ConsistencyLevel"] = "eventual"
        else:
            params["$orderby"] = "receivedDateTime desc"
        response = await self._request(
            "GET",
            f"/users/{self._user(mailbox)}/messages",
            params=params,
            headers=headers,
        )
        return [self._to_message(item) for item in response.json().get("value", [])]

    async def get_message(self, mailbox: str, message_id: str) -> MailMessage:
        response = await self._request(
            "GET",
            f"/users/{self._user(mailbox)}/messages/{message_id}",
            params={"$select": self._MESSAGE_FIELDS},
        )
        return self._to_message(response.json())

    async def send_message(self, mailbox: str, message: OutgoingMail) -> str:
        user = self._user(mailbox)
        if message.reply_to_message_id:
            await self._request(
                "POST",
                f"/users/{user}/messages/{message.reply_to_message_id}/reply",
                json={"comment": message.body_text},
            )
            return message.reply_to_message_id

        payload = {
            "message": {
                "subject": message.subject,
                "body": {"contentType": "text", "content": message.body_text},
                "toRecipients": [
                    {"emailAddress": {"address": address}} for address in message.to
                ],
                "ccRecipients": [
                    {"emailAddress": {"address": address}} for address in message.cc
                ],
                "bccRecipients": [
                    {"emailAddress": {"address": address}} for address in message.bcc
                ],
                "attachments": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": attachment.filename,
                        "contentType": attachment.content_type,
                        "contentBytes": base64.b64encode(attachment.data).decode(),
                    }
                    for attachment in message.attachments
                ],
            },
            "saveToSentItems": True,
        }
        await self._request("POST", f"/users/{user}/sendMail", json=payload)
        # sendMail answers 202 with no body: Graph does not hand back an id here.
        return ""

    async def mark_read(self, mailbox: str, message_id: str) -> bool:
        await self._request(
            "PATCH",
            f"/users/{self._user(mailbox)}/messages/{message_id}",
            json={"isRead": True},
        )
        return True

    # ── files ────────────────────────────────────────────────────────────────
    def _to_file(self, raw: dict) -> FileRef:
        return FileRef(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            mime_type=(raw.get("file") or {}).get("mimeType")
            or ("application/vnd.microsoft.folder" if raw.get("folder") else None),
            size_bytes=raw.get("size"),
            parent_id=(raw.get("parentReference") or {}).get("id"),
            web_link=raw.get("webUrl"),
            modified_at=(
                datetime.fromisoformat(raw["lastModifiedDateTime"].replace("Z", "+00:00"))
                if raw.get("lastModifiedDateTime")
                else None
            ),
            provider="microsoft",
        )

    async def list_files(
        self,
        folder_id: str | None = None,
        name_contains: str | None = None,
        limit: int = 50,
    ) -> list[FileRef]:
        root = self._drive_root()
        if name_contains:
            escaped = name_contains.replace("'", "''")
            path = f"{root}/items/{folder_id}/search(q='{escaped}')" if folder_id else (
                f"{root}/root/search(q='{escaped}')"
            )
        else:
            path = f"{root}/items/{folder_id}/children" if folder_id else f"{root}/root/children"
        response = await self._request(
            "GET", path, params={"$top": max(1, min(limit, 200))}
        )
        return [self._to_file(item) for item in response.json().get("value", [])]

    async def ensure_folder(self, name: str, parent_id: str | None = None) -> FileRef:
        """Get-or-create, in that order.

        Graph's own conflict behaviours do not offer this: "replace" would
        delete an existing month's folder and every report in it, and "rename"
        would quietly create "2026-09 1". So an existing child wins, and only a
        genuine 409 race falls back to a second lookup.
        """
        for child in await self.list_files(folder_id=parent_id, limit=200):
            if child.name == name and child.mime_type == "application/vnd.microsoft.folder":
                return child

        root = self._drive_root()
        parent = f"{root}/items/{parent_id}" if parent_id else f"{root}/root"
        try:
            response = await self._request(
                "POST",
                f"{parent}/children",
                json={
                    "name": name,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail",
                },
            )
        except WorkspaceError:
            # Another replica created it between the listing and the POST.
            for child in await self.list_files(folder_id=parent_id, limit=200):
                if child.name == name:
                    return child
            raise
        return self._to_file(response.json())

    async def upload(
        self,
        name: str,
        data: bytes,
        mime_type: str,
        folder_id: str | None = None,
    ) -> FileRef:
        root = self._drive_root()
        parent = f"{root}/items/{folder_id}" if folder_id else f"{root}/root"
        response = await self._request(
            "PUT",
            f"{parent}:/{name}:/content",
            content=data,
            headers={"Content-Type": mime_type},
        )
        return self._to_file(response.json())

    async def download(self, file_id: str) -> bytes:
        response = await self._request("GET", f"{self._drive_root()}/items/{file_id}/content")
        return response.content

    # ── workbooks (Excel on Graph) ───────────────────────────────────────────
    async def _first_sheet(self, file_id: str) -> str:
        response = await self._request(
            "GET", f"{self._drive_root()}/items/{file_id}/workbook/worksheets"
        )
        sheets = response.json().get("value", [])
        if not sheets:
            raise WorkspaceError(f"Workbook {file_id} has no worksheets.")
        return sheets[0].get("name", "Sheet1")

    async def read_range(self, file_id: str, cell_range: str) -> SheetValues:
        sheet, address = _split_sheet_range(cell_range)
        sheet = sheet or await self._first_sheet(file_id)
        base = f"{self._drive_root()}/items/{file_id}/workbook/worksheets('{sheet}')"
        path = f"{base}/range(address='{address}')" if address else f"{base}/usedRange"
        response = await self._request("GET", path)
        body = response.json()
        return SheetValues(
            range=body.get("address", cell_range),
            values=[
                ["" if cell is None else str(cell) for cell in row]
                for row in body.get("values", [])
            ],
        )

    async def append_rows(
        self, file_id: str, cell_range: str, rows: list[list[str]]
    ) -> int:
        """Write below the used range.

        Graph has no append verb for a plain worksheet — only for a formatted
        table — so the row after `usedRange` is computed and written directly.
        """
        if not rows:
            return 0
        sheet, _ = _split_sheet_range(cell_range)
        sheet = sheet or await self._first_sheet(file_id)
        base = f"{self._drive_root()}/items/{file_id}/workbook/worksheets('{sheet}')"
        used = await self._request("GET", f"{base}/usedRange")
        first_free_row = int(used.json().get("rowCount", 0)) + 1
        width = max(len(row) for row in rows)
        last_column = chr(ord("A") + width - 1) if width <= 26 else "Z"
        address = f"A{first_free_row}:{last_column}{first_free_row + len(rows) - 1}"
        await self._request(
            "PATCH",
            f"{base}/range(address='{address}')",
            json={"values": [list(row) + [""] * (width - len(row)) for row in rows]},
        )
        return len(rows)

    async def update_range(
        self, file_id: str, cell_range: str, rows: list[list[str]]
    ) -> int:
        sheet, address = _split_sheet_range(cell_range)
        sheet = sheet or await self._first_sheet(file_id)
        base = f"{self._drive_root()}/items/{file_id}/workbook/worksheets('{sheet}')"
        await self._request(
            "PATCH", f"{base}/range(address='{address}')", json={"values": rows}
        )
        return len(rows)
