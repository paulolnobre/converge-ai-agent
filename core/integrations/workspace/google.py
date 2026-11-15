"""Google Workspace connector: Calendar, Gmail, Drive and Sheets over REST.

Written directly against the JSON APIs with httpx rather than through
`google-api-python-client`, for one reason: that client is synchronous, and one
blocking Drive upload inside this event loop stalls every other conversation the
bot is holding. `google-auth` is still used, but only to mint tokens
(see `workspace/oauth.py`) — and only in a worker thread.
"""
from __future__ import annotations

import base64
import json as jsonlib
import logging
import re
import uuid
from datetime import datetime, time
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
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
from core.integrations.workspace.oauth import google_access_token

logger = logging.getLogger(__name__)

_CALENDAR = "https://www.googleapis.com/calendar/v3"
_GMAIL = "https://gmail.googleapis.com/gmail/v1"
_DRIVE = "https://www.googleapis.com/drive/v3"
_DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
_SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"

_FOLDER_MIME = "application/vnd.google-apps.folder"
_TAGS = re.compile(r"<[^>]+>")

_RESPONSE_STATUS: dict[str, AttendeeResponse] = {
    "accepted": "accepted",
    "declined": "declined",
    "tentative": "tentative",
    "needsAction": "needs_action",
}

_client: httpx.AsyncClient | None = None


def _host(url: str) -> str:
    """Only the host of a request, for an error message."""
    return httpx.URL(url).host


def _rate_limited(response: httpx.Response) -> bool:
    """Google's quota refusals come as 403 with a reason in the body; they
    pass, unlike a 403 for a missing permission. Only the reason is read."""
    try:
        errors = (response.json().get("error") or {}).get("errors") or []
    except ValueError:
        return False
    return any(item.get("reason") in ("rateLimitExceeded", "userRateLimitExceeded")
               for item in errors if isinstance(item, dict))


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


def _b64url_decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", "replace")


class GoogleWorkspace:
    """One tenant's Google Workspace, reached with one set of credentials.

    `subject` is the account being acted for under domain-wide delegation. The
    Gmail and Drive APIs address it as the literal string `me`, so it only has
    to appear in the token, not in every URL.
    """

    provider = "google"

    def __init__(self, subject: str | None = None):
        self.subject = subject or settings.google_delegated_subject or None

    # ── transport ────────────────────────────────────────────────────────────
    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: Any | None = None,
        content: bytes | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        token = await google_access_token(self.subject)
        request_headers = {"Authorization": f"Bearer {token}"}
        request_headers.update(headers or {})
        try:
            response = await _http().request(
                method,
                url,
                params=params,
                json=json,
                content=content,
                headers=request_headers,
            )
        except httpx.HTTPError as exc:
            # The httpx error carries the URL: neither its text nor itself
            # (as a chained cause) may reach a log.
            raise WorkspaceTransient(
                f"Google {method} {_host(url)} falhou ({type(exc).__name__})"
            ) from None

        status = response.status_code
        if status == 403 and _rate_limited(response):
            raise WorkspaceRateLimited("Google asked us to back off", None, status=status)
        if status in (401, 403):
            # 403 is also Google's "insufficient scope", which is the same fix:
            # re-authorize with the scope the deployment actually needs.
            raise WorkspaceAuthError(
                f"Google recusou {method} {_host(url)} ({status})", status=status
            )
        if status in (429, 503):
            retry_after = response.headers.get("retry-after")
            raise WorkspaceRateLimited(
                "Google asked us to back off",
                float(retry_after) if retry_after else None,
                status=status,
            )
        if status >= 500:
            raise WorkspaceTransient(f"Google {method} {_host(url)} falhou ({status})", status=status)
        if status >= 400:
            raise WorkspaceError(f"Google {method} {_host(url)} falhou ({status})", status=status)
        return response

    # ── calendar ─────────────────────────────────────────────────────────────
    def _to_event(self, raw: dict, calendar_id: str) -> CalendarEvent:
        def moment(side: dict) -> datetime:
            # An all-day event has `date` instead of `dateTime`: midnight of
            # that civil date in the office's zone, never the process's.
            if "dateTime" in side:
                return datetime.fromisoformat(side["dateTime"])
            day = datetime.fromisoformat(side["date"]).date()
            return datetime.combine(day, time(0), ZoneInfo(settings.office_timezone))

        video = next(
            (point.get("uri") for point in (raw.get("conferenceData") or {}).get("entryPoints", [])
             if point.get("entryPointType") == "video"),
            None,
        )
        return CalendarEvent(
            id=raw.get("id"),
            calendar_id=calendar_id,
            meeting_link=raw.get("hangoutLink") or video,
            title=raw.get("summary") or "(sem título)",
            description=raw.get("description"),
            location=raw.get("location"),
            start=moment(raw.get("start") or {}),
            end=moment(raw.get("end") or {}),
            attendees=[
                Attendee(
                    email=person.get("email", ""),
                    name=person.get("displayName"),
                    response=_RESPONSE_STATUS.get(
                        person.get("responseStatus", "needsAction"), "needs_action"
                    ),
                )
                for person in raw.get("attendees", [])
                if person.get("email")
            ],
            organizer=(raw.get("organizer") or {}).get("email"),
            web_link=raw.get("htmlLink"),
            provider="google",
            all_day="date" in (raw.get("start") or {}),
            status="tentative" if raw.get("status") == "tentative" else "confirmed",
            etag=raw.get("etag"),
            ical_uid=raw.get("iCalUID"),
            original_start=moment(raw["originalStartTime"]) if raw.get("originalStartTime") else None,
            organizer_is_self=bool((raw.get("organizer") or {}).get("self")),
        )

    def _event_body(self, event: CalendarEvent) -> dict:
        timezone = settings.office_timezone
        return {
            "summary": event.title,
            "description": event.description,
            "location": event.location,
            "start": {"dateTime": event.start.isoformat(), "timeZone": timezone},
            "end": {"dateTime": event.end.isoformat(), "timeZone": timezone},
            "attendees": [
                {"email": attendee.email, "displayName": attendee.name}
                for attendee in event.attendees
            ],
        }

    async def list_events(
        self, calendar_id: str, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        # Every page or nothing: a failure on any page raises, so no caller
        # ever takes a partial list for the whole calendar.
        params: dict = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            # Without singleEvents a recurring weekly site meeting comes back
            # as one master row and the free/busy maths is wrong all week.
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        }
        events: list[CalendarEvent] = []
        while True:
            response = await self._request(
                "GET", f"{_CALENDAR}/calendars/{calendar_id}/events", params=params)
            body = response.json()
            events.extend(self._to_event(item, calendar_id) for item in body.get("items", [])
                          if item.get("status") != "cancelled")
            token = body.get("nextPageToken")
            if not token:
                return events
            params = {**params, "pageToken": token}

    async def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent | Gone | NotFound:
        """One event: Gone when Google says it was cancelled (or 410), NotFound
        on 404, which can also mean the calendar is no longer shared."""
        try:
            response = await self._request(
                "GET", f"{_CALENDAR}/calendars/{calendar_id}/events/{event_id}")
        except WorkspaceError as exc:
            if exc.status == 410:
                return Gone()
            if exc.status == 404:
                return NotFound()
            raise
        body = response.json()
        if body.get("status") == "cancelled":
            return Gone()
        return self._to_event(body, calendar_id)

    async def create_event(self, calendar_id: str, event: CalendarEvent) -> CalendarEvent:
        body = self._event_body(event)
        params: dict = {"sendUpdates": "all"}
        if event.online_meeting:
            # A Meet link, made with the event: it is in the attendees' invite,
            # and comes back as hangoutLink. conferenceDataVersion=1 is what
            # makes Google read createRequest at all.
            body["conferenceData"] = {"createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }}
            params["conferenceDataVersion"] = 1
        response = await self._request(
            "POST", f"{_CALENDAR}/calendars/{calendar_id}/events", params=params, json=body,
        )
        created = self._to_event(response.json(), calendar_id)
        if event.online_meeting and not created.meeting_link and created.id:
            # Creation can still be pending in the first response; one re-read
            # usually has it. Without it the booking stands, just with no link.
            again = await self._request(
                "GET", f"{_CALENDAR}/calendars/{calendar_id}/events/{created.id}"
            )
            created = self._to_event(again.json(), calendar_id)
        return created

    async def update_event(
        self, calendar_id: str, event_id: str, event: CalendarEvent
    ) -> CalendarEvent:
        response = await self._request(
            "PATCH",
            f"{_CALENDAR}/calendars/{calendar_id}/events/{event_id}",
            params={"sendUpdates": "all"},
            json=self._event_body(event),
        )
        return self._to_event(response.json(), calendar_id)

    async def cancel_event(self, calendar_id: str, event_id: str) -> bool:
        await self._request(
            "DELETE",
            f"{_CALENDAR}/calendars/{calendar_id}/events/{event_id}",
            params={"sendUpdates": "all"},
        )
        return True

    async def busy_periods(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> dict[str, list[TimeSlot]]:
        response = await self._request(
            "POST",
            f"{_CALENDAR}/freeBusy",
            json={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "timeZone": settings.office_timezone,
                "items": [{"id": calendar_id} for calendar_id in calendar_ids],
            },
        )
        calendars = response.json().get("calendars", {})
        return {
            calendar_id: [
                TimeSlot(
                    start=datetime.fromisoformat(period["start"]),
                    end=datetime.fromisoformat(period["end"]),
                )
                for period in payload.get("busy", [])
            ]
            for calendar_id, payload in calendars.items()
        }

    # ── mail ─────────────────────────────────────────────────────────────────
    def _headers_of(self, payload: dict) -> dict[str, str]:
        return {
            header.get("name", "").lower(): header.get("value", "")
            for header in payload.get("headers", [])
        }

    def _body_of(self, payload: dict) -> str:
        """Depth-first walk for the best body part Gmail offers.

        text/plain wins; HTML is stripped only when there is nothing else, so a
        marketing email never reaches the model as markup.
        """
        mime = payload.get("mimeType", "")
        data = (payload.get("body") or {}).get("data")
        if data and mime == "text/plain":
            return _b64url_decode(data)
        html_fallback = ""
        for part in payload.get("parts", []):
            found = self._body_of(part)
            if found and part.get("mimeType") == "text/plain":
                return found
            html_fallback = html_fallback or found
        if data and mime == "text/html":
            return _strip_html(_b64url_decode(data))
        return html_fallback

    def _to_message(self, raw: dict) -> MailMessage:
        payload = raw.get("payload") or {}
        headers = self._headers_of(payload)
        received = None
        if headers.get("date"):
            try:
                received = parsedate_to_datetime(headers["date"])
            except (TypeError, ValueError):
                received = None
        return MailMessage(
            id=raw.get("id", ""),
            thread_id=raw.get("threadId"),
            sender=MailAddress(email=headers.get("from", "")) if headers.get("from") else None,
            to=[
                MailAddress(email=address.strip())
                for address in headers.get("to", "").split(",")
                if address.strip()
            ],
            subject=headers.get("subject", ""),
            body_text=self._body_of(payload).strip(),
            received_at=received,
            has_attachments=any(
                part.get("filename") for part in payload.get("parts", [])
            ),
            is_unread="UNREAD" in raw.get("labelIds", []),
            provider="google",
        )

    async def list_messages(
        self, mailbox: str, query: str | None = None, limit: int = 20
    ) -> list[MailMessage]:
        listing = await self._request(
            "GET",
            f"{_GMAIL}/users/{mailbox}/messages",
            params={"q": query or "", "maxResults": max(1, min(limit, 100))},
        )
        messages = []
        for stub in listing.json().get("messages", []):
            messages.append(await self.get_message(mailbox, stub["id"]))
        return messages

    async def get_message(self, mailbox: str, message_id: str) -> MailMessage:
        response = await self._request(
            "GET",
            f"{_GMAIL}/users/{mailbox}/messages/{message_id}",
            params={"format": "full"},
        )
        return self._to_message(response.json())

    async def send_message(self, mailbox: str, message: OutgoingMail) -> str:
        mime = EmailMessage()
        mime["To"] = ", ".join(message.to)
        if message.cc:
            mime["Cc"] = ", ".join(message.cc)
        if message.bcc:
            mime["Bcc"] = ", ".join(message.bcc)
        mime["Subject"] = message.subject
        mime.set_content(message.body_text)
        for attachment in message.attachments:
            maintype, _, subtype = attachment.content_type.partition("/")
            mime.add_attachment(
                attachment.data,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )
        body: dict[str, Any] = {
            "raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()
        }
        if message.reply_to_message_id:
            # Gmail threads by threadId, not by the In-Reply-To header alone.
            original = await self.get_message(mailbox, message.reply_to_message_id)
            body["threadId"] = original.thread_id
        response = await self._request(
            "POST", f"{_GMAIL}/users/{mailbox}/messages/send", json=body
        )
        return response.json().get("id", "")

    async def mark_read(self, mailbox: str, message_id: str) -> bool:
        await self._request(
            "POST",
            f"{_GMAIL}/users/{mailbox}/messages/{message_id}/modify",
            json={"removeLabelIds": ["UNREAD"]},
        )
        return True

    # ── files ────────────────────────────────────────────────────────────────
    _FILE_FIELDS = "id,name,mimeType,size,parents,webViewLink,modifiedTime"

    def _to_file(self, raw: dict) -> FileRef:
        parents = raw.get("parents") or []
        return FileRef(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            mime_type=raw.get("mimeType"),
            size_bytes=int(raw["size"]) if raw.get("size") else None,
            parent_id=parents[0] if parents else None,
            web_link=raw.get("webViewLink"),
            modified_at=(
                datetime.fromisoformat(raw["modifiedTime"].replace("Z", "+00:00"))
                if raw.get("modifiedTime")
                else None
            ),
            provider="google",
        )

    async def list_files(
        self,
        folder_id: str | None = None,
        name_contains: str | None = None,
        limit: int = 50,
    ) -> list[FileRef]:
        clauses = ["trashed = false"]
        if folder_id:
            clauses.append(f"'{folder_id}' in parents")
        if name_contains:
            # Drive's query language has no parameter binding; a stray quote
            # would otherwise change the meaning of the query.
            clauses.append(f"name contains '{name_contains.replace(chr(39), chr(92) + chr(39))}'")
        response = await self._request(
            "GET",
            f"{_DRIVE}/files",
            params={
                "q": " and ".join(clauses),
                "fields": f"files({self._FILE_FIELDS})",
                "pageSize": max(1, min(limit, 200)),
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            },
        )
        return [self._to_file(item) for item in response.json().get("files", [])]

    async def ensure_folder(self, name: str, parent_id: str | None = None) -> FileRef:
        existing = await self.list_files(folder_id=parent_id, name_contains=name, limit=10)
        for candidate in existing:
            if candidate.name == name and candidate.mime_type == _FOLDER_MIME:
                return candidate
        metadata: dict[str, Any] = {"name": name, "mimeType": _FOLDER_MIME}
        if parent_id:
            metadata["parents"] = [parent_id]
        response = await self._request(
            "POST",
            f"{_DRIVE}/files",
            params={"fields": self._FILE_FIELDS, "supportsAllDrives": "true"},
            json=metadata,
        )
        return self._to_file(response.json())

    async def upload(
        self,
        name: str,
        data: bytes,
        mime_type: str,
        folder_id: str | None = None,
    ) -> FileRef:
        """Multipart upload: metadata and bytes in one request.

        Fine up to a few megabytes, which covers a daily report, a photo or a
        DANFE PDF. A resumable session would be needed beyond that.
        """
        metadata: dict[str, Any] = {"name": name}
        if folder_id:
            metadata["parents"] = [folder_id]
        boundary = "workspace-upload-boundary"
        body = b"".join(
            [
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(),
                jsonlib.dumps(metadata).encode(),
                f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode(),
                data,
                f"\r\n--{boundary}--".encode(),
            ]
        )
        response = await self._request(
            "POST",
            f"{_DRIVE_UPLOAD}/files",
            params={
                "uploadType": "multipart",
                "fields": self._FILE_FIELDS,
                "supportsAllDrives": "true",
            },
            content=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )
        return self._to_file(response.json())

    async def download(self, file_id: str) -> bytes:
        response = await self._request(
            "GET",
            f"{_DRIVE}/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        return response.content

    # ── sheets ───────────────────────────────────────────────────────────────
    async def read_range(self, file_id: str, cell_range: str) -> SheetValues:
        response = await self._request(
            "GET", f"{_SHEETS}/{file_id}/values/{cell_range}"
        )
        body = response.json()
        return SheetValues(
            range=body.get("range", cell_range),
            values=[[str(cell) for cell in row] for row in body.get("values", [])],
        )

    async def append_rows(
        self, file_id: str, cell_range: str, rows: list[list[str]]
    ) -> int:
        response = await self._request(
            "POST",
            f"{_SHEETS}/{file_id}/values/{cell_range}:append",
            params={
                # USER_ENTERED so "1.234,56" and "08/09/2026" land as a number
                # and a date, the way a person typing them would.
                "valueInputOption": "USER_ENTERED",
                "insertDataOption": "INSERT_ROWS",
            },
            json={"values": rows},
        )
        updates = response.json().get("updates", {})
        return int(updates.get("updatedRows", 0))

    async def update_range(
        self, file_id: str, cell_range: str, rows: list[list[str]]
    ) -> int:
        response = await self._request(
            "PUT",
            f"{_SHEETS}/{file_id}/values/{cell_range}",
            params={"valueInputOption": "USER_ENTERED"},
            json={"values": rows},
        )
        return int(response.json().get("updatedRows", 0))
