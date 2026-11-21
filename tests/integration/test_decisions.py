import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_document_investigation import seed_document

from supplier_chaser.adapters.db import CaseRow, JobRow, PORow
from supplier_chaser.adapters.repositories import CommitmentRepository
from supplier_chaser.cases.decisions import DecisionRequest, record_decision
from supplier_chaser.commitments.reconcile import reconcile
from supplier_chaser.domain.models import Confirmation
from supplier_chaser.ingestion.poll import save_claim
from supplier_chaser.web.app import create_app


def line_claim(session, now, day="2027-01-04", line="2"):
    save_claim(
        session,
        Confirmation(
            document_id=f"reply-{line}-{day}",
            po_number="TEST-ORDER",
            line_number=line,
            supplier_id="SUP-TEST",
            unit="PC",
            deliveries=[{"quantity": "2600", "promised_date": day}],
            intent="confirmed",
        ),
        now,
    )
    session.flush()


def assess(session, line, now):
    repository = CommitmentRepository(session)
    return reconcile(repository.get_po_line(line), repository.get_claims(line), now)


def test_accepted_late_date_resolves_line_until_a_later_slip(settings, factory, clock):
    with factory.begin() as session:
        seed_document(session, clock.now())
        line_claim(session, clock.now(), "2027-01-09")
        late = assess(session, "TEST-ORDER/2", clock.now())
        assert [i.issue_type for i in late.issues] == ["LATE_COMMITMENT"]
        case = CaseRow(
            key="late",
            active_key="late",
            po_line_id="TEST-ORDER/2",
            issue_type="LATE_COMMITMENT",
            priority="P2",
            state="ESCALATED",
            evidence_summary=late.model_dump(mode="json"),
            created_at=clock.now(),
            updated_at=clock.now(),
        )
        session.add(case)
        session.flush()
        request = DecisionRequest(
            kind="late_date",
            choice="accept_date",
            decided_by="Buyer",
            reason="Stock covers demand until mid January.",
            case_id=case.id,
            promised="2027-01-09",
        )
        with pytest.raises(ValueError, match="current promised date"):
            record_decision(
                session, request.model_copy(update={"promised": "2027-01-08"}), clock.now()
            )
        _, lines = record_decision(session, request, clock.now())
        assert lines == ["TEST-ORDER/2"]
        assert session.scalar(select(JobRow).where(JobRow.job_type == "reconcile"))
        assert assess(session, "TEST-ORDER/2", clock.now()).status == "MATCH"
        line_claim(session, clock.now(), "2027-01-20")
        slipped = assess(session, "TEST-ORDER/2", clock.now())
        assert slipped.status != "MATCH"


def test_supplier_decision_applies_to_every_line_of_that_supplier(settings, factory, clock):
    with factory.begin() as session:
        seed_document(session, clock.now())
        for line in ("1", "2"):
            po = session.get(PORow, f"TEST-ORDER/{line}")
            session.add(
                CaseRow(
                    key=f"c{line}",
                    active_key=f"c{line}",
                    po_line_id=po.id,
                    issue_type="MISSING_CONFIRMATION",
                    priority="P2",
                    state="ESCALATED",
                    evidence_summary={},
                    created_at=clock.now(),
                    updated_at=clock.now(),
                )
            )
        session.flush()
        with pytest.raises(ValueError, match="renewed agreement"):
            record_decision(
                session,
                DecisionRequest(
                    kind="agreement_expiry",
                    choice="renewal_confirmed",
                    decided_by="Buyer",
                    reason="Amendment signed on 2026-11-20.",
                    supplier_id="SUP-TEST",
                ),
                clock.now(),
            )
        _, lines = record_decision(
            session,
            DecisionRequest(
                kind="supplier_qualification",
                choice="accept_conditional",
                decided_by="Buyer",
                reason="Quality released serial supply for this quarter.",
                supplier_id="SUP-TEST",
            ),
            clock.now(),
        )
        assert lines == ["TEST-ORDER/1", "TEST-ORDER/2"]
        assert len(CommitmentRepository(session).decisions("SUP-TEST", "TEST-ORDER/1")) == 1


def test_decision_route_requires_session_and_csrf(settings, factory, clock):
    with factory.begin() as session:
        seed_document(session, clock.now())
    body = {
        "kind": "supplier_qualification",
        "choice": "accept_conditional",
        "decided_by": "Buyer",
        "reason": "Quality released serial supply.",
        "supplier_id": "SUP-TEST",
    }
    with TestClient(create_app(settings, factory, clock)) as client:
        assert client.post("/api/v1/decisions", json=body).status_code == 401
        client.post("/login", data={"token": "test-secret"})
        assert client.post("/api/v1/decisions", json=body).status_code == 403
        page = client.get("/worker-queue").text
        client.headers["X-CSRF-Token"] = re.search(
            r'name="csrf-token" content="([^"]+)"', page
        ).group(1)
        assert (
            client.post("/api/v1/decisions", json={**body, "choice": "nonsense"}).status_code == 422
        )
        assert client.post("/api/v1/decisions", json=body).status_code == 202
