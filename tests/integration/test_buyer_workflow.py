import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from supplier_chaser.adapters.db import ApprovalRow, CaseRow, ClaimRow, MessageRow
from supplier_chaser.adapters.llm_fixture import FixtureLLM
from supplier_chaser.communications.receive import receive
from supplier_chaser.demo import deliver_reply
from supplier_chaser.domain.models import Inbound
from supplier_chaser.web.app import create_app


class ReviewedDrafter:
    model_version = "test-reviewed-draft"

    def draft_supplier_question(self, po, supplier, reason):
        return FixtureLLM().draft_supplier_question(po, supplier, reason)


@pytest.mark.parametrize("outcome", ["match", "terms", "invalid", "reject"])
def test_buyer_draft_send_review_loop(seeded, factory, clock, worker, outcome):
    seeded.auto_draft = False
    worker.drafter = ReviewedDrafter()
    worker.tick()
    with TestClient(create_app(seeded, factory, clock)) as client:
        client.headers["Authorization"] = "Bearer test-secret"
        cases = client.get("/api/v1/cases").json()
        case = next(c for c in cases if c["issue_type"] == "QUANTITY_MISMATCH")
        url = f"/api/v1/cases/{case['id']}"
        page = client.get(f"/cases/{case['id']}").text
        assert "Draft supplier question" in page
        assert "The worker will prepare" not in page
        first = client.post(url + "/draft").json()
        assert client.post(url + "/draft").json()["job_id"] == first["job_id"]
        assert client.get(url).json()["action_group"] == "In progress"
        worker.drain()
        assert client.get(url).json()["state"] == "NEEDS_APPROVAL"
        assert client.post(url + "/approve-send", json={}).status_code == 202
        worker.drain()
        assert client.get(url).json()["state"] == "AWAITING_REPLY"
        deliver_reply(seeded, factory, clock.now())
        data = json.loads((seeded.fixture_mail_dir / "inbox/reply-1.json").read_text())
        data["identity_verified"] = False
        if outcome == "terms":
            extraction = json.loads(Path("tests/fixtures/demo/reply.json").read_text())
            extraction["terms"] = "Release requires acceptance of EXW"
            data["body"] = (
                extraction["evidence_quote"] + "\nCONFIRMATION_JSON:" + json.dumps(extraction)
            )
        elif outcome == "invalid":
            data["body"] = "We may be able to ship, but have no confirmed quantity or date."
        with factory.begin() as session:
            receive(
                session,
                Inbound.model_validate(data),
                worker.extractor,
                clock.now(),
                seeded.mail_from,
            )
        detail = client.get(url).json()
        reply = next(m for m in detail["messages"] if m["direction"] == "inbound")
        assert detail["action_group"] == "Needs your action"
        assert detail["state"] == "ESCALATED"
        review = {
            "message_id": reply["id"],
            "decision": "reject" if outcome == "reject" else "verify",
            "reviewer": "Test Buyer",
            "note": "Checked supplier directory and original mailbox.",
            "identity_verified": outcome == "reject",
        }
        if outcome != "reject":
            assert client.post(url + "/review-reply", json=review).status_code == 422
        review["identity_verified"] = True
        other = next(c for c in cases if c["id"] != case["id"])
        assert (
            client.post(f"/api/v1/cases/{other['id']}/review-reply", json=review).status_code == 404
        )
        assert client.post(url + "/review-reply", json=review).status_code == 202
        assert client.post(url + "/review-reply", json=review).status_code == 202
        worker.drain()
        final = client.get(url).json()
        if outcome == "match":
            assert final["state"] == "RESOLVED"
            assert final["action_group"] == "Completed"
        else:
            assert final["state"] == "ESCALATED"
        if outcome == "terms":
            assert final["assessment"]["latest_claim"]["confirmation"]["terms"]
        if outcome == "reject":
            assert (
                next(m for m in final["messages"] if m["id"] == reply["id"])["status"] == "rejected"
            )
        if outcome == "invalid":
            rejection = {
                **review,
                "decision": "reject",
                "note": "Reply contains no supported commitment; request clarification.",
            }
            assert client.post(url + "/review-reply", json=rejection).status_code == 202
            assert client.post(url + "/review-reply", json=rejection).status_code == 202
            reviewed = next(m for m in client.get(url).json()["messages"] if m["id"] == reply["id"])
            assert reviewed["status"] == "rejected"
        ordered = client.get("/api/v1/cases").json()
        assert ordered[0]["action_group"] == "Needs your action"
        assert "Needs your action" in client.get("/").text
    with factory() as session:
        assert session.scalar(
            select(func.count())
            .select_from(ApprovalRow)
            .where(ApprovalRow.action_type == "verify_reply_identity")
        ) == (2 if outcome == "invalid" else 1)
        assert session.scalar(
            select(func.count()).select_from(ClaimRow).where(ClaimRow.message_id == reply["id"])
        ) == (1 if outcome in {"match", "terms"} else 0)
        assert (
            session.scalar(
                select(func.count())
                .select_from(MessageRow)
                .where(MessageRow.direction == "outbound")
            )
            == 1
        )


def test_review_requires_authentication_and_csrf(seeded, factory, clock):
    with TestClient(create_app(seeded, factory, clock)) as client:
        body = {
            "message_id": "none",
            "decision": "reject",
            "reviewer": "Buyer",
            "note": "Not a trusted supplier",
        }
        assert client.post("/api/v1/cases/none/review-reply", json=body).status_code == 401
        client.post("/login", data={"token": "test-secret"})
        assert client.post("/api/v1/cases/none/review-reply", json=body).status_code == 403


def test_action_group_precedes_priority_and_manual_fixture_draft_never_sends(
    seeded, factory, clock, worker
):
    seeded.auto_draft = False
    worker.tick()
    with factory.begin() as session:
        cases = list(session.scalars(select(CaseRow).order_by(CaseRow.id)))
        cases[0].state, cases[0].priority = "AWAITING_REPLY", "P0"
        cases[1].state, cases[1].priority = "NEW", "P3"
        action_id = cases[1].id
    with TestClient(create_app(seeded, factory, clock)) as client:
        client.headers["Authorization"] = "Bearer test-secret"
        inbox = client.get("/api/v1/cases").json()
        assert inbox[0]["id"] == action_id
        assert client.post(f"/api/v1/cases/{action_id}/draft").status_code == 202
        worker.drain()
        detail = client.get(f"/api/v1/cases/{action_id}").json()
        assert detail["state"] == "NEEDS_APPROVAL"
        assert [m["status"] for m in detail["messages"]] == ["draft"]


def test_escalated_draft_is_held_and_can_be_replaced(seeded, factory, clock, worker):
    from datetime import timedelta

    from supplier_chaser.domain.models import State

    seeded.auto_draft = False
    worker.tick()
    with TestClient(create_app(seeded, factory, clock)) as client:
        client.headers["Authorization"] = "Bearer test-secret"
        case = client.get("/api/v1/cases").json()[0]
        url = f"/api/v1/cases/{case['id']}"
        client.post(url + "/draft")
        worker.drain()
        with factory.begin() as session:
            row = session.get(CaseRow, case["id"])
            row.state = State.ESCALATED
            row.updated_at = clock.now() + timedelta(seconds=1)
        page = client.get(f"/cases/{case['id']}").text
        assert "What happens next" in page
        assert "Waiting alone will not progress" in page
        assert "Prepare updated clarification" in page
        assert 'data-action="approve-send"' not in page
        assert client.post(url + "/approve-send", json={}).status_code == 409
        assert client.post(url + "/draft").status_code == 202
        worker.drain()
        detail = client.get(url).json()
        assert detail["state"] == "NEEDS_APPROVAL"
        assert sorted(m["status"] for m in detail["messages"]) == ["draft", "superseded"]
        assert 'data-action="approve-send"' in client.get(f"/cases/{case['id']}").text


def test_held_reply_blocks_approval_even_if_old_draft_state_remains(seeded, factory, clock, worker):
    seeded.auto_draft = False
    worker.tick()
    with TestClient(create_app(seeded, factory, clock)) as client:
        client.headers["Authorization"] = "Bearer test-secret"
        case = client.get("/api/v1/cases").json()[0]
        url = f"/api/v1/cases/{case['id']}"
        client.post(url + "/draft")
        worker.drain()
        with factory.begin() as session:
            session.add(
                MessageRow(
                    case_id=case["id"],
                    external_message_id="held-review@example.test",
                    direction="inbound",
                    sender="supplier@example.test",
                    recipients=[seeded.mail_from],
                    subject="Reply",
                    body="Please review the confirmation.",
                    status="needs_review",
                    sent_or_received_at=clock.now(),
                    correlation_key=case["id"],
                    metadata_json={},
                )
            )
        response = client.post(url + "/approve-send", json={})
        assert response.status_code == 409
        assert "received supplier reply" in response.json()["detail"]
        page = client.get(f"/cases/{case['id']}").text
        assert 'data-action="approve-send"' not in page
        assert "Review supplier reply" in page
        assert client.get(url).json()["next_action"] == "Review supplier reply"
        assert client.post(url + "/draft").status_code == 409
