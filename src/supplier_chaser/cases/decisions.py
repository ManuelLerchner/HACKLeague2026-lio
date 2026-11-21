"""Record buyer decisions that settle failed checks, once per supplier or per order line."""

from datetime import date, datetime
from typing import Literal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import CaseRow, DecisionRow, PORow
from supplier_chaser.cases.service import record_event
from supplier_chaser.domain.models import Value
from supplier_chaser.workflow.jobs import enqueue

CHOICES = {
    "terms_conflict": {"accept_supplier_terms", "order_terms_apply"},
    "agreement_expiry": {"renewal_confirmed", "not_renewed"},
    "supplier_qualification": {"accept_conditional", "block"},
    "late_date": {"accept_date"},
}


class DecisionRequest(Value):
    kind: Literal["terms_conflict", "agreement_expiry", "supplier_qualification", "late_date"]
    choice: str
    decided_by: str = Field(min_length=2, max_length=100)
    reason: str = Field(min_length=10, max_length=2000)
    supplier_id: str | None = None
    case_id: str | None = None
    accepted: str | None = None
    promised: date | None = None
    valid_until: date | None = None


def _promised_days(case: CaseRow) -> list[date]:
    claim = (case.evidence_summary or {}).get("latest_claim") or {}
    deliveries = claim.get("confirmation", {}).get("deliveries", [])
    return [date.fromisoformat(d["promised_date"]) for d in deliveries if d.get("promised_date")]


def record_decision(
    session: Session, request: DecisionRequest, now: datetime
) -> tuple[DecisionRow, list[str]]:
    if request.choice not in CHOICES[request.kind]:
        raise ValueError(f"{request.choice} is not a choice for {request.kind}")
    value: dict = {}
    po_line_id = None
    if request.kind == "late_date":
        case = session.get(CaseRow, request.case_id) if request.case_id else None
        if case is None or case.active_key is None:
            raise LookupError("Open case not found")
        promised = _promised_days(case)
        # Accept exactly what the supplier promised; a later slip needs a new decision.
        if not promised or request.promised != max(promised):
            raise ValueError("Accept the supplier's current promised date")
        po = session.get(PORow, case.po_line_id)
        assert po is not None
        supplier_id, po_line_id = po.supplier_id, po.id
        value = {"promised": request.promised.isoformat()}
    else:
        if not request.supplier_id:
            raise ValueError("A supplier decision needs the supplier")
        supplier_id = request.supplier_id
        if request.choice == "accept_supplier_terms":
            if not request.accepted:
                raise ValueError("Name the supplier terms you accept")
            value = {"accepted": request.accepted}
        if request.choice == "renewal_confirmed" and not request.valid_until:
            raise ValueError("Give the date until which the renewed agreement applies")
    row = DecisionRow(
        kind=request.kind,
        supplier_id=supplier_id,
        po_line_id=po_line_id,
        choice=request.choice,
        value=value,
        reason=request.reason,
        decided_by=request.decided_by,
        decided_at=now,
        valid_until=request.valid_until,
    )
    session.add(row)
    session.flush()
    lines = (
        [po_line_id]
        if po_line_id
        else sorted(
            set(
                session.scalars(
                    select(CaseRow.po_line_id)
                    .join(PORow, PORow.id == CaseRow.po_line_id)
                    .where(PORow.supplier_id == supplier_id, CaseRow.active_key.is_not(None))
                )
            )
        )
    )
    for line in lines:
        for case in session.scalars(
            select(CaseRow).where(CaseRow.po_line_id == line, CaseRow.active_key.is_not(None))
        ):
            record_event(
                session,
                case,
                "decision.recorded",
                {
                    "decision_id": row.id,
                    "kind": row.kind,
                    "choice": row.choice,
                    "reason": f"{row.decided_by}: {row.reason}",
                },
                now,
            )
        enqueue(session, "reconcile", {"po_line_id": line}, f"decision:{row.id}:{line}", now)
    return row, lines


def open_cases_for_supplier(session: Session, supplier_id: str) -> int:
    return len(
        list(
            session.scalars(
                select(CaseRow.id)
                .join(PORow, PORow.id == CaseRow.po_line_id)
                .where(PORow.supplier_id == supplier_id, CaseRow.active_key.is_not(None))
            )
        )
    )
