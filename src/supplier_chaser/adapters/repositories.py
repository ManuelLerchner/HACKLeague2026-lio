from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import ClaimRow, PORow, SourceFact, SourceFile
from supplier_chaser.domain.models import Claim, Confirmation, POLine
from supplier_chaser.domain.units import canonical_unit

# Our own material master. Read by exact path so a supplier upload can never stand in for it.
MATERIAL_MASTER_PATH = "soony/master/components.csv"


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
        po = self.session.get(PORow, line_id)
        master_unit = self.master_unit(po.part_number) if po else None
        claims = []
        for r in rows:
            confirmation = Confirmation.model_validate(r.data)
            unit_source = None
            # A bare quantity is read in the material's unit only when master data and our
            # order agree on it; a unit the supplier did state is never replaced.
            if (
                confirmation.unit is None
                and master_unit
                and po
                and canonical_unit(master_unit) == canonical_unit(po.data.get("unit"))
            ):
                confirmation = confirmation.model_copy(update={"unit": canonical_unit(master_unit)})
                unit_source = MATERIAL_MASTER_PATH
            claims.append(
                Claim(
                    id=r.id,
                    evidence_id=r.source_fact_id or r.message_id or r.id,
                    received_at=r.received_at,
                    source_kind="file" if r.source_fact_id else "message",
                    confirmation=confirmation,
                    unit_source=unit_source,
                    **self._buyer_review(r),
                )
            )
        return claims

    def master_unit(self, part_number: str) -> str | None:
        if not hasattr(self, "_master_units"):
            table = self.session.scalar(
                select(SourceFact)
                .join(SourceFile)
                .where(SourceFile.path == MATERIAL_MASTER_PATH, SourceFact.fact_type == "table")
                .order_by(SourceFact.extracted_at.desc())
            )
            rows = table.normalized_json.get("rows", []) if table else []
            self._master_units = {
                row["values"].get("component_id"): row["values"].get("uom") or None for row in rows
            }
        return self._master_units.get(part_number)

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
