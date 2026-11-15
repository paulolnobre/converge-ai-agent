"""The bot's side of the OCR container (core/services/ocr_server.py).

The container may have been taken over by the file it read, so nothing in its
answer is trusted beyond a string of text, a page count and a flag, all cut
to the same bounds as a local read. One deadline covers connecting, sending
and the answer; past it, or when the request is cancelled, the connection is
dropped and the container stops reading.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat

from core.services.ocr import OcrError
from core.services.ocr_limits import MAX_INPUT, MAX_PAGES, MAX_TEXT, WALL_SECONDS

MAX_REPLY = 256 * 1024
DEADLINE = WALL_SECONDS + 10
_UNAVAILABLE = "A leitura de texto está indisponível neste servidor."
_UNREADABLE = "Não foi possível ler este arquivo."
_TOO_SLOW = "A leitura demorou demais. Tente uma imagem menor."
_REFUSALS = {413, 415, 422, 429, 503}


async def extract(path: str, data: bytes) -> dict:
    if not data or len(data) > MAX_INPUT:
        raise OcrError(413, "Use uma imagem ou PDF de até 10 MB.")
    try:
        # A socket, not whatever else the container may have left in its place.
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise OcrError(503, _UNAVAILABLE)
        async with asyncio.timeout(DEADLINE):
            reader, writer = await asyncio.open_unix_connection(path)
            try:
                writer.write(len(data).to_bytes(8, "big") + data)
                await writer.drain()
                raw = await _read_all(reader)
            finally:
                writer.transport.abort()
    except TimeoutError:
        raise OcrError(422, _TOO_SLOW) from None
    except OSError:
        raise OcrError(503, _UNAVAILABLE) from None
    return _checked(raw)


async def _read_all(reader: asyncio.StreamReader) -> bytes:
    raw = b""
    while chunk := await reader.read(65536):
        raw += chunk
        if len(raw) > MAX_REPLY:
            raise OcrError(422, _UNREADABLE)
    return raw


def _valid_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")  # a lone surrogate would break the HTTP answer
    except UnicodeEncodeError:
        return False
    return True


def _checked(raw: bytes) -> dict:
    try:
        answer = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise OcrError(422, _UNREADABLE) from None
    if not isinstance(answer, dict):
        raise OcrError(422, _UNREADABLE)
    if "status" in answer:
        status, detail = answer.get("status"), answer.get("detail")
        if type(status) is not int or status not in _REFUSALS or not _valid_text(detail):
            raise OcrError(422, _UNREADABLE)
        assert isinstance(detail, str)
        raise OcrError(status, detail[:200])
    text, pages, limited = answer.get("text"), answer.get("pages"), answer.get("limited")
    if not (_valid_text(text) and type(pages) is int and isinstance(limited, bool)):
        raise OcrError(422, _UNREADABLE)
    assert isinstance(text, str)
    return {"text": text[:MAX_TEXT], "pages": max(0, min(pages, MAX_PAGES)), "limited": limited}
