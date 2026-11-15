"""Real codecs in the deployed image, without network, credentials or disk writes."""

import asyncio
import io
import subprocess
import wave

from PIL import Image

from core.services.upload_types import InvalidUpload, normalize


async def main():
    raw = io.BytesIO()
    with wave.open(raw, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\0" * 32000)
    wav = raw.getvalue()
    cases = {"wav": wav}
    for name, args in [
        ("webm", ["-c:a", "libopus", "-f", "webm"]),
        ("m4a", ["-c:a", "aac", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4"]),
    ]:
        cases[name] = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-i",
                "pipe:0",
                *args,
                "pipe:1",
            ],
            input=wav,
            stdout=subprocess.PIPE,
            check=True,
            timeout=15,
        ).stdout
    for name, data in cases.items():
        result = await normalize(data, "audio", "test." + name)
        assert result.data.startswith(b"OggS") and b"OpusHead" in result.data
        print("PASS actual " + name + " to mono Ogg/Opus")
    image = io.BytesIO()
    Image.new("RGB", (8, 8)).save(image, format="PNG")
    result = await normalize(image.getvalue(), "image", "photo.png")
    assert result.mime == "image/jpeg"
    try:
        await normalize(b"<svg onload=alert(1)>", "image", "photo.png")
    except InvalidUpload:
        pass
    else:
        raise AssertionError("HTML image was accepted")
    print("PASS verified image and rejected disguised HTML")


asyncio.run(main())
