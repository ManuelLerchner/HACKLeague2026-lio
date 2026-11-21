from fastapi.testclient import TestClient
from sqlalchemy import select

from supplier_chaser.adapters.db import CaseRow, MessageRow
from supplier_chaser.cases.service import move
from supplier_chaser.domain.models import State
from supplier_chaser.web.app import create_app


def test_authenticated_views_and_protected_actions(seeded, factory, clock, worker):
    worker.tick()
    app = create_app(seeded, factory, clock)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/cases").status_code == 401
        assert client.post("/api/v1/cases/unknown/draft").status_code == 401
        client.headers["Authorization"] = "Bearer test-secret"
        cases = client.get("/api/v1/cases").json()
        assert len(cases) == 2
        case_id = cases[0]["id"]
        assert client.get("/").status_code == 200
        assert client.get(f"/cases/{case_id}").status_code == 200
        assert client.get("/system").status_code == 200
        assert client.get("/api/v1/cases/nope").status_code == 404
        assert client.get("/api/v1/cases?priority=oops").status_code == 422
        assert client.post(f"/api/v1/cases/{case_id}/approve-send", json={}).status_code == 409
        assert client.post(f"/api/v1/cases/{case_id}/reconcile").status_code == 202
        evidence = client.get(f"/api/v1/cases/{case_id}").json()["evidence"]
        assert evidence
        assert client.get(f"/evidence/{evidence[0]['id']}").status_code == 200
        del client.headers["Authorization"]
        assert client.get(f"/evidence/{evidence[0]['id']}").status_code == 401


def test_session_requires_csrf(seeded, factory, clock, worker):
    worker.tick()
    with TestClient(create_app(seeded, factory, clock)) as client:
        assert client.post("/login", data={"token": "wrong"}).status_code == 401
        assert client.post("/login", data={"token": "test-secret"}).status_code == 200
        case_id = client.get("/api/v1/cases").json()[0]["id"]
        assert client.post(f"/api/v1/cases/{case_id}/reconcile").status_code == 403


def test_held_reply_explains_manual_action_and_recheck_limits(seeded, factory, clock, worker):
    worker.tick()
    with factory.begin() as session:
        case = session.scalar(select(CaseRow))
        case_id = case.id
        move(session, case, State.ESCALATED, clock.now(), "Sender authentication is unverified")
        session.add(
            MessageRow(
                case_id=case.id,
                external_message_id="held-reply@example.test",
                direction="inbound",
                sender="supplier@example.test",
                recipients=["buyer@example.test"],
                subject="Confirmation",
                body="Please review the proposed terms.",
                status="needs_review",
                sent_or_received_at=clock.now(),
            )
        )
    with TestClient(create_app(seeded, factory, clock)) as client:
        client.headers["Authorization"] = "Bearer test-secret"
        response = client.get(f"/cases/{case_id}")
        assert response.status_code == 200
        assert "This case is paused for manual review" in response.text
        assert "Sender authentication is unverified" in response.text
        assert "Reply received, awaiting review" in response.text
        assert "only evaluates evidence already imported" in response.text
        assert "no in-app control" in response.text
