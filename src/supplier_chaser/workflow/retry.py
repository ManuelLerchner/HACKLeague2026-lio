"""Explicit recovery of failed processing work without replaying outbound side effects."""

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import CaseRow, JobRow, MessageRow
from supplier_chaser.cases.service import move
from supplier_chaser.domain.models import State
from supplier_chaser.workflow.events import append_event
from supplier_chaser.workflow.jobs import enqueue

RETRYABLE_JOB_TYPES = frozenset(
    {
        "investigate_document",
        "extract_source",
        "apply_mapping",
        "reconcile",
        "draft",
        "review_reply",
    }
)


def retry_job(session: Session, job: JobRow, actor: str, now: datetime) -> JobRow:
    if job.status != "failed" or job.job_type not in RETRYABLE_JOB_TYPES:
        raise ValueError(
            "Only failed processing jobs can be retried; outbound sends require review."
        )
    append_event(
        session,
        job.id,
        "workflow.job_retry_requested",
        {"by": actor, "attempts": job.attempts, "previous_error": job.last_error},
        now,
    )
    job.status = "queued"
    job.run_after = now
    job.lease_owner = None
    job.lease_until = None
    job.last_error = None
    return job


def retry_rate_limited_message(
    session: Session, message: MessageRow, actor: str, now: datetime
) -> JobRow:
    reasons = message.metadata_json.get("block_reasons", [])
    if (
        message.direction != "outbound"
        or message.status != "blocked"
        or len(reasons) != 1
        or not reasons[0].startswith("Supplier send limit reached:")
        or not message.metadata_json.get("approved_by")
        or message.metadata_json.get("approved_body_sha256")
        != hashlib.sha256(message.body.encode()).hexdigest()
    ):
        raise ValueError(
            "Only an unchanged, approved inquiry blocked by the supplier limit can resume."
        )
    case = session.scalar(select(CaseRow).where(CaseRow.id == message.case_id).with_for_update())
    if case is None or case.active_key is None or case.state != State.BLOCKED:
        raise ValueError("Case is no longer blocked on this send.")
    if session.scalar(
        select(MessageRow.id).where(
            MessageRow.case_id == case.id,
            MessageRow.direction == "inbound",
            MessageRow.status.in_(["needs_review", "review_queued"]),
        )
    ):
        raise ValueError("Review the held supplier reply before resuming an inquiry.")
    append_event(
        session,
        case.id,
        "message.rate_limit_retry_requested",
        {"by": actor, "message_id": message.id, "previous_reasons": reasons},
        now,
        case_id=case.id,
    )
    move(session, case, State.INVESTIGATING, now, "Resume unchanged approved information request")
    move(session, case, State.DRAFT_READY, now, "Existing approval retained for unchanged inquiry")
    message.status = "queued"
    message.metadata_json = {**message.metadata_json, "block_reasons": []}
    return enqueue(
        session,
        "send",
        {"case_id": case.id, "message_id": message.id},
        f"send-rate-recovery:{message.id}",
        now,
    )
