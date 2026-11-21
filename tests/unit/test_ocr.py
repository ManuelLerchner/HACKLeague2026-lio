import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from supplier_chaser.ingestion import ocr as ocr_module
from supplier_chaser.ingestion.ocr import OCR
from supplier_chaser.ingestion.parse import parse

_, UNAVAILABLE = ocr_module.engine("eng")
needs_tesseract = pytest.mark.skipif(
    UNAVAILABLE is not None, reason=f"local OCR unavailable: {UNAVAILABLE}"
)


def scan(text: str) -> Image.Image:
    image = Image.new("RGB", (1400, 260), "white")
    ImageDraw.Draw(image).text((40, 80), text, fill="black", font=ImageFont.load_default(64))
    return image


def encoded(images: list[Image.Image], format: str) -> bytes:
    buffer = io.BytesIO()
    images[0].save(
        buffer, format, save_all=len(images) > 1, append_images=images[1:], resolution=150
    )
    return buffer.getvalue()


@needs_tesseract
def test_photo_yields_text_fact_with_ocr_provenance():
    [fact] = parse(
        "receiving/WZ-1.jpg", encoded([scan("DELIVERY NOTE 4711")], "JPEG"), OCR("eng", 5, 60)
    )
    assert fact.kind == "text" and fact.locator == "page:1"
    assert "DELIVERY NOTE 4711" in fact.data["text"]
    assert fact.data["ocr"]["status"] == "ok"
    assert fact.data["ocr"]["engine"].startswith("tesseract ")
    assert fact.data["ocr"]["languages"] == "eng"


@needs_tesseract
def test_image_only_pdf_pages_fall_back_to_ocr_within_page_cap():
    pdf = encoded([scan("SUPPLY AGREEMENT"), scan("SECOND PAGE")], "PDF")
    first, second = parse("contract.pdf", pdf, OCR("eng", 1, 60))
    assert "SUPPLY AGREEMENT" in first.data["text"] and first.data["ocr"]["status"] == "ok"
    assert second.data == {
        "text": "",
        "ocr": first.data["ocr"] | {"status": "page limit reached"},
    }


def test_embedded_pdf_text_skips_ocr():
    data = Path("tests/fixtures/demo/amendment.pdf").read_bytes()
    assert parse("amendment.pdf", data, OCR("eng", 5, 60)) == parse("amendment.pdf", data)
    assert "ocr" not in parse("amendment.pdf", data, OCR("eng", 5, 60))[0].data


def test_without_ocr_scans_keep_previous_behavior():
    assert parse("photo.png", encoded([scan("IGNORED")], "PNG")) == []
    [page] = parse("scan.pdf", encoded([scan("IGNORED")], "PDF"))
    assert page.data == {"text": ""}


def test_missing_engine_is_reported_on_the_fact(monkeypatch):
    monkeypatch.setattr(ocr_module, "engine", lambda _: ("tesseract", "tesseract binary not found"))
    [fact] = parse("photo.png", encoded([scan("IGNORED")], "PNG"), OCR("eng", 5, 60))
    assert fact.data["text"] == ""
    assert fact.data["ocr"]["status"] == "tesseract binary not found"


def test_exhausted_budget_and_oversized_scan_skip_recognition(monkeypatch):
    monkeypatch.setattr(ocr_module, "engine", lambda _: ("tesseract 5", None))
    expired = OCR("eng", 5, 60)
    expired.deadline = 0
    assert expired.text("", lambda: scan("X"))["ocr"]["status"] == "time budget exhausted"
    monkeypatch.setattr(ocr_module, "MAX_PIXELS", 100)
    assert OCR("eng", 5, 60).text("", lambda: scan("X"))["ocr"]["status"] == "image too large"
