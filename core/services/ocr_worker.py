"""Local text extraction. Invoked as a bounded, separate process by ocr.py.

The original and rendered pages exist only in RAM-backed /dev/shm, in a
private directory removed on exit. No provider, credentials, database or logs.
"""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import warnings
from pathlib import Path

from core.services.ocr_limits import MAX_EDGE, MAX_INPUT, MAX_PAGES, MAX_PIXELS, MAX_TEXT


class InvalidDocument(Exception):
    pass


def image_text(data: bytes, folder: Path) -> str:
    from PIL import Image, ImageOps

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data), formats=["JPEG", "PNG", "WEBP", "GIF"]) as image:
            if image.width * image.height > MAX_PIXELS:
                raise InvalidDocument()
            image = ImageOps.exif_transpose(image)
            image.thumbnail((MAX_EDGE, MAX_EDGE))
            image.convert("RGB").save(folder / "page.png")
    completed = subprocess.run(
        ["tesseract", str(folder / "page.png"), "stdout", "-l", "por+eng", "--dpi", "300"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=True,
    )
    return completed.stdout.decode("utf-8", errors="replace").strip()


def extract(data: bytes, folder: Path) -> dict:
    if not data or len(data) > MAX_INPUT:
        raise InvalidDocument()
    limited = False
    if data.startswith(b"%PDF-"):
        from pypdf import PdfReader

        pdf = PdfReader(io.BytesIO(data))
        if pdf.is_encrypted:
            raise InvalidDocument()
        count = min(len(pdf.pages), MAX_PAGES)
        if not count:
            raise InvalidDocument()
        limited = len(pdf.pages) > MAX_PAGES
        (folder / "source.pdf").write_bytes(data)
        texts = []
        for index in range(count):
            text = (pdf.pages[index].extract_text() or "").strip()
            if not text:
                subprocess.run(
                    ["pdftoppm", "-f", str(index + 1), "-l", str(index + 1), "-singlefile",
                     "-scale-to", str(MAX_EDGE), "-png", str(folder / "source.pdf"),
                     str(folder / "rendered")],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True,
                )
                text = image_text((folder / "rendered.png").read_bytes(), folder)
            texts.append(text)
        text = "\n\n".join(texts)
    else:
        text, count = image_text(data, folder), 1
    return {"text": text[:MAX_TEXT], "pages": count,
            "limited": limited or len(text) > MAX_TEXT}


def main() -> None:
    import resource

    # The child and its native tools inherit these limits. A parser cannot
    # consume the web worker's memory/CPU or leave a huge rendered file.
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (35, 35))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024**2, 32 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    try:
        data = sys.stdin.buffer.read(MAX_INPUT + 1)
        result = extract(data, Path(sys.argv[1]))
    except FileNotFoundError:
        result = {"error": "unavailable"}
    except subprocess.TimeoutExpired:
        result = {"error": "timeout"}
    except Exception:
        result = {"error": "invalid"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
