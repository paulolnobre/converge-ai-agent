"""The OCR container: one file per connection on a Unix socket, a JSON answer back.

It runs the same image as the bot with no network at all, no secrets, no
credentials and no customer files (deploy/docker-compose.prod.yml). The bot
reaches it through a socket file on a shared volume; the container cannot open
a connection to anything. A file crafted against the image parsers finds
nothing to take and nowhere to send it. It imports only the OCR itself, never
core.config.

Protocol: 8 bytes big-endian size, then the file. The answer is
{"text", "pages", "limited"} or {"status", "detail"}, and the connection
closes. If the bot hangs up first, the reading stops and its files go.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os

from core.services import ocr
from core.services.ocr_limits import MAX_INPUT

SOCKET = "/run/ocr/ocr.sock"
READ_SECONDS = 30
_busy = asyncio.Lock()


async def _extract_unless_gone(reader: asyncio.StreamReader, data: bytes) -> dict | None:
    """The OCR, cancelled (children killed, RAM files removed) as soon as the
    bot hangs up. None when it did."""
    work = asyncio.ensure_future(ocr.extract(data))
    gone = asyncio.ensure_future(reader.read(1))
    try:
        await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        gone.cancel()
        if not work.done():
            work.cancel()
            with contextlib.suppress(BaseException):
                await work
    if work.cancelled():
        return None
    try:
        return work.result()
    except ocr.OcrError as exc:
        return {"status": exc.status, "detail": exc.detail}


async def _answer(reader: asyncio.StreamReader) -> dict | None:
    try:
        size = int.from_bytes(await asyncio.wait_for(reader.readexactly(8), READ_SECONDS), "big")
    except (asyncio.IncompleteReadError, TimeoutError, ConnectionError):
        return None
    if not 0 < size <= MAX_INPUT:
        return {"status": 413, "detail": "Use uma imagem ou PDF de até 10 MB."}
    if _busy.locked():
        return {"status": 429, "detail": "Outra leitura está em andamento. Tente em instantes."}
    async with _busy:
        try:
            data = await asyncio.wait_for(reader.readexactly(size), READ_SECONDS)
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError):
            return None
        return await _extract_unless_gone(reader, data)


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    sent = False
    try:
        answer = await _answer(reader)
        if answer is not None:
            writer.write(json.dumps(answer).encode())
            await asyncio.wait_for(writer.drain(), READ_SECONDS)
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), READ_SECONDS)
            sent = True
    except (ConnectionError, TimeoutError):
        pass
    finally:
        if not sent:
            writer.transport.abort()


async def serve(path: str = SOCKET) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)  # left by the previous run
    server = await asyncio.start_unix_server(handle, path)
    os.chmod(path, 0o600)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(serve())
