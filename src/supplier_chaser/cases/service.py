from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import CaseRow, MessageRow, PORow
from supplier_chaser.adapters.repositories import CommitmentRepository
from supplier_chaser.cases.state_machine import transition
from supplier_chaser.commitments.reconcile import reconcile
from supplier_chaser.config import Settings
from supplier_chaser.domain.models import State
from supplier_chaser.workflow.events import append_event
from supplier_chaser.workflow.jobs import enqueue


def record_event(session: Session, case: CaseRow, kind: str, payload: dict, now: datetime) -> None:
    append_event(session, case.id, kind, payload, now, case_id=case.id)


def move(
    session: Session,
    case: CaseRow,
    target: State,
    now: datetime,
    reason: str,
    adequate: bool = False,
) -> None:
    previous = case.state
    case.state = transition(State(previous), target, adequate_evidence=adequate)
    case.updated_at = now
    if target in {State.RESOLVED, State.CANCELLED}:
        case.active_key = None
        case.next_action_at = None
    if target in {State.ESCALATED, State.BLOCKED}:
        case.next_action_at = None
    record_event(
        session,
        case,
        f"case.{target.lower()}",
        {"from": previous, "to": target, "reason": reason},
        now,
    )


def reconcile_line(session: Session, line_id: str, settings: Settings, now: datetime) -> None:
    row = session.scalar(select(PORow).where(PORow.id == line_id).with_for_update())
    if row is None:
        return
    repository = CommitmentRepository(session)
    po = repository.get_po_line(line_id)
    assessment = reconcile(
        po,
        repository.get_claims(line_id),
        now,
        settings.confirmation_grace_hours,
        settings.launch_cutoff,
    )
    active = list(
        session.scalars(
            select(CaseRow).where(CaseRow.po_line_id == line_id, CaseRow.active_key.is_not(None))
        )
    )
    # Periodic refresh must not close a case while its new supplier evidence is held.
    if session.scalar(
        select(MessageRow.id)
        .where(
            MessageRow.case_id.in_([case.id for case in active]),
            MessageRow.direction == "inbound",
            MessageRow.status.in_(["needs_review", "review_queued"]),
        )
        .limit(1)
    ):
        return
    if assessment.status == "UNKNOWN":
        if not active:
            key = f"{po.supplier_id}:{line_id}:CONTRADICTORY_EVIDENCE"
            case = CaseRow(
                key=key,
                active_key=key,
                po_line_id=line_id,
                issue_type="CONTRADICTORY_EVIDENCE",
                priority="P1" if po.launch_critical else "P2",
                state=State.NEW,
                evidence_summary=assessment.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
            session.add(case)
            session.flush()
            active.append(case)
            record_event(session, case, "case.created", {"reason": assessment.reason}, now)
        for case in active:
            case.evidence_summary = assessment.model_dump(mode="json")
            if case.state != State.ESCALATED:
                move(session, case, State.ESCALATED, now, assessment.reason)
        return
    issue_types = {i.issue_type for i in assessment.issues}
    for case in active:
        if (
            case.issue_type not in issue_types
            and assessment.status in {"MATCH", "DISCREPANCY"}
            and "CONTRADICTORY_EVIDENCE" not in issue_types
            and assessment.latest_claim is not None
        ):
            if case.state != State.RECONCILING:
                if case.state == State.NEW:
                    move(session, case, State.INVESTIGATING, now, "Evidence available")
                move(session, case, State.RECONCILING, now, "Re-evaluate confirmation evidence")
            case.evidence_summary = assessment.model_dump(mode="json")
            move(session, case, State.RESOLVED, now, assessment.reason, adequate=True)
    for issue in assessment.issues:
        key = f"{po.supplier_id}:{line_id}:{issue.issue_type}"
        issue_case = session.scalar(select(CaseRow).where(CaseRow.active_key == key))
        summary = assessment.model_dump(mode="json")
        if issue_case is None:
            issue_case = CaseRow(
                key=key,
                active_key=key,
                po_line_id=line_id,
                issue_type=issue.issue_type,
                priority=issue.severity,
                state=State.NEW,
                evidence_summary=summary,
                created_at=now,
                updated_at=now,
            )
            session.add(issue_case)
            session.flush()
            record_event(session, issue_case, "case.created", issue.model_dump(mode="json"), now)
            if issue.issue_type == "CONTRADICTORY_EVIDENCE":
                move(session, issue_case, State.ESCALATED, now, issue.explanation)
            elif settings.auto_draft:
                enqueue(
                    session,
                    "draft",
                    {"case_id": issue_case.id},
                    f"initial-draft:{issue_case.id}",
                    now,
                )
        elif issue_case.evidence_summary != summary:
            issue_case.evidence_summary = summary
            issue_case.priority = issue.severity
            issue_case.updated_at = now
            record_event(session, issue_case, "case.updated", {"reason": assessment.reason}, now)
        if issue_case.state == State.RECONCILING:
            move(
                session,
                issue_case,
                State.ESCALATED,
                now,
                "Reply leaves a discrepancy; buyer review required",
            )
