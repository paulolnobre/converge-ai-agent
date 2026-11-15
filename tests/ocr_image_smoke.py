"""Run inside the built app image, without network, using synthetic documents."""
import asyncio
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from core.services import ocr


def samples():
    image = Image.new("RGB", (1600, 500), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 56)
    draw.text((60, 70), "CONVERGE TESTE\nPROTOCOLO 123456\nRECEPCAO", font=font, fill="black", spacing=30)
    png = io.BytesIO()
    image.save(png, format="PNG")
    native = io.BytesIO()
    pdf = canvas.Canvas(native)
    for page in range(5):
        pdf.drawString(60, 700, f"PROTOCOLO 123456 PAGINA {page + 1}")
        pdf.showPage()
    pdf.save()
    scanned = io.BytesIO()
    pdf = canvas.Canvas(scanned, pagesize=(800, 250))
    pdf.drawImage(ImageReader(io.BytesIO(png.getvalue())), 0, 0, width=800, height=250)
    pdf.save()
    return png.getvalue(), native.getvalue(), scanned.getvalue()


async def main():
    before = set(Path("/dev/shm").glob("converge-ocr-*"))
    png, native, scanned = samples()
    for kind, data in [("image", png), ("PDF text", native), ("PDF scan", scanned)]:
        result = await ocr.extract(data)
        assert "123456" in result["text"], kind
        assert result["pages"] == (3 if kind == "PDF text" else 1)
        assert result["limited"] == (kind == "PDF text")
        print(f"PASS: {kind}")
    writer = PdfWriter()
    writer.add_page(PdfReader(io.BytesIO(native)).pages[0])
    writer.encrypt("synthetic-password")
    protected = io.BytesIO()
    writer.write(protected)
    for data in [protected.getvalue(), b"invalid image"]:
        try:
            await ocr.extract(data)
        except ocr.OcrError as exc:
            assert exc.status == 422
        else:
            raise AssertionError("Invalid/protected document accepted")
    assert set(Path("/dev/shm").glob("converge-ocr-*")) == before
    print("PASS: protected/invalid files refused; no RAM files left")


if __name__ == "__main__":
    asyncio.run(main())
