"""Local OCR fallback for scans; documents never leave this host."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache

import pypdfium2
import pytesseract
from PIL import Image, ImageOps

from supplier_chaser.config import Settings

# Scans often carry a stamp or page number in a text layer; that is not reviewable text.
MIN_EMBEDDED_CHARS = 20
RENDER_DPI = 300
# A4 at 600 dpi; bounds decoder memory and tesseract runtime for oversized scans.
MAX_PIXELS = 40_000_000


@cache
def engine(languages: str) -> tuple[str, str | None]:
    """Return the engine label and why OCR is unavailable, if it is."""
    try:
        version = pytesseract.get_tesseract_version()
        installed = set(pytesseract.get_languages())
    except (pytesseract.TesseractNotFoundError, pytesseract.TesseractError, OSError):
        return "tesseract", "tesseract binary not found"
    missing = [lang for lang in languages.split("+") if lang not in installed]
    label = f"tesseract {version}"
    return label, f"missing language data: {', '.join(missing)}" if missing else None


@dataclass
class OCR:
    """Per-document OCR budget so one large scan cannot stall the worker."""

    languages: str
    max_pages: int
    timeout_seconds: float
    pages: int = 0
    deadline: float = field(init=False)

    def __post_init__(self) -> None:
        self.deadline = time.monotonic() + self.timeout_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> "OCR | None":
        if not settings.ocr_enabled:
            return None
        return cls(settings.ocr_languages, settings.ocr_max_pages, settings.ocr_timeout_seconds)

    def text(self, embedded: str, render: Callable[[], Image.Image]) -> dict:
        """Keep embedded text when present; otherwise recognize the rendered page."""
        if len(embedded.strip()) >= MIN_EMBEDDED_CHARS:
            return {"text": embedded}
        label, unavailable = engine(self.languages)
        provenance = {"method": "ocr", "engine": label, "languages": self.languages}
        self.pages += 1
        remaining = self.deadline - time.monotonic()
        if unavailable:
            return {"text": embedded, "ocr": provenance | {"status": unavailable}}
        if self.pages > self.max_pages:
            return {"text": embedded, "ocr": provenance | {"status": "page limit reached"}}
        if remaining <= 0:
            return {"text": embedded, "ocr": provenance | {"status": "time budget exhausted"}}
        image = render()
        # Checked before decoding: Image.open only reads the header.
        if image.width * image.height > MAX_PIXELS:
            return {"text": embedded, "ocr": provenance | {"status": "image too large"}}
        # Phone photos store rotation in EXIF; tesseract reads raw pixel order.
        upright = ImageOps.exif_transpose(image).convert("RGB")
        try:
            recognized = pytesseract.image_to_string(
                upright, lang=self.languages, timeout=remaining
            )
        except RuntimeError as exc:
            # pytesseract signals a killed process with a bare RuntimeError.
            status = "time budget exhausted" if "timeout" in str(exc) else "engine failed"
            return {"text": embedded, "ocr": provenance | {"status": status}}
        return {"text": recognized.strip(), "ocr": provenance | {"status": "ok"}}


def render_pdf_page(data: bytes, index: int) -> Image.Image:
    document = pypdfium2.PdfDocument(data)
    try:
        page = document[index]
        width, height = page.get_size()
        scale = RENDER_DPI / 72
        if width * height * scale**2 > MAX_PIXELS:
            scale = (MAX_PIXELS / (width * height)) ** 0.5
        # Copy so the image does not borrow the bitmap buffer of the closed document.
        return page.render(scale=scale).to_pil().copy()
    finally:
        document.close()
