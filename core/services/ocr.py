"""Run local OCR without blocking the server or exposing its environment."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile

from core.services.ocr_limits import MAX_INPUT, MAX_TEXT, WALL_SECONDS


class OcrError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(detail)


async def extract(data: bytes) -> dict:
    if not data or len(data) > MAX_INPUT:
        raise OcrError(413, "Use uma imagem ou PDF de até 10 MB.")
    try:
        with tempfile.TemporaryDirectory(prefix="converge-ocr-", dir="/dev/shm") as folder:
            return await _run(data, folder)
    except OSError:
        raise OcrError(503, "A leitura de texto está indisponível neste servidor.") from None


async def _run(data: bytes, folder: str) -> dict:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "core.services.ocr_worker", folder,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        env={"PATH": os.defpath, "LANG": "C.UTF-8", "OMP_THREAD_LIMIT": "1"},
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(data), WALL_SECONDS)
    except BaseException as exc:
        # Kill the whole group, including a native parser still running when
        # the request times out or is cancelled. No plaintext directory survives.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        if isinstance(exc, TimeoutError):
            raise OcrError(422, "A leitura demorou demais. Tente uma imagem menor.") from None
        raise
    try:
        result = json.loads(output)
    except (ValueError, UnicodeDecodeError):
        raise OcrError(422, "Não foi possível ler este arquivo.") from None
    if not isinstance(result, dict):
        raise OcrError(422, "Não foi possível ler este arquivo.")
    error = result.get("error")
    if error == "unavailable":
        raise OcrError(503, "A leitura de texto está indisponível neste servidor.")
    if error == "timeout":
        raise OcrError(422, "A leitura demorou demais. Tente uma imagem menor.")
    if process.returncode or error or not isinstance(result.get("text"), str):
        raise OcrError(422, "Não foi possível ler este arquivo. Use uma imagem ou PDF legível.")
    return {"text": result["text"][:MAX_TEXT], "pages": result["pages"],
            "limited": bool(result.get("limited"))}
