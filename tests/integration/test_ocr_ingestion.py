import pytest
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from supplier_chaser.adapters.db import SourceFact, SourceFile
from supplier_chaser.ingestion.ocr import engine

_, UNAVAILABLE = engine("eng")


@pytest.mark.skipif(UNAVAILABLE is not None, reason=f"local OCR unavailable: {UNAVAILABLE}")
def test_polled_photo_stores_ocr_text_fact(seeded, worker, factory):
    seeded.ocr_languages = "eng"
    image = Image.new("RGB", (1400, 260), "white")
    font = ImageFont.load_default(64)
    ImageDraw.Draw(image).text((40, 80), "DELIVERY NOTE 4711", fill="black", font=font)
    image.save(seeded.fixture_portal_dir / "WZ-261021-680.jpg")
    worker.tick()
    with factory() as session:
        source = session.scalars(
            select(SourceFile).where(SourceFile.path == "WZ-261021-680.jpg")
        ).one()
        [fact] = session.scalars(select(SourceFact).where(SourceFact.source_file_id == source.id))
        assert source.parse_status != "unhandled"
        assert fact.fact_type == "text" and fact.confidence == "ocr"
        assert "DELIVERY NOTE 4711" in fact.normalized_json["text"]
        assert fact.normalized_json["ocr"]["status"] == "ok"
