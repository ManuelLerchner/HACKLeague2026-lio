from fastapi.testclient import TestClient

from supplier_chaser.adapters.db import JobRow
from supplier_chaser.web.app import create_app


def test_queue_is_private_shows_active_work_and_refreshes(settings, factory, clock):
    with factory.begin() as session:
        for state in ["queued", "running", "failed", "succeeded"]:
            session.add(
                JobRow(
                    job_type=f"operation_{state}",
                    payload_json={},
                    idempotency_key=state,
                    status=state,
                    run_after=clock.now(),
                    last_error="<script>bad</script>" if state == "failed" else None,
                )
            )
    with TestClient(create_app(settings, factory, clock)) as client:
        for path in ["/worker-queue", "/worker-queue/panel", "/worker-queue/summary"]:
            assert client.get(path).status_code == 401
        client.headers["Authorization"] = "Bearer test-secret"
        page = client.get("/worker-queue")
        assert page.status_code == 200
        assert "1 queued · 1 running · 1 failed · 1 completed" in page.text
        assert "Operation running" in page.text
        assert "Operation succeeded" not in page.text
        assert "&lt;script&gt;bad&lt;/script&gt;" in page.text
        assert 'hx-trigger="every 5s"' in page.text
        assert "Worker offline" in page.text
        with factory.begin() as session:
            session.query(JobRow).filter_by(status="running").update({"status": "succeeded"})
        summary = client.get("/worker-queue/summary").text
        assert "1 queued · 0 running" in summary
        panel = client.get("/worker-queue/panel").text
        assert "Operation running" not in panel
