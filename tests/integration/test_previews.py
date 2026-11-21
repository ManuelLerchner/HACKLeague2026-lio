from fastapi.testclient import TestClient
from sqlalchemy import select

from supplier_chaser.adapters.db import SourceFact, SourceFile
from supplier_chaser.web.app import create_app


def test_preview_is_inline_authenticated_and_download_remains_explicit(
    seeded, factory, clock, worker
):
    worker.tick()
    with factory() as session:
        source = session.scalar(select(SourceFile).where(SourceFile.path == "orders.csv"))
        fact = session.scalar(select(SourceFact).where(SourceFact.source_file_id == source.id))
        source_id, fact_id = source.id, fact.id
    with TestClient(create_app(seeded, factory, clock)) as client:
        for url in [f"/sources/{source_id}/preview", f"/evidence/{fact_id}/preview"]:
            assert client.get(url).status_code == 401
        client.headers["Authorization"] = "Bearer test-secret"
        for url in [f"/sources/{source_id}/preview", f"/evidence/{fact_id}/preview"]:
            response = client.get(url)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/plain")
            assert response.headers["content-disposition"].startswith("inline")
            assert response.headers["x-frame-options"] == "SAMEORIGIN"
            assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
            assert "nosniff" == response.headers["x-content-type-options"]
        assert (
            client.get(f"/sources/{source_id}/download")
            .headers["content-disposition"]
            .startswith("attachment")
        )
        progress = client.get(f"/api/v1/sources/{source_id}/progress").json()
        assert progress["pending"] is False
        assert len(progress["revision"]) == 64
        assert "Preview original evidence" in client.get(f"/sources/{source_id}").text


def test_html_is_previewed_as_inert_text_and_unsupported_formats_are_explicit(
    settings, factory, clock, worker
):
    settings.fixture_portal_dir.mkdir()
    (settings.fixture_portal_dir / "note.html").write_text("<script>alert('untrusted')</script>")
    (settings.fixture_portal_dir / "archive.docx").write_bytes(b"unsupported")
    worker.tick()
    with factory() as session:
        ids = {row.path: row.id for row in session.scalars(select(SourceFile))}
    with TestClient(create_app(settings, factory, clock)) as client:
        client.headers["Authorization"] = "Bearer test-secret"
        response = client.get(f"/sources/{ids['note.html']}/preview")
        assert response.headers["content-type"].startswith("text/plain")
        assert "sandbox" in response.headers["content-security-policy"]
        assert client.get(f"/sources/{ids['archive.docx']}/preview").status_code == 415
