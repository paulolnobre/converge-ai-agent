"""Keeping what a customer sends, for the Inbox (spec 2026-10-22, §2 and §7).

The turn calls three things, in this order:

- `guard_event`, right after the chat is locked: an update with an attachment
  that was already handled (its mark is in media_events), or too old to still
  be trusted, ends the turn there — before the late path, the rate limit or
  any download. With capture on, the update is marked in the turn's own
  transaction: if the turn falls, the mark falls with it.
- `normalized`: with capture off, an update converted while it was on is
  treated as today — no `_media`, and no WhatsApp caption the conversion added.
- `capture`: download once, seal to disk, record the attachment with its own
  line. Every way this can go wrong is a status on the record, never an
  exception that takes the turn down — except the turn's own cancellation.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import psycopg

from core import channels, content
from core.config import MEDIA_EVENT_DAYS, settings
from core.integrations.media_download import Downloaded, TooLarge
from core.models import Contact
from core.repositories import media as media_repo
from core.services import media_store
from core.services.media_types import Attachment, Kind, attachment_of, sniff

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Captured:
    kind: Kind
    status: str
    audio: bytes | None  # an audio's bytes when it was downloaded, for the transcript


class SameAttachment:
    """The attachment is already on record (a caption edit): no download, no
    new line; the turn goes on with the caption only."""

    def __repr__(self) -> str:
        return "SAME_ATTACHMENT"


SAME_ATTACHMENT = SameAttachment()


def deadlines(kind: Kind | str, *, stored_at: datetime | None,
              created_at: datetime) -> tuple[datetime | None, datetime]:
    """(hold_until, expires_at). A record without a file keeps only its name,
    for MEDIA_RETENTION_DAYS; an audio is held 48 h, and at most 168 h."""
    if stored_at is None:
        return None, created_at + timedelta(days=settings.media_retention_days)
    if kind == "audio":
        return (stored_at + timedelta(hours=settings.media_audio_hours),
                stored_at + timedelta(hours=settings.media_audio_max_hours))
    return None, stored_at + timedelta(days=settings.media_retention_days)


def _written_at(message: dict) -> datetime:
    stamp = message.get("edit_date") or message.get("date")
    if isinstance(stamp, int | float):
        return datetime.fromtimestamp(stamp, tz=UTC)
    return datetime.now(UTC)


async def guard_event(conn, update: dict, message: dict) -> Literal["replayed", "too_old"] | None:
    if attachment_of(message) is None:
        return None
    update_id = str(update.get("update_id"))
    # Read even with capture off: an event handled while it was on stays handled.
    if await media_repo.event_seen(conn, update_id):
        return "replayed"
    if not (settings.media_enabled or "_media" in message):
        return None
    if datetime.now(UTC) - _written_at(message) > timedelta(days=MEDIA_EVENT_DAYS):
        logger.warning("A media update older than %d days was not handled", MEDIA_EVENT_DAYS)
        return "too_old"
    # In the turn's transaction, capture on or off (spec §2, 1a): a payload
    # still carrying _media is marked even after capture was turned off.
    await media_repo.mark_event(conn, update_id)
    return None


def normalized(message: dict) -> dict:
    if settings.media_enabled or "_media" not in message:
        return message
    plain = {key: value for key, value in message.items() if key not in ("_media", "caption")}
    return plain


def _new(contact: Contact, chat_id, found: Attachment, *, status: str,
         created_at: datetime, stored_at: datetime | None = None, mime: str | None = None,
         stored_bytes: int | None = None, storage_key: uuid.UUID | None = None,
         declared: int | None = None) -> media_repo.NewMedia:
    hold_until, expires_at = deadlines(found.kind, stored_at=stored_at, created_at=created_at)
    assert contact.id is not None
    return media_repo.NewMedia(
        contact_id=contact.id, chat_id=str(chat_id), source_id=found.source_id, kind=found.kind,
        mime=mime, declared_bytes=declared if declared is not None else found.declared_bytes,
        stored_bytes=stored_bytes, filename=found.filename, storage_key=storage_key,
        status=status, stored_at=stored_at, hold_until=hold_until, expires_at=expires_at)


async def capture(conn, contact: Contact, chat_id, message: dict, *,
                  edited: bool) -> Captured | SameAttachment | None:
    """None: nothing could be recorded (an SQL error rolled the savepoint
    back); the turn goes on as it would without capture."""
    found = attachment_of(message)
    if found is None or contact.id is None:
        return None
    if await media_repo.exists(conn, found.source_id):
        if not edited:
            logger.warning("A new update carried an attachment already on record")
        return SAME_ATTACHMENT

    now = datetime.now(UTC)
    fetched = await channels.fetch_attachment(found.file_id, declared=found.declared_bytes)
    audio: bytes | None = None
    if isinstance(fetched, Downloaded):
        data = fetched.data
        audio = data if found.kind == "audio" else None
        mime, _ = sniff(data, found.kind)
        total = await media_repo.stored_total(conn)
        new = _new(contact, chat_id, found, status="no_space", created_at=now, mime=mime)
        try:
            if media_store.has_room(len(data), total):
                key = uuid.uuid4()
                media_store.write(key, data)
                new = _new(contact, chat_id, found, status="stored", created_at=now,
                           stored_at=now, mime=mime, stored_bytes=len(data), storage_key=key)
            else:
                logger.error("Media not kept: no space (MEDIA_MAX_TOTAL_MB/MEDIA_MIN_FREE_MB)")
        except Exception as exc:
            logger.error("Media not kept: %s (%s)", type(exc).__name__, found.source_id)
            new = _new(contact, chat_id, found, status="failed", created_at=now, mime=mime)
    elif isinstance(fetched, TooLarge):
        new = _new(contact, chat_id, found, status="too_large", created_at=now,
                   declared=fetched.declared)
    else:
        logger.error("Media not downloaded: %s (%s)", fetched.reason, found.source_id)
        new = _new(contact, chat_id, found, status="failed", created_at=now)

    try:
        media_id = await media_repo.record(conn, content.text(f"media.line_{found.kind}"), new)
    except psycopg.Error as exc:
        logger.error("Media not recorded: %s (%s)", type(exc).__name__, found.source_id)
        return None
    if media_id is None:
        return SAME_ATTACHMENT
    return Captured(kind=found.kind, status=new.status, audio=audio)


async def record_without_download(conn, contact: Contact, chat_id, found: Attachment,
                                  status: Literal["late", "rate_limited"]) -> None:
    """A record and its line, nothing downloaded (a late message, a flood).
    An attachment already on record keeps its record and its file."""
    if contact.id is None or await media_repo.exists(conn, found.source_id):
        return
    new = _new(contact, chat_id, found, status=status, created_at=datetime.now(UTC))
    try:
        await media_repo.record(conn, content.text(f"media.line_{found.kind}"), new)
    except psycopg.Error as exc:
        logger.error("Media not recorded: %s (%s)", type(exc).__name__, found.source_id)
