from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from math import isfinite

from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import CaseRow, JobRow, SystemRow
from supplier_chaser.cases.service import move, record_event
from supplier_chaser.config import Settings
from supplier_chaser.domain.models import State
from supplier_chaser.workflow.jobs import enqueue


def follow_ups(session: Session, settings: Settings, now: datetime) -> None:
    cases = session.scalars(
        select(CaseRow)
        .where(CaseRow.state == State.AWAITING_REPLY, CaseRow.next_action_at <= now)
        .with_for_update(skip_locked=True)
    )
    for case in cases:
        if case.reminders >= settings.max_reminders:
            move(session, case, State.ESCALATED, now, "NO_RESPONSE: reminder limit reached")
        else:
            case.reminders += 1
            case.next_action_at = None
            record_event(
                session, case, "commitment.discrepancy_detected", {"issue_type": "NO_RESPONSE"}, now
            )
            enqueue(
                session,
                "draft",
                {"case_id": case.id, "reminder": True},
                f"reminder:{case.id}:{case.reminders}",
                now,
            )


def schedule(session: Session, settings: Settings, now: datetime) -> None:
    for kind in ("portal", "mailbox", "followups", "refresh"):
        pending = session.scalar(
            select(JobRow.id)
            .where(JobRow.job_type == kind, JobRow.status.in_(["queued", "running"]))
            .limit(1)
        )
        if pending:
            continue
        slot = int(now.timestamp()) // settings.portal_poll_seconds
        enqueue(session, kind, {}, f"{kind}:{slot}", now)
    heartbeat = session.get(SystemRow, "worker")
    if heartbeat:
        heartbeat.updated_at = now
        heartbeat.value = {"status": "running"}
    else:
        session.add(SystemRow(key="worker", value={"status": "running"}, updated_at=now))


def retry_delay(
    attempts: int, retry_after: str | None = None, now: datetime | None = None
) -> timedelta:
    seconds = min(300, 2 ** min(attempts, 8))
    if retry_after:
        try:
            requested = float(retry_after)
        except ValueError:
            try:
                target = parsedate_to_datetime(retry_after)
                requested = (target - now).total_seconds() if now and target.tzinfo else 0
            except (ValueError, TypeError):
                requested = 0
        if isfinite(requested):
            seconds = max(seconds, min(3600, int(requested)))
    return timedelta(seconds=seconds)
