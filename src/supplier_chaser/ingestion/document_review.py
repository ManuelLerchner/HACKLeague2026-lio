"""Audited buyer interpretation, separate from supplier and model claims."""

import hashlib
import json
import re
from datetime import datetime
from pathlib import PurePosixPath

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import (
    ClaimRow,
    JobRow,
    PORow,
    SourceFact,
    SourceFile,
    SupplierRow,
)
from supplier_chaser.agent.interpret_reply import source_quote
from supplier_chaser.domain.documents import DocumentItems
from supplier_chaser.domain.models import Confirmation, Delivery, Value
from supplier_chaser.ingestion.poll import save_claim
from supplier_chaser.workflow.jobs import enqueue


class EvidenceReference(Value):
    fact_id: str
    quote: str = Field(min_length=1)


class ClaimReview(Value):
    reviewer: str = Field(min_length=2, max_length=100)
    note: str = Field(min_length=10, max_length=2000)
    unit: str | None = None
    unit_evidence: EvidenceReference | None = None
    baseline_terms_evidence: EvidenceReference | None = None


class ItemReview(ClaimReview):
    item_id: str
    po_line_id: str
    attribution_evidence: EvidenceReference
    part_alias_evidence: EvidenceReference | None = None


class DocumentReview(Value):
    analysis_id: str
    items: list[ItemReview] = Field(min_length=1)


def evidence_text(session: Session, reference: EvidenceReference) -> tuple[SourceFact, str]:
    fact = session.get(SourceFact, reference.fact_id)
    if not fact or fact.fact_type not in {"text", "table", "po", "supplier"}:
        raise ValueError("Choose an original text, table, order or supplier evidence fact")
    text = fact.normalized_json.get("text") or json.dumps(fact.normalized_json, ensure_ascii=False)
    return fact, source_quote(text, reference.quote)


def contains(text: str, value: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text, re.IGNORECASE))


def supplier_root(session: Session, supplier_id: str) -> str | None:
    supplier = session.get(SupplierRow, supplier_id)
    fact = session.get(SourceFact, supplier.source_ref) if supplier and supplier.verified else None
    source = session.get(SourceFile, fact.source_file_id) if fact else None
    root = str(PurePosixPath(source.path).parent) if source else "."
    return root if root != "." else None


def reviewed_values(
    session: Session, confirmation: Confirmation, review: ClaimReview, source_id: str | None
) -> tuple[Confirmation, bool]:
    unit = confirmation.unit
    if review.unit:
        if unit and unit != review.unit:
            raise ValueError("A review cannot replace an explicitly stated different unit")
        if not review.unit_evidence:
            raise ValueError("Unit review requires an original evidence quote")
        _, quote = evidence_text(session, review.unit_evidence)
        po = session.get(PORow, f"{confirmation.po_number}/{confirmation.line_number}")
        if not po or not contains(quote, review.unit) or not contains(quote, po.part_number):
            raise ValueError(
                "Unit evidence must identify this order material and the selected unit"
            )
        unit = review.unit
    baseline = False
    if review.baseline_terms_evidence:
        fact, quote = evidence_text(session, review.baseline_terms_evidence)
        if fact.source_file_id == source_id:
            raise ValueError(
                "Baseline terms need independent contract evidence, not this confirmation"
            )
        baseline_source = session.get(SourceFile, fact.source_file_id)
        root = supplier_root(session, confirmation.supplier_id)
        if not baseline_source or not root or not baseline_source.path.startswith(root + "/"):
            raise ValueError(
                "Baseline contract must belong to the verified supplier evidence directory"
            )
        normalized = " ".join(quote.split()).casefold()
        terms = [
            " ".join(term.split()).casefold()
            for term in confirmation.terms.splitlines()
            if term.strip()
        ]
        if not terms or any(term not in normalized for term in terms):
            raise ValueError("Quote the existing baseline supporting every stated term")
        baseline = True
    # Risk flags remain immutable, even when an existing baseline is evidenced.
    return confirmation.model_copy(update={"unit": unit}), baseline


def persist_claim(
    session: Session,
    confirmation: Confirmation,
    review: ClaimReview,
    source_id: str,
    actor: str,
    now: datetime,
    key: str,
    baseline: bool,
) -> SourceFact:
    existing = session.scalar(select(SourceFact).where(SourceFact.record_locator == key))
    if existing:
        return existing
    fact = SourceFact(
        source_file_id=source_id,
        record_locator=key,
        fact_type="reviewed_confirmation",
        normalized_json={
            "confirmation": confirmation.model_dump(mode="json"),
            "review": review.model_dump(mode="json"),
            "authenticated_by": actor,
            "baseline_terms_verified": baseline,
        },
        extracted_at=now,
        confidence="buyer_review",
    )
    session.add(fact)
    session.flush()
    save_claim(session, confirmation, now, fact_id=fact.id)
    return fact


def review_claim(
    session: Session, claim_id: str, review: ClaimReview, actor: str, now: datetime
) -> JobRow:
    claim = session.get(ClaimRow, claim_id)
    if not claim:
        raise LookupError("Claim not found")
    original = Confirmation.model_validate(claim.data)
    origin = session.get(SourceFact, claim.source_fact_id) if claim.source_fact_id else None
    source_id = origin.source_file_id if origin else None
    confirmation, baseline = reviewed_values(session, original, review, source_id)
    if not review.unit and not review.baseline_terms_evidence:
        raise ValueError("Provide unit evidence or baseline terms evidence")
    evidence = review.unit_evidence or review.baseline_terms_evidence
    assert evidence
    anchor, _ = evidence_text(session, evidence)
    key = (
        "claim-review:" + hashlib.sha256((claim.id + review.model_dump_json()).encode()).hexdigest()
    )
    confirmation = confirmation.model_copy(
        update={
            "document_id": key,
            "supersedes": [original.document_id],
        }
    )
    fact = persist_claim(
        session, confirmation, review, source_id or anchor.source_file_id, actor, now, key, baseline
    )
    job = enqueue(session, "reconcile", {"po_line_id": claim.po_line_id}, f"review:{fact.id}", now)
    return job


def review_document(
    session: Session, review: DocumentReview, actor: str, now: datetime
) -> list[JobRow]:
    from supplier_chaser.agent.discover import relevant_documents

    analysis = session.get(SourceFact, review.analysis_id)
    if not analysis or analysis.fact_type != "document_analysis":
        raise LookupError("Document analysis not found")
    current = next(
        (d for d in relevant_documents(session) if d[0].id == analysis.source_file_id), None
    )
    if not current or analysis.record_locator != f"investigation:{current[3]}":
        raise ValueError("Review the current source and order version")
    document = DocumentItems.model_validate(analysis.normalized_json.get("document"))
    if document.document_type != "confirmation":
        raise ValueError("Only a supplier confirmation can create reviewed commitments")
    if len({item.item_id for item in review.items}) != len(review.items):
        raise ValueError("Each document item can be assigned once")
    if len({item.po_line_id for item in review.items}) != len(review.items):
        raise ValueError(
            "Review one item per order line; split deliveries require explicit extraction"
        )
    key = "document-review:" + hashlib.sha256(review.model_dump_json().encode()).hexdigest()
    old = session.scalar(select(SourceFact).where(SourceFact.record_locator == key))
    if old:
        return [
            enqueue(
                session,
                "reconcile",
                {"po_line_id": item.po_line_id},
                f"review:{old.id}:{item.po_line_id}",
                now,
            )
            for item in review.items
        ]
    previous_reviews = list(
        session.scalars(
            select(SourceFact).where(
                SourceFact.source_file_id == analysis.source_file_id,
                SourceFact.fact_type == "document_review",
            )
        )
    )
    assignments = {
        item["item_id"]: item["po_line_id"]
        for previous in previous_reviews
        for item in previous.normalized_json["request"]["items"]
    }
    findings = []
    for item_review in review.items:
        if (
            item_review.item_id in assignments
            and assignments[item_review.item_id] != item_review.po_line_id
        ):
            raise ValueError("This source item already has a different reviewed order assignment")
        item = next((i for i in document.items if i.item_id == item_review.item_id), None)
        po = session.get(PORow, item_review.po_line_id)
        if (
            not item
            or not po
            or (po.po_number, po.supplier_id) != (document.po_number, document.supplier_id)
        ):
            raise ValueError("Item and order must belong to the same supplier and PO")
        _, attribution = evidence_text(session, item_review.attribution_evidence)
        if not contains(attribution, item.item_reference or item.part_number):
            raise ValueError("Attribution evidence must identify the supplier item")
        if item.part_number != po.part_number:
            if not item_review.part_alias_evidence:
                raise ValueError("Different part identifiers require explicit alias evidence")
            _, alias = evidence_text(session, item_review.part_alias_evidence)
            if not all(contains(alias, part) for part in [item.part_number, po.part_number]):
                raise ValueError("Alias evidence must identify both part identifiers")
        prior = []
        for claim in session.scalars(select(ClaimRow).where(ClaimRow.po_line_id == po.id)):
            fact = session.get(SourceFact, claim.source_fact_id) if claim.source_fact_id else None
            if fact and fact.source_file_id == analysis.source_file_id:
                prior.append(claim.data["document_id"])
        source = session.get(SourceFile, analysis.source_file_id)
        root = supplier_root(session, po.supplier_id)
        risks = list(document.risks)
        if not source or not root or not source.path.startswith(root + "/"):
            risks.append("identity")
        confirmation = Confirmation(
            document_id=f"{key}:{item.item_id}",
            po_number=po.po_number,
            line_number=po.line_number,
            supplier_id=po.supplier_id,
            unit=item.unit,
            deliveries=[Delivery(quantity=item.quantity, promised_date=item.promised_date)],
            terms="\n".join(dict.fromkeys(filter(None, [document.document_terms, item.terms]))),
            intent=document.intent,
            risks=sorted(set(risks)),
            evidence_quote=item.evidence_quote,
            supersedes=prior,
        )
        confirmation, baseline = reviewed_values(
            session, confirmation, item_review, analysis.source_file_id
        )
        persist_claim(
            session,
            confirmation,
            item_review,
            analysis.source_file_id,
            actor,
            now,
            f"{key}:{item.item_id}",
            baseline,
        )
        findings.append(
            {
                "po_line_id": po.id,
                "status": "matched",
                "attribution": "matched",
                "reason": "Buyer reviewed this exact document item and order attribution.",
                "quantity": str(po.data["ordered_qty"]),
                "requested_date": po.data.get("requested_delivery"),
                "promised_date": item.promised_date.isoformat() if item.promised_date else None,
                "date_gap_days": None,
                "supplier_items": [item.model_dump(mode="json")],
            }
        )
    fact = SourceFact(
        source_file_id=analysis.source_file_id,
        record_locator=key,
        fact_type="document_review",
        normalized_json={
            "projection_hash": current[3],
            "analysis_id": analysis.id,
            "request": review.model_dump(mode="json"),
            "authenticated_by": actor,
            "line_findings": findings,
        },
        extracted_at=now,
        confidence="buyer_review",
    )
    session.add(fact)
    session.flush()
    return [
        enqueue(
            session,
            "reconcile",
            {"po_line_id": item.po_line_id},
            f"review:{fact.id}:{item.po_line_id}",
            now,
        )
        for item in review.items
    ]


def review_findings(session: Session, source_id: str, projection_hash: str) -> dict | None:
    reviews = [
        r
        for r in session.scalars(
            select(SourceFact)
            .where(
                SourceFact.source_file_id == source_id, SourceFact.fact_type == "document_review"
            )
            .order_by(SourceFact.extracted_at, SourceFact.id)
        )
        if r.normalized_json.get("projection_hash") == projection_hash
    ]
    if not reviews:
        return None
    analysis = session.get(SourceFact, reviews[-1].normalized_json["analysis_id"])
    if not analysis:
        return None
    findings = {f["po_line_id"]: f for f in analysis.normalized_json.get("line_findings", [])}
    for review in reviews:
        findings.update({f["po_line_id"]: f for f in review.normalized_json["line_findings"]})
    return {
        **analysis.normalized_json,
        "line_findings": list(findings.values()),
        "status": "complete"
        if all(f["attribution"] == "matched" for f in findings.values())
        else "ambiguous",
        "summary": "\n".join(f"{f['po_line_id']}: {f['reason']}" for f in findings.values()),
        "reviewed_line_ids": [
            f["po_line_id"] for r in reviews for f in r.normalized_json["line_findings"]
        ],
    }
