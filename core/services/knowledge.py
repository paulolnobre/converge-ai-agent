"""What the desk knows, and the discipline of not knowing anything else.

This is the module that makes phase 1 safe. Without it the answering model is
free to invent a price, an address or a policy, and it will — fluently, in the
organization's voice, with the organization's credentials behind it.

The shape is deliberate. Retrieval is generous: a low floor and several chunks,
because a near-miss chunk costs tokens while a missing one costs the answer.
The refusal is not done by the similarity score but by the model, which is given
the retrieved text and told in as many words that anything not in it does not
exist. A score cannot tell "qual o preço da consulta" from "qual o preço da
biópsia"; reading the chunk can.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re

from core.config import settings
from core.integrations import workspace
from core.integrations.llm import embed
from core.repositories import knowledge as knowledge_repo

logger = logging.getLogger(__name__)

# A heading and the prose under it, which for an FAQ is a question and its
# answer. Splitting anywhere else — a fixed window, a sentence count — cuts the
# price away from the service it belongs to.
_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def split(document: str) -> list[dict]:
    """The document as retrievable chunks: one per `##` heading.

    The heading is kept with the body and embedded with it, because "Qual o
    prazo para laudos?" is most of the signal for matching a question to it.
    """
    matches = list(_HEADING.finditer(document))
    chunks: list[dict] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(document)
        body = document[match.end():end].strip()
        if not body:
            continue
        heading = match.group(1).strip()
        chunks.append({
            "heading": heading,
            "body": body,
            "content_hash": hashlib.sha256(
                f"{heading}\n{body}".encode()
            ).hexdigest(),
        })
    return chunks


async def load_document() -> tuple[str, str]:
    """The FAQ text and the source it came from.

    The drive wins over the local file when both are configured: that is the
    copy the organization actually edits, and a pilot that answers from a stale
    checkout is a pilot that answers wrong.
    """
    if settings.knowledge_document_id:
        data = await workspace.files().download(settings.knowledge_document_id)
        return data.decode("utf-8"), settings.knowledge_document_id

    from core.content import _root

    path = _root() / settings.knowledge_document_path
    if not path.exists():
        raise FileNotFoundError(f"No knowledge document at {path}.")
    return path.read_text(encoding="utf-8"), settings.knowledge_document_path


async def ingest(conn, document: str, source: str) -> int:
    """Chunk, embed and store a document. Returns how many chunks were stored.

    Every chunk is re-embedded on every ingest. That is affordable for an FAQ
    and it is the honest default: an incremental path that skips unchanged
    hashes has to be right about what "unchanged" means, and getting it wrong
    leaves the desk answering from a paragraph the organization deleted.
    """
    chunks = split(document)
    if not chunks:
        raise ValueError(
            f"{source} produced no chunks. The document needs '## question' "
            "headings with the answer below each one."
        )

    vectors = await embed([f"{chunk['heading']}\n{chunk['body']}" for chunk in chunks])
    if vectors is None:
        raise RuntimeError(
            "Could not embed the knowledge document; leaving the previous "
            "version in place."
        )
    for chunk, vector in zip(chunks, vectors, strict=True):
        chunk["embedding"] = vector

    stored = await knowledge_repo.replace(conn, source, chunks)
    logger.info("Stored %d knowledge chunks from %s.", stored, source)
    return stored


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


async def retrieve(conn, question: str, *, top_k: int | None = None
                   ) -> list[dict]:
    """The chunks most likely to contain the answer, best first.

    A plain cosine over every chunk. An FAQ is tens of rows, not millions, and
    an index that has to be kept in step with the document is a way for the desk
    to answer from something the organization already deleted.
    """
    chunks = await knowledge_repo.all_chunks(conn)
    if not chunks:
        logger.warning("The knowledge base is empty; the desk can answer nothing.")
        return []

    vectors = await embed([question])
    if not vectors:
        logger.error("Could not embed the question; refusing to answer from memory.")
        return []

    scored = [
        (_cosine(vectors[0], chunk["embedding"]), chunk) for chunk in chunks
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {**chunk, "score": score}
        for score, chunk in scored[: top_k or settings.knowledge_top_k]
        if score >= settings.knowledge_min_similarity
    ]


def as_context(chunks: list[dict]) -> str:
    """The retrieved chunks, as the answering prompt reads them."""
    return "\n\n".join(
        f"[{index}] {chunk['heading']}\n{chunk['body']}"
        for index, chunk in enumerate(chunks, start=1)
    )
