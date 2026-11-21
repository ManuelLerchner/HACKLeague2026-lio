from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from supplier_chaser.domain.models import (
    Assessment,
    Claim,
    Discrepancy,
    IssueType,
    POLine,
)
from supplier_chaser.domain.policies import LAUNCH_CUTOFF, priority
from supplier_chaser.domain.units import canonical_unit


def reconcile(
    po: POLine,
    claims: list[Claim],
    now: datetime,
    grace_hours: int = 24,
    cutoff: datetime = LAUNCH_CUTOFF,
) -> Assessment:
    applicable = [
        c
        for c in claims
        if (c.confirmation.po_number, c.confirmation.line_number, c.confirmation.supplier_id)
        == (po.po_number, po.line_number, po.supplier_id)
    ]
    evidence = sorted(c.evidence_id for c in applicable)

    def issue(kind: IssueType, reason: str, claim: Claim | None = None) -> Discrepancy:
        violation = kind in {IssueType.LATE_COMMITMENT, IssueType.QUANTITY_MISMATCH}
        severity, explanation = priority(po, violation, cutoff)
        days = [d.day for d in claim.confirmation.deliveries if d.day] if claim else []
        return Discrepancy(
            po_line_id=po.key,
            issue_type=kind,
            affected_quantity=abs(po.ordered_qty - claim.confirmation.quantity) if claim else None,
            requested_date=po.requested_delivery,
            committed_date=max(days) if days else None,
            evidence_fact_ids=evidence,
            explanation=f"{reason} {explanation}",
            severity=severity,
        )

    if not po.unit or not po.requested_delivery:
        return Assessment(status="UNKNOWN", reason="Order unit or requested date is missing.")
    if not applicable:
        ordered_upper_bound = po.ordered_at
        if not isinstance(ordered_upper_bound, datetime):
            # A date represents an interval; only chase once its latest possible time is past grace.
            ordered_upper_bound = datetime.combine(
                ordered_upper_bound + timedelta(days=1), time.min, ZoneInfo("Europe/Berlin")
            )
        if now < ordered_upper_bound + timedelta(hours=grace_hours):
            return Assessment(status="GRACE", reason="Within confirmation grace period.")
        missing = issue(IssueType.MISSING_CONFIRMATION, "No attributable supplier confirmation.")
        return Assessment(status="DISCREPANCY", issues=[missing], reason=missing.explanation)
    superseded = {x for c in applicable for x in c.confirmation.supersedes}
    active = [c for c in applicable if c.confirmation.document_id not in superseded]
    # Identical repeated snapshots carry evidence without creating competing commitments.
    signatures: dict[str, Claim] = {}
    for claim in sorted(active, key=lambda c: (c.received_at, c.id)):
        signature = claim.confirmation.model_dump_json(
            exclude={"document_id", "supersedes", "evidence_quote", "replaces_previous"}
        )
        signatures[signature] = claim
    active = list(signatures.values())
    if len(active) != 1:
        conflict = issue(
            IssueType.CONTRADICTORY_EVIDENCE,
            "Incompatible confirmations; no explicit supersession establishes precedence.",
        )
        return Assessment(status="DISCREPANCY", issues=[conflict], reason=conflict.explanation)
    latest = active[0]
    confirmation = latest.confirmation
    if (
        confirmation.intent != "confirmed"
        or not confirmation.deliveries
        or canonical_unit(confirmation.unit) != canonical_unit(po.unit)
        or any(d.day is None for d in confirmation.deliveries)
        or set(confirmation.risks) - set(latest.acknowledged_risks)
        or (bool(confirmation.terms.strip()) and not latest.baseline_terms_verified)
    ):
        return Assessment(
            status="UNKNOWN",
            reason="Uncertain intent, unit, date, or policy-sensitive conditions require review.",
            latest_claim=latest,
        )
    issues = []
    if confirmation.quantity != po.ordered_qty:
        issues.append(
            issue(
                IssueType.QUANTITY_MISMATCH, "Confirmed total differs from ordered total.", latest
            )
        )
    if any(d.day and d.day > po.requested_delivery for d in confirmation.deliveries):
        issues.append(
            issue(
                IssueType.LATE_COMMITMENT,
                "A delivery is promised after the requested date.",
                latest,
            )
        )
    elif po.launch_critical and po.lead_time_hours is not None:
        if any(
            d.promised_at and d.promised_at + timedelta(hours=po.lead_time_hours) > cutoff
            for d in confirmation.deliveries
        ):
            issues.append(
                issue(
                    IssueType.LATE_COMMITMENT,
                    "Promised time plus packing lead time exceeds launch cutoff.",
                    latest,
                )
            )
    previous = [c for c in applicable if c.confirmation.document_id in confirmation.supersedes]
    if latest.source_kind == "file" and any(
        (c.confirmation.deliveries, c.confirmation.terms)
        != (confirmation.deliveries, confirmation.terms)
        for c in previous
    ):
        issues.append(
            issue(
                IssueType.CHANGED_COMMITMENT,
                "Supplier amended an earlier commitment; reconfirm the change.",
                latest,
            )
        )
    return Assessment(
        status="DISCREPANCY" if issues else "MATCH",
        issues=issues,
        reason="; ".join(i.explanation for i in issues)
        if issues
        else "Supplier confirmation matches the order. Physical receipt remains unverified.",
        latest_claim=latest,
    )
