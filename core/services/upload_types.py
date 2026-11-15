"""Bounded local normalization. No plaintext upload is written to disk."""

from __future__ import annotations

import asyncio
import io
import os
import unicodedata
from dataclasses import dataclass
from pathlib import PurePath

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_BYTES = 20 * 1024 * 1024
AUDIO_SECONDS = 180
_DOCS = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class InvalidUpload(ValueError):
    pass


@dataclass(frozen=True)
class Upload:
    data: bytes
    kind: str
    mime: str
    filename: str


def clean_name(name: str) -> str:
    return (
        "".join(c for c in name if unicodedata.category(c)[0] != "C" and c not in '/\\"').strip()[
            :120
        ]
        or "arquivo"
    )


def _image(data: bytes) -> Upload:
    try:
        with Image.open(io.BytesIO(data)) as original:
            if (
                original.format not in ("JPEG", "PNG", "WEBP")
                or original.width * original.height > 20_000_000
            ):
                raise InvalidUpload("Use uma foto JPEG, PNG ou WebP de até 20 megapixels.")
            image = ImageOps.exif_transpose(original)
            stream = io.BytesIO()
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                image.convert("RGBA").save(stream, format="PNG")
                mime, name = "image/png", "foto.png"
            else:
                image.convert("RGB").save(stream, format="JPEG", quality=88)
                mime, name = "image/jpeg", "foto.jpg"
            value = stream.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise InvalidUpload("Não consegui abrir esta foto. Escolha JPEG, PNG ou WebP.") from None
    if len(value) > 5 * 1024 * 1024:
        raise InvalidUpload(
            "A foto precisa ficar abaixo de 5 MB. Reduza o tamanho e tente de novo."
        )
    return Upload(value, "image", mime, name)


async def _ffmpeg(args: list[str], data: bytes) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-filter_complex_threads",
            "1",
            "-max_alloc",
            "67108864",
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        )
    except FileNotFoundError:
        raise InvalidUpload("O envio de áudio ainda não está disponível neste servidor.") from None
    try:
        async with asyncio.timeout(25):
            out, _ = await process.communicate(data)
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
        raise
    if process.returncode or not out:
        raise InvalidUpload("Não consegui abrir o áudio. Grave novamente ou escolha outro arquivo.")
    return out


async def normalize(data: bytes, kind: str, filename: str) -> Upload:
    if not data:
        raise InvalidUpload("O arquivo está vazio.")
    if len(data) > MAX_BYTES:
        raise InvalidUpload("O arquivo pode ter até 20 MB.")
    if kind == "image":
        return await asyncio.to_thread(_image, data)
    if kind == "audio":
        # Decode at most one second beyond the limit, then reject rather than silently truncate.
        pcm = await _ffmpeg(
            [
                "-protocol_whitelist",
                "pipe",
                "-probesize",
                "1048576",
                "-analyzeduration",
                "5000000",
                "-i",
                "pipe:0",
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-t",
                str(AUDIO_SECONDS + 1),
                "-threads",
                "1",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "s16le",
                "pipe:1",
            ],
            data,
        )
        if len(pcm) > AUDIO_SECONDS * 32000:
            raise InvalidUpload("O áudio pode ter até 3 minutos.")
        encoded = await _ffmpeg(
            [
                "-f",
                "s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-map_metadata",
                "-1",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                "-threads",
                "1",
                "-f",
                "ogg",
                "pipe:1",
            ],
            pcm,
        )
        return Upload(encoded, "audio", "audio/ogg", "audio.ogg")
    if kind != "document":
        raise InvalidUpload("Tipo de anexo inválido.")
    name = clean_name(filename)
    return Upload(
        data, "document", _DOCS.get(PurePath(name).suffix.lower(), "application/octet-stream"), name
    )
