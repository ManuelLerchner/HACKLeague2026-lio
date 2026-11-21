import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
import time_machine
from sqlalchemy import func, select

from supplier_chaser.adapters.db import CaseEvent, CaseRow, ClaimRow, JobRow, MessageRow, SourceFile
from supplier_chaser.clock import SystemClock
from supplier_chaser.communications.receive import receive
from supplier_chaser.communications.send import send_message
from supplier_chaser.demo import deliver_reply
from supplier_chaser.domain.models import Inbound, SendResult, State
from supplier_chaser.workflow.jobs import claim_job, enqueue
from supplier_chaser.workflow.worker import Worker


def counts(factory):
    with factory() as session:
        return {
            cls.__tablename__: session.scalar(select(func.count()).select_from(cls))
            for cls in (CaseRow, ClaimRow, MessageRow, SourceFile, CaseEvent)
        }


def snapshot_state(factory):
    with factory() as session:
        return [
            {
                "po_line": c.po_line_id,
                "issue": c.issue_type,
                "state": c.state,
                "priority": c.priority,
                "reason": c.evidence_summary["reason"],
                "messages": [
                    {
                        "direction": m.direction,
                        "to": m.recipients,
                        "status": m.status,
                        "body": m.body,
                    }
                    for m in session.scalars(
                        select(MessageRow)
                        .where(MessageRow.case_id == c.id)
                        .order_by(MessageRow.sent_or_received_at, MessageRow.direction)
                    )
                ],
            }
            for c in session.scalars(select(CaseRow).order_by(CaseRow.key))
        ]


def test_initial_loop_and_idempotent_repoll(seeded, worker, factory, clock):
    worker.tick()
    with factory() as session:
        assert set(session.scalars(select(CaseRow.state))) == {State.AWAITING_REPLY}
        assert session.scalar(select(func.count()).select_from(CaseRow)) == 2
        assert session.scalar(select(func.count()).select_from(MessageRow)) == 2
        assert not list(session.scalars(select(JobRow).where(JobRow.status == "failed")))
    before = counts(factory)
    clock.value += timedelta(minutes=2)
    worker.tick()
    assert counts(factory) == before


@pytest.mark.freeze
def test_full_replay(seeded, worker, factory, clock, snapshot):
    worker.tick()
    initial = snapshot_state(factory)
    shutil.copyfile(
        Path("tests/fixtures/demo/amendment.pdf"), seeded.fixture_portal_dir / "amendment.pdf"
    )
    clock.value += timedelta(minutes=2)
    worker.tick()
    amended = snapshot_state(factory)
    clock.value += timedelta(minutes=2)
    deliver_reply(seeded, factory, clock.now())
    worker.tick()
    final = snapshot_state(factory)
    before = counts(factory)
    clock.value += timedelta(minutes=2)
    worker.tick()
    assert counts(factory) == before
    assert all(c["state"] == "RESOLVED" for c in final if c["po_line"] == "DEMO-4100/10")
    assert {"initial": initial, "amended": amended, "final": final, "counts": before} == snapshot


@pytest.mark.freeze
def test_frozen_followup_and_restart(seeded, worker, factory, clock):
    with time_machine.travel(clock.now(), tick=False):
        assert SystemClock().now() == clock.now()
        worker.tick()
    clock.value += timedelta(hours=4, seconds=1)
    restarted = Worker(
        factory, seeded, clock, worker.portal, worker.mail, worker.extractor, worker.drafter
    )
    restarted.tick()
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MessageRow)) == 4
        assert set(session.scalars(select(CaseRow.reminders))) == {1}
    restarted.tick()
    clock.value += timedelta(hours=4, seconds=1)
    restarted.tick()
    with factory() as session:
        assert set(session.scalars(select(CaseRow.state))) == {State.ESCALATED}
        assert session.scalar(select(func.count()).select_from(MessageRow)) == 4


def test_lease_recovery_success_not_claimed(factory, clock):
    with factory.begin() as session:
        enqueue(session, "draft", {"case_id": "test"}, "one", clock.now())
    first = claim_job(factory, "a", clock.now())
    assert first is not None
    assert claim_job(factory, "b", clock.now()) is None
    clock.value += timedelta(minutes=6)
    recovered = claim_job(factory, "b", clock.now())
    assert recovered.id == first.id
    with factory.begin() as session:
        session.get(JobRow, first.id).status = "succeeded"
    assert claim_job(factory, "c", clock.now()) is None


def test_unknown_file_and_corrupt_file_visible(seeded, worker, factory):
    (seeded.fixture_portal_dir / "unknown.bin").write_bytes(b"opaque")
    (seeded.fixture_portal_dir / "bad.json").write_text('{"invalid":')
    worker.tick()
    with factory() as session:
        states = dict(session.execute(select(SourceFile.path, SourceFile.parse_status)).all())
        assert states["unknown.bin"] == "unhandled"
        assert states["bad.json"] == "needs_review"
        assert states["orders.csv"] == "parsed"


def test_uncertain_send_never_retried(seeded, worker, factory, clock):
    class Uncertain:
        calls = 0

        def send(self, message):
            self.calls += 1
            return SendResult(status="send_uncertain")

    transport = Uncertain()
    with factory.begin() as session:
        from supplier_chaser.ingestion.poll import poll_portal

        poll_portal(session, worker.portal, seeded, clock.now(), worker.extractor)
    # Prepare without draining the send jobs.
    for _ in range(4):
        job = claim_job(factory, "setup", clock.now())
        if job:
            if job.job_type != "send":
                worker.execute(job)
            with factory.begin() as session:
                session.get(JobRow, job.id).status = (
                    "succeeded" if job.job_type != "send" else "queued"
                )
    with factory() as session:
        message = session.scalar(select(MessageRow).where(MessageRow.status == "queued"))
        assert message is not None
        identifier = message.id
    send_message(factory, identifier, transport, seeded, clock.now())
    send_message(factory, identifier, transport, seeded, clock.now())
    assert transport.calls == 1


def test_ambiguous_reply_cannot_resolve(seeded, worker, factory, clock):
    worker.tick()
    deliver_reply(seeded, factory, clock.now())
    path = seeded.fixture_mail_dir / "inbox/reply-1.json"
    data = json.loads(path.read_text())
    data["identity_verified"] = False
    message = Inbound.model_validate(data)
    with factory.begin() as session:
        receive(session, message, worker.extractor, clock.now(), seeded.mail_from)
    with factory() as session:
        assert (
            session.scalar(select(CaseRow.state).where(CaseRow.po_line_id == "DEMO-4100/10"))
            == State.ESCALATED
        )


@pytest.mark.parametrize(
    "change", ["unknown_recipient", "unregistered_sender", "unverified_mailbox"]
)
def test_live_send_guards(seeded, worker, factory, clock, change):
    worker.tick()
    with factory.begin() as session:
        message = session.scalar(select(MessageRow).where(MessageRow.direction == "outbound"))
        case = session.get(CaseRow, message.case_id)
        case.state = State.DRAFT_READY
        message.status = "queued"
        if change == "unknown_recipient":
            message.recipients = ["attacker@example.test"]
        identifier = message.id
    live = seeded.model_copy(
        update={
            "mail_mode": "smtp_imap",
            "registered_sender_verified": change != "unregistered_sender",
            "mailbox_verified": change != "unverified_mailbox",
            "outbound_mail_enabled": True,
            "dry_run": False,
        }
    )

    class Forbidden:
        def send(self, message):
            pytest.fail("Transport must not be invoked")

    send_message(factory, identifier, Forbidden(), live, clock.now())
    with factory() as session:
        assert session.get(MessageRow, identifier).status == "blocked"
