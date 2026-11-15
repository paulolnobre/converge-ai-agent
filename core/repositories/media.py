"""The media kept for the Inbox: one row per attachment, pointing at its own
line of the conversation, and the marks of the updates already handled.

The original file name is a sealed field (core/sealed_fields.py). Nothing here
ever logs a name or a byte.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from core import content
from core.repositories import contacts as contacts_repo
from core.sealed_fields import field, read_lenient, write

_FILENAME = field("media", "filename")

# Whether a kept file may still be opened, on the alias `m` (spec §7): stored,
# before its ceiling, and an audio past its basic 48 h only while a request is
# open in the Inbox for that conversation.
AVAILABLE_SQL = (
    "(m.status = 'stored' AND NOW() < m.expires_at AND ("
    "m.hold_until IS NULL OR NOW() < m.hold_until OR EXISTS ("
    "SELECT 1 FROM handoff_sessions h "
    "WHERE h.chat_id = m.chat_id AND h.status IN ('open', 'claimed'))))"
)


@dataclass
class NewMedia:
    contact_id: int
    chat_id: str
    source_id: str
    kind: str
    mime: str | None
    declared_bytes: int | None
    stored_bytes: int | None
    filename: str | None
    storage_key: UUID | None
    status: str
    stored_at: datetime | None
    hold_until: datetime | None
    expires_at: datetime


class _Conflict(Exception):
    """Another turn recorded this attachment first: undo our line too."""


async def event_seen(conn, update_id: str) -> bool:
    cur = await conn.execute("SELECT 1 FROM media_events WHERE update_id = %s", (update_id,))
    return await cur.fetchone() is not None


async def mark_event(conn, update_id: str) -> None:
    await conn.execute(
        "INSERT INTO media_events (update_id) VALUES (%s) ON CONFLICT DO NOTHING", (update_id,))


async def exists(conn, source_id: str) -> bool:
    cur = await conn.execute("SELECT 1 FROM media WHERE source_id = %s", (source_id,))
    return await cur.fetchone() is not None


async def record(conn, line: str, media: NewMedia) -> int | None:
    """The media's own line and its row, together in a savepoint. When the
    source was recorded meanwhile, both are undone and None comes back."""
    sealed = write(_FILENAME, media.filename)
    try:
        async with conn.transaction():
            session_id = await contacts_repo.record_message(
                conn, media.contact_id, media.chat_id, "user", line, "media")
            cur = await conn.execute(
                "INSERT INTO media (contact_id, session_id, chat_id, source_id, kind, mime, "
                "declared_bytes, stored_bytes, filename, filename_sealed, storage_key, status, "
                "stored_at, hold_until, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (source_id) DO NOTHING RETURNING id",
                (media.contact_id, session_id, str(media.chat_id), media.source_id, media.kind,
                 media.mime, media.declared_bytes, media.stored_bytes, sealed["filename"],
                 sealed["filename_sealed"], media.storage_key, media.status, media.stored_at,
                 media.hold_until, media.expires_at),
            )
            row = await cur.fetchone()
            if row is None:
                raise _Conflict
            return row[0]
    except _Conflict:
        return None


async def stored_total(conn) -> int:
    cur = await conn.execute(
        "SELECT COALESCE(SUM(stored_bytes), 0) FROM media WHERE status = 'stored'")
    row = await cur.fetchone()
    return int(row[0]) if row else 0


@dataclass(frozen=True)
class Servable:
    id: int
    kind: str
    storage_key: UUID
    filename: str | None


_DOWNLOAD = "application/octet-stream"


async def for_sessions(conn, session_ids: list[int]) -> dict[int, dict]:
    return await _for_lines(conn, 'session_id', session_ids)


async def for_admin_messages(conn, message_ids: list[int]) -> dict[int, dict]:
    return await _for_lines(conn, 'admin_message_id', message_ids)


async def _for_lines(conn, column: str, ids: list[int]) -> dict[int, dict]:
    if column not in ('session_id', 'admin_message_id'):
        raise ValueError('Unknown media target')
    if not ids:
        return {}
    cur = await conn.execute(
        f"SELECT m.{column}, m.id, m.kind, m.mime, m.stored_bytes, m.declared_bytes, "
        f"m.status, m.filename, m.filename_sealed, {AVAILABLE_SQL} AS available, "
        "m.delivery_state FROM media m WHERE m." + column + " = ANY(%s)", (list(ids),))
    found = {}
    for (line_id, media_id, kind, mime, stored_bytes, declared_bytes, status, legacy,
         sealed, available, delivery) in await cur.fetchall():
        shown = 'expired' if status == 'stored' else status
        found[line_id] = {
            'id': media_id, 'kind': kind, 'mime': mime, 'stored_bytes': stored_bytes,
            'declared_bytes': declared_bytes, 'status': status, 'available': bool(available),
            'filename': read_lenient(_FILENAME, legacy, sealed, row_id=media_id),
            'inline': bool(available) and mime is not None and mime != _DOWNLOAD,
            'status_text': None if available else content.text(f'media.{shown}'),
            'delivery_state': delivery,
        }
    return found


async def admin_servable(conn, media_id: int) -> Servable | None:
    cur = await conn.execute(
        f"SELECT m.id,m.kind,m.storage_key,m.filename,m.filename_sealed FROM media m "
        f"WHERE m.id=%s AND m.admin_message_id IS NOT NULL AND {AVAILABLE_SQL}", (media_id,))
    row = await cur.fetchone()
    if row is None or row[2] is None:
        return None
    return Servable(row[0],row[1],row[2],read_lenient(_FILENAME,row[3],row[4],row_id=row[0]))


async def servable(conn, contact_id: int, media_id: int) -> Servable | None:
    """The record, only if it is this contact's and may still be opened."""
    cur = await conn.execute(
        f"SELECT m.id, m.kind, m.storage_key, m.filename, m.filename_sealed FROM media m "
        f"WHERE m.id = %s AND m.contact_id = %s AND {AVAILABLE_SQL}", (media_id, contact_id))
    row = await cur.fetchone()
    if row is None or row[2] is None:
        return None
    return Servable(row[0], row[1], row[2],
                    read_lenient(_FILENAME, row[3], row[4], row_id=row[0]))


async def mark_missing(conn, media_id: int) -> None:
    """A stored file that is not on disk. A record the sweep already expired
    stays expired."""
    await conn.execute(
        "UPDATE media SET status = 'missing', filename_sealed = NULL "
        "WHERE id = %s AND status = 'stored'", (media_id,))
