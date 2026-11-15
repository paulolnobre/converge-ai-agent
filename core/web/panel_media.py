"""Serving a kept file to the panel's Inbox (spec 2026-10-22, §6).

Only a record that is the contact's own and may still be opened; the type
from the file's bytes, never from what the sender declared; one range for
the players (Safari plays no audio or video without it); and at most
MEDIA_SERVE_CONCURRENCY files open in memory at once — a slot is taken
before the file is read and given back when the response is done or the
connection drops.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable
from typing import Literal

from fastapi.responses import Response, StreamingResponse

from core.config import settings
from core.crypto import SealedDataError
from core.repositories import media as media_repo
from core.services import media_store
from core.services.media_types import disposition, sniff

logger = logging.getLogger(__name__)

_WAIT_SECONDS: float = 5
_CHUNK = 256 * 1024
# Up to 18 digits: past what a 20 MB file needs, and short of the 4300 that
# int() refuses. A longer number is no range at all: the whole file.
_RANGE = re.compile(r"bytes=(\d{0,18})-(\d{0,18})")
_slots: asyncio.Semaphore | None = None


def _security() -> dict[str, str]:
    return {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
            "Content-Security-Policy": "default-src 'none'; sandbox"}


def reset_slots() -> None:
    global _slots
    _slots = None


def _semaphore() -> asyncio.Semaphore:
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(settings.media_serve_concurrency)
    return _slots


def parse_range(header: str | None, total: int) -> tuple[int, int] | Literal["unsatisfiable"] | None:
    """One range: (start, end) inclusive; "unsatisfiable" outside the file;
    None — the whole file — for no header, several ranges or anything else."""
    if not header:
        return None
    match = _RANGE.fullmatch(header.strip())
    if match is None:
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":
        suffix = int(last)
        if suffix == 0:
            return "unsatisfiable"
        return max(total - suffix, 0), total - 1
    start = int(first)
    end = int(last) if last else total - 1
    if start >= total:
        return "unsatisfiable"
    if end < start:
        return None
    return start, min(end, total - 1)


class _Body:
    """The response body, giving its slot back when it is read to the end or
    closed. A generator would not do: one that never started runs no finally."""

    def __init__(self, body: bytes, release: Callable[[], None]):
        self._body, self._release, self._offset = body, release, 0

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if self._offset >= len(self._body):
            self._release()
            raise StopAsyncIteration
        chunk = self._body[self._offset:self._offset + _CHUNK]
        self._offset += _CHUNK
        return chunk

    async def aclose(self) -> None:
        self._release()


class _FileResponse(StreamingResponse):
    """However the response ends — read to the end, the client gone before the
    first byte, a send that fails — its slot comes back."""

    def __init__(self, content: _Body, release: Callable[[], None], **kwargs):
        super().__init__(content, **kwargs)
        self._release = release

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()


def _empty(status: int, extra: dict[str, str] | None = None) -> Response:
    return Response(status_code=status, headers={**_security(), **(extra or {})})


async def serve(conn, contact_id: int, media_id: int, *, method: str,
                range_header: str | None, download: bool = False) -> Response:
    """`download`: the "Baixar" beside a player — always an attachment, so a
    file the browser cannot play (Ogg on an iPhone) is still saved."""
    found = await media_repo.servable(conn, contact_id, media_id)
    if found is None:
        return _empty(404)
    return await serve_record(conn, found, method=method, range_header=range_header, download=download)


async def serve_record(conn, found: media_repo.Servable, *, method: str, range_header: str | None, download: bool = False) -> Response:
    slots = _semaphore()
    try:
        await asyncio.wait_for(slots.acquire(), timeout=_WAIT_SECONDS)
    except TimeoutError:
        return _empty(503, {"Retry-After": "5"})
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            slots.release()

    try:
        return await _respond(conn, found, method, range_header, download, release)
    except BaseException:
        # Whatever fails before the response owns the slot gives it back.
        release()
        raise


async def _respond(conn, found: media_repo.Servable, method: str, range_header: str | None,
                   download: bool, release: Callable[[], None]) -> Response:
    try:
        data = media_store.read(found.storage_key)
    except FileNotFoundError:
        release()
        await media_repo.mark_missing(conn, found.id)
        return _empty(404)
    except SealedDataError:
        release()
        logger.error("A kept file did not open (media id=%s)", found.id)
        return _empty(404)

    mime, inline = sniff(data, found.kind)  # type: ignore[arg-type]
    if download:
        mime, inline = "application/octet-stream", False
    headers = {**_security(), "Accept-Ranges": "bytes", "Content-Type": mime,
               "Content-Disposition": disposition(found.filename, inline=inline)}
    wanted = parse_range(range_header, len(data))
    if wanted == "unsatisfiable":
        release()
        return Response(status_code=416, headers={**headers,
                                                  "Content-Range": f"bytes */{len(data)}"})
    status, body = 200, data
    if wanted is not None:
        start, end = wanted
        status, body = 206, data[start:end + 1]
        headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
    headers["Content-Length"] = str(len(body))
    if method == "HEAD":
        release()
        return Response(status_code=status, headers=headers)
    return _FileResponse(_Body(body, release), release, status_code=status, headers=headers,
                         media_type=mime)
