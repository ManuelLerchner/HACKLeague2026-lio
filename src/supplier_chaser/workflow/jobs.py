from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from supplier_chaser.adapters.db import JobRow, uid
from supplier_chaser.domain.models import Event


def enqueue(session: Session, kind: str, payload: dict, key: str, now: datetime) -> JobRow:
    existing = session.scalar(select(JobRow).where(JobRow.idempotency_key == key))
    if existing:
        return existing
    event = Event(
        id=uid(),
        type=kind,
        aggregate_id=payload.get("case_id", payload.get("po_line_id", "system")),
        occurred_at=now,
        payload=payload,
        idempotency_key=key,
    )
    job = JobRow(
        id=event.id,
        job_type=kind,
        payload_json=event.model_dump(mode="json"),
        idempotency_key=key,
        run_after=now,
    )
    session.add(job)
    session.flush()
    return job


def claim_job(factory: sessionmaker, owner: str, now: datetime) -> JobRow | None:
    with factory.begin() as session:
        eligible = and_(
            JobRow.run_after <= now,
            or_(
                JobRow.status == "queued",
                and_(JobRow.status == "running", JobRow.lease_until < now),
            ),
        )
        candidate = session.scalar(
            select(JobRow)
            .where(eligible)
            .order_by(JobRow.run_after, JobRow.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if candidate is None:
            return None
        result = session.execute(
            update(JobRow)
            .where(JobRow.id == candidate.id, eligible)
            .values(
                status="running",
                lease_owner=owner,
                lease_until=now + timedelta(minutes=5),
                attempts=JobRow.attempts + 1,
            )
        )
        if result.rowcount != 1:
            return None
        session.refresh(candidate)
        return candidate
