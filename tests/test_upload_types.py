"""Real local image/audio conversion with synthetic files and no provider."""

import io
import wave

import pytest
from PIL import Image

from core.services.upload_types import InvalidUpload, normalize


async def test_image_is_decoded_reencoded_and_metadata_removed():
    stream = io.BytesIO()
    image = Image.new("RGB", (8, 8))
    exif = Image.Exif()
    exif[270] = "private description"
    image.save(stream, format="JPEG", exif=exif)
    result = await normalize(stream.getvalue(), "image", "../../name.jpg")
    assert result.mime == "image/jpeg" and b"private description" not in result.data
    assert result.filename == "foto.jpg"


async def test_audio_is_real_mono_opus():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\0" * 3200)
    result = await normalize(stream.getvalue(), "audio", "clip.wav")
    assert result.data.startswith(b"OggS") and b"OpusHead" in result.data
    assert result.mime == "audio/ogg"


async def test_html_never_becomes_an_inline_image():
    with pytest.raises(InvalidUpload):
        await normalize(b"<svg onload=alert(1)>", "image", "foto.png")
    result = await normalize(b"<html>text</html>", "document", "pagina.html")
    assert result.mime == "application/octet-stream"


async def test_oversized_duration_is_refused_instead_of_silently_truncated():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\0" * (181 * 32000))
    with pytest.raises(InvalidUpload, match="3 minutos"):
        await normalize(stream.getvalue(), "audio", "long.wav")
