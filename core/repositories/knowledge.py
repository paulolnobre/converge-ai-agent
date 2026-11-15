"""The FAQ, chunked and embedded, as the desk reads it.

The only table in this schema that mirrors something in the client's drive. It
is a cache with a hash, not a second source of truth: the document is the
document, and `replace` swaps a source's chunks wholesale so a deleted question
disappears from the desk rather than lingering as an answer nobody can find in
the original.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def replace(conn, source: str, chunks: list[dict]) -> int:
    """Make this source's chunks exactly the ones given.

    Delete-then-insert inside the caller's transaction, because the alternative
    — updating in place — leaves a question that was removed from the document
    still answerable by the desk.
    """
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM knowledge_chunks WHERE source = %s", (source,))
        for chunk in chunks:
            await cur.execute(
                "INSERT INTO knowledge_chunks "
                "(source, heading, body, embedding, content_hash) "
                "VALUES (%s, %s, %s, %s, %s)",
                (source, chunk["heading"], chunk["body"],
                 chunk["embedding"], chunk["content_hash"]),
            )
    return len(chunks)


async def all_chunks(conn, source: str | None = None) -> list[dict]:
    """Every chunk, with its vector. Small by construction — this is an FAQ."""
    async with conn.cursor() as cur:
        if source:
            await cur.execute(
                "SELECT heading, body, embedding, source FROM knowledge_chunks "
                "WHERE source = %s ORDER BY id",
                (source,),
            )
        else:
            await cur.execute(
                "SELECT heading, body, embedding, source FROM knowledge_chunks "
                "ORDER BY id"
            )
        rows = await cur.fetchall()
    return [
        {"heading": row[0], "body": row[1], "embedding": row[2], "source": row[3]}
        for row in rows
    ]


async def hashes(conn, source: str) -> dict[str, str]:
    """heading → content hash, so unchanged questions are not re-embedded."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT heading, content_hash FROM knowledge_chunks WHERE source = %s",
            (source,),
        )
        rows = await cur.fetchall()
    return {row[0]: row[1] for row in rows}


async def count(conn) -> int:
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM knowledge_chunks")
        row = await cur.fetchone()
    return row[0] if row else 0
