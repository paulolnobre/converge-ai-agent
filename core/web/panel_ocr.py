"""Manual OCR: the same ownership/expiry boundary as opening a kept file."""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from core.config import settings
from core.crypto import SealedDataError
from core.repositories import media as media_repo
from core.services import media_store, ocr, ocr_remote
from core.services.media_types import sniff

_slot = asyncio.Lock()


@asynccontextmanager
async def slot():
    if _slot.locked():
        raise ocr.OcrError(429, "Outra leitura está em andamento. Tente em instantes.")
    async with _slot:
        yield


async def read(conn, contact_id: int, media_id: int) -> bytes:
    found = await media_repo.servable(conn, contact_id, media_id)
    if found is None:
        raise ocr.OcrError(404, "Arquivo indisponível.")
    if found.kind not in ("image", "document", "sticker"):
        raise ocr.OcrError(415, "A leitura aceita imagens e PDFs.")
    try:
        data = media_store.read(found.storage_key)
    except FileNotFoundError:
        await media_repo.mark_missing(conn, media_id)
        raise ocr.OcrError(404, "Arquivo indisponível.") from None
    except SealedDataError:
        raise ocr.OcrError(404, "Arquivo indisponível.") from None
    if not (sniff(data, "image")[1] or (found.kind == "document" and data.startswith(b"%PDF-"))):
        raise ocr.OcrError(415, "A leitura aceita imagens JPEG, PNG, WebP, GIF e PDFs.")
    return data


def enabled() -> bool:
    """On, and never inside the bot in production: there it needs the container."""
    return settings.inbox_ocr_enabled and (bool(settings.panel_ocr_socket)
                                           or settings.app_env != "production")


async def extract(data: bytes) -> dict:
    """In the OCR container when there is one; else in a child process here."""
    if settings.panel_ocr_socket:
        return await ocr_remote.extract(settings.panel_ocr_socket, data)
    return await ocr.extract(data)


async def unless_gone(request, work) -> dict:
    """The reading, cancelled when the browser hangs up (the dialog closed): the
    container then sees the socket close and stops too."""
    task = asyncio.ensure_future(work)

    async def hung_up() -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass

    gone = asyncio.ensure_future(hung_up())
    try:
        await asyncio.wait({task, gone}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        gone.cancel()
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
    if task.cancelled():
        raise ocr.OcrError(499, "Leitura cancelada.")
    return task.result()
