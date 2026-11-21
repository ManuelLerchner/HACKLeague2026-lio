import hashlib
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from supplier_chaser.adapters.db import CaseEvent, CaseRow, JobRow, MessageRow
from supplier_chaser.communications.send import send_message
from supplier_chaser.domain.models import SendResult, State
from supplier_chaser.web.app import create_app
from supplier_chaser.workflow.jobs import enqueue
from supplier_chaser.workflow.retry import retry_job, retry_rate_limited_message


class CountingMail:
    def __init__(self):
        self.calls = 0

    def send(self, message):
        self.calls += 1
        return SendResult(status="sent")


def approved_inquiry(seeded, factory, clock, worker):
    seeded.auto_draft = False
    worker.tick()
    with TestClient(create_app(seeded, factory, clock)) as client:
        client.headers["Authorization"] = "Bearer test-secret"
        case_id = client.get("/api/v1/cases").json()[0]["id"]
        client.post(f"/api/v1/cases/{case_id}/draft")
        worker.drain()
        client.post(f"/api/v1/cases/{case_id}/approve-send", json={})
    with factory() as session:
        return session.scalar(select(MessageRow).where(MessageRow.case_id == case_id)).id


def fill_supplier_quota(factory, message_id, now):
    with factory.begin() as session:
        message = session.get(MessageRow, message_id)
        case = session.get(CaseRow, message.case_id)
        previous = CaseRow(
            id="previous-case",
            key="previous",
            active_key=None,
            po_line_id=case.po_line_id,
            issue_type=case.issue_type,
            priority=case.priority,
            state=State.RESOLVED,
            evidence_summary={},
            created_at=now,
            updated_at=now,
        )
        session.add(previous)
        session.flush()
        session.add(
            MessageRow(
                id="previous-message",
                external_message_id="<previous@example.test>",
                case_id=previous.id,
                direction="outbound",
                sender=message.sender,
                recipients=message.recipients,
                subject="Previous inquiry",
                body="Previous inquiry",
                status="sent",
                sent_or_received_at=now,
                metadata_json={},
            )
        )


def test_supplier_limit_defers_same_approved_message_and_sends_once(seeded, factory, clock, worker):
    message_id = approved_inquiry(seeded, factory, clock, worker)
    seeded.max_supplier_sends_per_day = 1
    fill_supplier_quota(factory, message_id, clock.now())
    transport = CountingMail()
    send_message(factory, message_id, transport, seeded, clock.now())
    send_message(factory, message_id, transport, seeded, clock.now())
    assert transport.calls == 0
    with factory() as session:
        deferred = list(
            session.scalars(
                select(JobRow).where(JobRow.idempotency_key.startswith("send-deferred:"))
            )
        )
        assert len(deferred) == 1
        assert deferred[0].status == "queued"
        assert deferred[0].run_after == clock.now() + timedelta(days=1, microseconds=1)
        assert session.get(MessageRow, message_id).status == "queued"
        message_count = len(list(session.scalars(select(MessageRow))))
    clock.value += timedelta(days=1, seconds=1)
    send_message(factory, message_id, transport, seeded, clock.now())
    send_message(factory, message_id, transport, seeded, clock.now())
    assert transport.calls == 1
    with factory() as session:
        assert len(list(session.scalars(select(MessageRow)))) == message_count
        assert session.get(MessageRow, message_id).status == "sent"


@pytest.mark.parametrize("change", ["held_reply", "edited_body", "resolved", "unverified"])
def test_deferred_send_rechecks_current_authorization(seeded, factory, clock, worker, change):
    message_id = approved_inquiry(seeded, factory, clock, worker)
    seeded.max_supplier_sends_per_day = 1
    fill_supplier_quota(factory, message_id, clock.now())
    transport = CountingMail()
    send_message(factory, message_id, transport, seeded, clock.now())
    with factory.begin() as session:
        message = session.get(MessageRow, message_id)
        case = session.get(CaseRow, message.case_id)
        if change == "held_reply":
            session.add(
                MessageRow(
                    case_id=case.id,
                    direction="inbound",
                    sender=message.recipients[0],
                    recipients=[message.sender],
                    subject="Reply",
                    external_message_id="<reply@example.test>",
                    body="Please review",
                    status="needs_review",
                    sent_or_received_at=clock.now(),
                    metadata_json={},
                )
            )
        elif change == "edited_body":
            message.body += " A material change."
        elif change == "resolved":
            case.state, case.active_key = State.RESOLVED, None
        else:
            from supplier_chaser.adapters.db import PORow, SupplierRow

            po = session.get(PORow, case.po_line_id)
            session.get(SupplierRow, po.supplier_id).verified = False
    clock.value += timedelta(days=1, seconds=1)
    send_message(factory, message_id, transport, seeded, clock.now())
    assert transport.calls == 0
    with factory() as session:
        assert session.get(MessageRow, message_id).status in {"blocked", "cancelled"}


def test_retry_processing_preserves_attempts_and_error_audit(factory, clock):
    with factory.begin() as session:
        job = enqueue(session, "investigate_document", {}, "failed-processing", clock.now())
        job.status, job.attempts, job.last_error = "failed", 5, "Source validation failed"
        retry_job(session, job, "buyer", clock.now())
        assert (job.status, job.attempts, job.last_error) == ("queued", 5, None)
        event = session.scalar(
            select(CaseEvent).where(CaseEvent.kind == "workflow.job_retry_requested")
        )
        assert event.payload_json["payload"]["previous_error"] == "Source validation failed"
        with pytest.raises(ValueError):
            retry_job(session, job, "buyer", clock.now())


def test_retry_routes_require_authentication_csrf_and_safe_job_type(settings, factory, clock):
    with factory.begin() as session:
        job = enqueue(session, "investigate_document", {}, "retry-route", clock.now())
        job.status = "failed"
        identifier = job.id
        unsafe = enqueue(session, "send", {}, "unsafe-route", clock.now())
        unsafe.status = "failed"
        unsafe_id = unsafe.id
    with TestClient(create_app(settings, factory, clock)) as client:
        path = f"/api/v1/jobs/{identifier}/retry"
        assert client.post(path).status_code == 401
        assert client.post("/api/v1/messages/missing/retry-send").status_code == 401
        assert client.post("/login", data={"token": "test-secret"}).status_code == 200
        assert client.post(path).status_code == 403
        page = client.get("/worker-queue").text
        assert f'data-retry-url="{path}"' in page
        assert f'data-retry-url="/api/v1/jobs/{unsafe_id}/retry"' not in page
        token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        client.headers["X-CSRF-Token"] = token
        assert client.post(path).status_code == 202
        assert client.post(path).status_code == 409
        assert client.post(f"/api/v1/jobs/{unsafe_id}/retry").status_code == 409
        assert client.post("/api/v1/jobs/missing/retry").status_code == 404


def test_explicit_rate_limit_recovery_rejects_uncertain_and_changed_messages(
    seeded, factory, clock, worker
):
    message_id = approved_inquiry(seeded, factory, clock, worker)
    with factory.begin() as session:
        message = session.get(MessageRow, message_id)
        case = session.get(CaseRow, message.case_id)
        message.metadata_json = {
            **message.metadata_json,
            "block_reasons": ["Supplier send limit reached: 2 sends; limit 2."],
        }
        case.state = State.BLOCKED
        for status in ["sent", "send_uncertain", "sending", "failed_before_send"]:
            message.status = status
            with pytest.raises(ValueError):
                retry_rate_limited_message(session, message, "buyer", clock.now())
        message.status = "blocked"
        message.body += " edited"
        with pytest.raises(ValueError):
            retry_rate_limited_message(session, message, "buyer", clock.now())
        message.body = message.body.removesuffix(" edited")
        job = retry_rate_limited_message(session, message, "buyer", clock.now())
        assert job.job_type == "send"
        assert message.status == "queued"
        assert case.state == State.DRAFT_READY
        assert (
            message.metadata_json["approved_body_sha256"]
            == hashlib.sha256(message.body.encode()).hexdigest()
        )


def test_queue_explains_deliberately_delayed_jobs(settings, factory, clock):
    from datetime import timedelta

    with factory.begin() as session:
        job = enqueue(session, "reconcile", {"po_line_id": "X/1"}, "later", clock.now())
        job.run_after = clock.now() + timedelta(hours=20)
    with TestClient(create_app(settings, factory, clock)) as client:
        client.post("/login", data={"token": "test-secret"})
        page = client.get("/worker-queue").text
        assert "Scheduled for a later run. Starts" in page
        assert "Waiting for worker / scheduled time" not in page
