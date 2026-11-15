"""The attachment of a message, its type read from its bytes, and the name it
downloads under. All pure."""
import pytest

from core.config import settings
from core.services import media_types
from core.services.media_types import Attachment, attachment_of, disposition, sniff

MB = 1024 * 1024


def _tg(**fields) -> dict:
    return {"message_id": 7, "chat": {"id": 42, "type": "private"}, "date": 1759780000, **fields}


def test_telegram_photo_takes_the_largest_size_within_the_cap(monkeypatch):
    monkeypatch.setattr(settings, "max_media_bytes", 20 * MB)
    sizes = [{"file_id": "s", "file_unique_id": "U1", "file_size": 10_000},
             {"file_id": "m", "file_unique_id": "U2", "file_size": 2 * MB},
             {"file_id": "x", "file_unique_id": "U3", "file_size": 25 * MB}]
    found = attachment_of(_tg(photo=sizes, caption="olha"))
    assert found == Attachment(kind="image", file_id="m", source_id="tg:42:7:U2",
                               mime_type=None, filename=None, declared_bytes=2 * MB)


def test_telegram_photo_all_over_the_cap_declares_the_largest(monkeypatch):
    monkeypatch.setattr(settings, "max_media_bytes", 1 * MB)
    found = attachment_of(_tg(photo=[{"file_id": "a", "file_unique_id": "A", "file_size": 2 * MB},
                                     {"file_id": "b", "file_unique_id": "B", "file_size": 3 * MB}]))
    assert found is not None and found.file_id == "b" and found.declared_bytes == 3 * MB


@pytest.mark.parametrize("field, kind", [
    ("document", "document"), ("video", "video"), ("video_note", "video"),
    ("voice", "audio"), ("audio", "audio"), ("sticker", "sticker"),
])
def test_telegram_fields_and_ids_include_the_file_unique_id(field, kind):
    body = {"file_id": "F", "file_unique_id": "AQAD", "file_size": 100,
            "mime_type": "application/pdf", "file_name": "exame.pdf"}
    found = attachment_of(_tg(**{field: body}))
    assert found is not None and found.kind == kind and found.source_id == "tg:42:7:AQAD"
    assert found.declared_bytes == 100


def test_a_whatsapp_message_reads_only_its_media_field():
    voice_only = {"chat": {"id": "wa:5591", "type": "private"}, "message_id": "W",
                  "voice": {"file_id": "wa-media:M1", "mime_type": "audio/ogg"}}
    assert attachment_of(voice_only) is None  # converted with capture off
    media = {"kind": "audio", "file_id": "wa-media:M1", "mime_type": "audio/ogg",
             "filename": None, "declared_bytes": None, "source_id": "wa:W:M1"}
    assert attachment_of({**voice_only, "_media": media}) == Attachment(**media)


def test_a_text_message_has_no_attachment():
    assert attachment_of(_tg(text="oi")) is None


@pytest.mark.parametrize("head, kind, expected", [
    (b"\xff\xd8\xff\xe0", "image", ("image/jpeg", True)),
    (b"\x89PNG\r\n\x1a\n", "image", ("image/png", True)),
    (b"GIF89a", "sticker", ("image/gif", True)),
    (b"RIFF\x00\x00\x00\x00WEBP", "sticker", ("image/webp", True)),
    (b"OggS\x00", "audio", ("audio/ogg", True)),
    (b"\xff\xf1\x50", "audio", ("audio/aac", True)),
    (b"\xff\xf9\x50", "audio", ("audio/aac", True)),
    (b"ID3\x04", "audio", ("audio/mpeg", True)),
    (b"\xff\xfb\x90", "audio", ("audio/mpeg", True)),
    (b"\x00\x00\x00\x18ftypmp42", "video", ("video/mp4", True)),
    (b"\x00\x00\x00\x18ftypisom", "video", ("video/mp4", True)),
    (b"\x00\x00\x00\x18ftypM4A ", "audio", ("audio/mp4", True)),
    (b"\x00\x00\x00\x14ftypqt  ", "video", ("application/octet-stream", False)),
    (b"\x00\x00\x00\x143gp4", "video", ("application/octet-stream", False)),
    (b"#!AMR\n", "audio", ("application/octet-stream", False)),
    (b"%PDF-1.7", "document", ("application/octet-stream", False)),
    (b"<html><script>", "image", ("application/octet-stream", False)),
    (b"<svg xmlns=", "image", ("application/octet-stream", False)),
    (b"\x89PNG\r\n\x1a\n", "document", ("application/octet-stream", False)),
    (b"OggS\x00", "image", ("application/octet-stream", False)),
    (b"", "image", ("application/octet-stream", False)),
])
def test_type_comes_from_the_bytes_and_must_match_the_kind(head, kind, expected):
    assert sniff(head + b"\x00" * 16, kind) == expected


def _ascii(value: str) -> str:
    return value.split('filename="', 1)[1].split('"', 1)[0]


@pytest.mark.parametrize("name, ascii_part", [
    ("../../etc/passwd", "....etcpasswd"), ('a"b\r\nc.pdf', "abc.pdf"),
    ("   ", "arquivo"), (None, "arquivo"), ("x" * 300, "x" * 120), ("\x00\x1f", "arquivo"),
])
def test_the_download_name_is_clean(name, ascii_part):
    value = disposition(name, inline=False)
    assert value.startswith("attachment;")
    assert _ascii(value) == ascii_part
    assert "\r" not in value and "\n" not in value


def test_a_non_ascii_name_uses_rfc5987():
    value = disposition("relatório 📄.pdf", inline=False)
    assert "filename*=UTF-8''relat%C3%B3rio%20%F0%9F%93%84.pdf" in value
    assert _ascii(value) == "relatorio _.pdf"


def test_inline_says_inline():
    assert disposition("foto.jpg", inline=True).startswith("inline;")


def test_a_pdf_without_a_name_downloads_as_arquivo():
    """Review Focus 5: a WhatsApp document with no filename."""
    mime, inline = sniff(b"%PDF-1.4" + b"\x00" * 16, "document")
    assert (mime, inline) == ("application/octet-stream", False)
    assert _ascii(disposition(None, inline=inline)) == "arquivo"


def test_kinds_are_the_five():
    assert set(media_types.KINDS) == {"image", "audio", "video", "document", "sticker"}


def test_a_telegram_file_without_a_unique_id_falls_back_to_its_file_id():
    found = attachment_of(_tg(photo=[{"file_id": "F9", "file_size": 10}]))
    assert found is not None and found.source_id == "tg:42:7:F9"
