"""Downloading what a customer sent, with a cap checked while it streams.

The size a channel declares is checked first, but never trusted alone: the
body is read in chunks and the download stops at the chunk that crosses the
cap. The result says what happened — the bytes, too large, or failed with a
technical reason — and the reason never carries any of the content.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Downloaded:
    data: bytes
    mime: str | None


@dataclass(frozen=True)
class TooLarge:
    declared: int | None  # what the channel said, when it said it


@dataclass(frozen=True)
class Failed:
    reason: str  # technical only: a status code, an exception class, "timeout"


Download = Downloaded | TooLarge | Failed


def declared_over(size: object, max_bytes: int) -> int | None:
    """The declared size when it is a number over the cap, else None."""
    return size if isinstance(size, int) and size > max_bytes else None


async def fetch_capped(client: httpx.AsyncClient, url: str, *, headers: dict | None = None,
                       max_bytes: int) -> Download:
    try:
        async with client.stream("GET", url, headers=headers) as response:
            if not response.is_success:
                return Failed(f"status {response.status_code}")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    return TooLarge(None)
                chunks.append(chunk)
            return Downloaded(b"".join(chunks), response.headers.get("content-type"))
    except httpx.HTTPError as exc:
        return Failed(type(exc).__name__)
