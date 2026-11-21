from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import ClaimRow, PORow, SourceFact
from supplier_chaser.domain.models import Claim, Confirmation, POLine


class CommitmentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_po_line(self, line_id: str) -> POLine:
        row = self.session.get(PORow, line_id)
        if row is None:
            raise LookupError("Purchase order line not found")
        return POLine.model_validate(row.data)

    def get_claims(self, line_id: str) -> list[Claim]:
        rows = self.session.scalars(select(ClaimRow).where(ClaimRow.po_line_id == line_id))
        return [
            Claim(
                id=r.id,
                evidence_id=r.source_fact_id or r.message_id or r.id,
                received_at=r.received_at,
                source_kind="file" if r.source_fact_id else "message",
                confirmation=Confirmation.model_validate(r.data),
                **self._buyer_review(r),
            )
            for r in rows
        ]

    def _buyer_review(self, row: ClaimRow) -> dict:
        """Review outcomes count only for the exact confirmation an authenticated buyer saw."""
        fact = self.session.get(SourceFact, row.source_fact_id) if row.source_fact_id else None
        if not (
            fact
            and fact.fact_type == "reviewed_confirmation"
            and fact.normalized_json.get("authenticated_by")
            and fact.normalized_json.get("confirmation") == row.data
        ):
            return {}
        return {
            "baseline_terms_verified": bool(fact.normalized_json.get("baseline_terms_verified")),
            "acknowledged_risks": fact.normalized_json.get("acknowledged_risks", []),
        }
