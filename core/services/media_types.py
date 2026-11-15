"""What a message carries, read without I/O: the attachment and where it came
from, its type read from its first bytes (never from what the sender
declared), and the name it downloads under.

A file only opens inside the panel when its bytes are one of a fixed list of
images, audio and video *and* match the kind the channel said it was; any
other file — a PDF, an SVG, HTML dressed up as a photo — is a download.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from core.config import settings

Kind = Literal["image", "audio", "video", "document", "sticker"]
KINDS: tuple[Kind, ...] = ("image", "audio", "video", "document", "sticker")

_WA_PREFIX = "wa:"
_TELEGRAM_FIELDS: tuple[tuple[str, Kind], ...] = (
    ("document", "document"), ("video", "video"), ("video_note", "video"),
    ("voice", "audio"), ("audio", "audio"), ("sticker", "sticker"),
)
_DOWNLOAD = "application/octet-stream"
_MP4_BRANDS = {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4A ", b"dash"}
_NAME_MAX = 120


@dataclass(frozen=True)
class Attachment:
    kind: Kind
    file_id: str            # "wa-media:<id>" or a Telegram file_id
    source_id: str          # "wa:<wamid>:<media id>" / "tg:<chat>:<message>:<file_unique_id>"
    mime_type: str | None   # as declared; never trusted for display
    filename: str | None
    declared_bytes: int | None


def attachment_of(message: dict) -> Attachment | None:
    """The one attachment of a message, if any. A WhatsApp message only has
    one when it was converted with capture on (its `_media`)."""
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if chat_id.startswith(_WA_PREFIX):
        media = message.get("_media")
        return Attachment(**media) if isinstance(media, dict) else None
    where = f"tg:{chat_id}:{message.get('message_id')}"
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        sizes = sorted(photo, key=lambda p: p.get("file_size") or 0)
        within = [p for p in sizes if (p.get("file_size") or 0) <= settings.max_media_bytes]
        chosen = (within or sizes)[-1]
        unique = chosen.get("file_unique_id") or chosen["file_id"]
        return Attachment("image", chosen["file_id"], f"{where}:{unique}",
                          None, None, chosen.get("file_size"))
    for field, kind in _TELEGRAM_FIELDS:
        body = message.get(field)
        if isinstance(body, dict) and body.get("file_id"):
            unique = body.get("file_unique_id") or body["file_id"]
            return Attachment(kind, body["file_id"], f"{where}:{unique}",
                              body.get("mime_type"), body.get("file_name"),
                              body.get("file_size"))
    return None


def _signature(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if len(data) >= 2 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0:
        return "audio/aac"  # ADTS: FF F1 / FF F9
    if data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if data[4:8] == b"ftyp" and data[8:12] in _MP4_BRANDS:
        return "mp4"
    return None


def sniff(data: bytes, kind: Kind) -> tuple[str, bool]:
    """(mime, inline): inline only when the bytes are a known type that
    matches the kind; everything else is application/octet-stream."""
    found = _signature(data)
    if found == "mp4":
        found = {"video": "video/mp4", "audio": "audio/mp4"}.get(kind)
    if found is None:
        return _DOWNLOAD, False
    family = found.split("/")[0]
    allowed = {"image": "image", "sticker": "image", "audio": "audio", "video": "video"}
    if allowed.get(kind) != family:
        return _DOWNLOAD, False
    return found, True


def _clean(filename: str | None) -> str:
    name = "".join(ch for ch in (filename or "")
                   if unicodedata.category(ch)[0] != "C" and ch not in '"/\\')
    return name.strip()[:_NAME_MAX].strip() or "arquivo"


def disposition(filename: str | None, *, inline: bool) -> str:
    """The Content-Disposition value: an ASCII fallback name and the real one
    in RFC 5987 form. Nothing in it can break the header or name a path."""
    name = _clean(filename)
    folded = unicodedata.normalize("NFKD", name)
    plain = "".join(ch for ch in folded if not unicodedata.combining(ch))
    plain = re.sub(r"[^\x20-\x7e]", "_", plain).strip() or "arquivo"
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{plain}\"; filename*=UTF-8''{quote(name, safe='')}"
