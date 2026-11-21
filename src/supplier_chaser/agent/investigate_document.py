import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import CaseRow, MessageRow, SourceFact, SourceFile, SupplierRow
from supplier_chaser.agent.discover import relevant_documents
from supplier_chaser.agent.draft import create_draft
from supplier_chaser.agent.interpret_reply import (
    source_quote,
    supports_date,
    validate_quantity_unit,
)
from supplier_chaser.cases.service import move, reconcile_line
from supplier_chaser.communications.receive import supersede_drafts
from supplier_chaser.config import Settings
from supplier_chaser.domain.documents import DocumentItems
from supplier_chaser.domain.models import Confirmation, Delivery, POLine, State
from supplier_chaser.ingestion.attribution import attribute_items
from supplier_chaser.ingestion.order_coverage import all_order_lines
from supplier_chaser.ingestion.poll import save_claim
from supplier_chaser.ports.llm import LLMDrafter, LLMExtractor
from supplier_chaser.workflow.events import append_event


def validate_document(document: DocumentItems, text: str, orders: list[POLine]) -> DocumentItems:
    if (document.po_number, document.supplier_id) not in {
        (po.po_number, po.supplier_id) for po in orders
    }:
        raise ValueError("Document attribution differs from candidate orders")
    items = []
    for item in document.items:
        quote = source_quote(text, item.evidence_quote)
        if item.part_number not in quote:
            raise ValueError("Item quote lacks the material identifier")
        # Use the same deterministic quantity/date validation as reply extraction.
        from supplier_chaser.agent.interpret_reply import quantities_in_quote

        if item.quantity not in quantities_in_quote(quote):
            raise ValueError("Item quantity lacks evidence")
        if item.promised_date and not supports_date(quote, item.promised_date):
            raise ValueError("Item date lacks evidence")
        if item.item_reference and item.item_reference not in quote:
            raise ValueError("Item reference lacks evidence")
        unit = validate_quantity_unit(item.unit, quote)
        items.append(item.model_copy(update={"evidence_quote": quote, "unit": unit}))
    return document.model_copy(update={"items": items, "complete_order_lines": False})


def validated_document(extractor: LLMExtractor, text: str, orders: list[POLine]) -> DocumentItems:
    context = json.dumps(
        [{"po_number": po.po_number, "supplier_id": po.supplier_id} for po in orders]
    )
    for attempt in range(2):
        try:
            document = extractor.extract_document(
                "Candidate order/supplier identifiers only (not evidence): "
                + context
                + "\nOriginal document:\n"
                + text
            ).model_copy(update={"complete_order_lines": False})
            return validate_document(document, text, orders)
        except (ValueError, TypeError):
            if attempt:
                raise
    raise ValueError("Document extraction failed")


def investigate_document(
    session: Session,
    source_file_id: str,
    projection_hash: str,
    settings: Settings,
    extractor: LLMExtractor,
    drafter: LLMDrafter,
    now: datetime,
) -> None:
    source = session.scalar(
        select(SourceFile).where(SourceFile.id == source_file_id).with_for_update()
    )
    if source is None:
        raise ValueError("Source unavailable")
    locator = f"investigation:{projection_hash}"
    if session.scalar(
        select(SourceFact.id).where(
            SourceFact.source_file_id == source.id, SourceFact.record_locator == locator
        )
    ):
        return
    current = next(
        (
            item
            for item in relevant_documents(session)
            if item[0].id == source.id and item[3] == projection_hash
        ),
        None,
    )
    if current is None:
        return
    _, text, rows, _ = current
    orders = [POLine.model_validate(row.data) for row in rows]
    reviewed_lines = {
        finding["po_line_id"]
        for review in session.scalars(
            select(SourceFact).where(
                SourceFact.source_file_id == source.id, SourceFact.fact_type == "document_review"
            )
        )
        for finding in review.normalized_json.get("line_findings", [])
    }
    result: dict = {
        "status": "error",
        "summary": "Document extraction could not be validated; no supplier claim was accepted.",
        "line_findings": [],
    }
    try:
        document = None
        cached = session.scalars(
            select(SourceFact)
            .where(
                SourceFact.source_file_id == source.id,
                SourceFact.fact_type == "document_analysis",
            )
            .order_by(SourceFact.extracted_at.desc(), SourceFact.id)
        )
        for previous in cached:
            if previous.normalized_json.get("status") not in {
                "complete",
                "ambiguous",
                "unsupported",
            }:
                continue
            try:
                candidate = DocumentItems.model_validate(previous.normalized_json["document"])
                document = validate_document(candidate, text, orders)
                result["extraction_reused_from"] = previous.id
                break
            except (ValueError, TypeError, KeyError):
                continue
        if document is None:
            document = validated_document(extractor, text, orders)
        result["document"] = document.model_dump(mode="json")
        if document.document_type != "confirmation":
            result.update(
                status="unsupported",
                summary="Related reference document found; it is not an order confirmation.",
            )
        else:
            matching_context, incomplete = all_order_lines(session, orders)
            attributions = [
                attribution
                for attribution in attribute_items(document, matching_context)
                if attribution.po_line_id in {po.key for po in orders}
            ]
            attributions = [
                attribution.model_copy(
                    update={
                        "status": "ambiguous",
                        "item_ids": [],
                        "method": "none",
                        "reason": "Order coverage unknown; clarify attribution.",
                    }
                )
                if attribution.po_line_id in incomplete
                else attribution
                for attribution in attributions
            ]
            items = {item.item_id: item for item in document.items}
            summary = []
            for attribution in attributions:
                po = next(po for po in orders if po.key == attribution.po_line_id)
                candidates = [items[key] for key in attribution.candidate_item_ids]
                dates = {item.promised_date for item in candidates}
                promised = next(iter(dates)) if len(dates) == 1 else None
                gap = (
                    (promised - po.requested_delivery).days
                    if promised and po.requested_delivery
                    else None
                )
                finding = {
                    **attribution.model_dump(mode="json"),
                    "quantity": str(po.ordered_qty),
                    "requested_date": po.requested_delivery.isoformat()
                    if po.requested_delivery
                    else None,
                    "promised_date": promised.isoformat() if promised else None,
                    "date_gap_days": gap,
                    "attribution": attribution.status,
                    "supplier_items": [item.model_dump(mode="json") for item in candidates],
                }
                result["line_findings"].append(finding)
                summary.append(
                    f"PO {po.po_number} line {po.line_number}: "
                    f"{len(candidates)} candidate item(s); document date {promised or 'unknown'}, "
                    f"requested {po.requested_delivery}. {attribution.reason}"
                )
                summary.extend(
                    f"Supplier item {item.item_reference or item.item_id}: "
                    f"{item.quantity} {item.unit or 'unit unspecified'}, "
                    f"date {item.promised_date or 'unknown'}."
                    for item in candidates
                )
                if attribution.status == "matched" and po.key not in reviewed_lines:
                    item = items[attribution.item_ids[0]]
                    supplier = session.get(SupplierRow, po.supplier_id)
                    supplier_fact = (
                        session.get(SourceFact, supplier.source_ref) if supplier else None
                    )
                    trusted_file = (
                        session.get(SourceFile, supplier_fact.source_file_id)
                        if supplier_fact
                        else None
                    )
                    # Only the approved supplier directory establishes document attribution.
                    from pathlib import PurePosixPath

                    trusted_root = (
                        str(PurePosixPath(trusted_file.path).parent) if trusted_file else "."
                    )
                    identity_ok = trusted_root != "." and source.path.startswith(trusted_root + "/")
                    risks = list(document.risks)
                    if not identity_ok:
                        risks.append("identity")
                    confirmation = Confirmation(
                        document_id=f"{source.id}:{item.item_id}",
                        po_number=po.po_number,
                        line_number=po.line_number,
                        supplier_id=po.supplier_id,
                        unit=item.unit,
                        deliveries=[
                            Delivery(quantity=item.quantity, promised_date=item.promised_date)
                        ],
                        intent=document.intent,
                        terms="\n".join(filter(None, [document.document_terms, item.terms])),
                        risks=sorted(set(risks)),
                        evidence_quote=item.evidence_quote,
                    )
                    fact = SourceFact(
                        source_file_id=source.id,
                        record_locator=f"{locator}:{po.key}",
                        fact_type="confirmation",
                        normalized_json=confirmation.model_dump(mode="json"),
                        extracted_at=now,
                        confidence="validated_document_item",
                    )
                    session.add(fact)
                    session.flush()
                    save_claim(session, confirmation, now, fact_id=fact.id)
            result.update(
                status="ambiguous"
                if any(a.status != "matched" for a in attributions)
                else "complete",
                summary="\n".join(summary),
                model=extractor.model_version,
            )
            if document.document_terms:
                result["summary"] += (
                    "\nSupplier conditions (not accepted): " + document.document_terms
                )
    except (ValueError, TypeError):
        pass
    session.add(
        SourceFact(
            source_file_id=source.id,
            record_locator=locator,
            fact_type="document_analysis",
            normalized_json=result,
            extracted_at=now,
            confidence="validated_document_analysis",
        )
    )
    session.flush()
    append_event(
        session, source.id, "source.document_investigated", {"status": result["status"]}, now
    )
    for po in orders:
        reconcile_line(session, po.key, settings, now)
        if result["status"] in {"ambiguous", "complete"}:
            cases = list(
                session.scalars(
                    select(CaseRow).where(
                        CaseRow.po_line_id == po.key, CaseRow.active_key.is_not(None)
                    )
                )
            )
            for case in cases:
                held_reply = session.scalar(
                    select(MessageRow.id)
                    .where(
                        MessageRow.case_id == case.id,
                        MessageRow.direction == "inbound",
                        MessageRow.status.in_(["needs_review", "review_queued"]),
                    )
                    .limit(1)
                )
                if held_reply:
                    continue
                if case.state in {State.NEW, State.ESCALATED}:
                    try:
                        with session.begin_nested():
                            # An older draft predates this evidence and would block the new one.
                            supersede_drafts(
                                session,
                                case,
                                now,
                                "Superseded by a draft from new document evidence",
                            )
                            move(
                                session,
                                case,
                                State.INVESTIGATING,
                                now,
                                "Prepare factual clarification from existing document evidence",
                            )
                            create_draft(
                                session, case.id, drafter, settings, now, require_approval=True
                            )
                    except Exception:
                        # Draft generation cannot discard validated source analysis and claims.
                        move(
                            session,
                            case,
                            State.ESCALATED,
                            now,
                            "Document investigated; draft failed. Request a new draft.",
                        )
                        append_event(
                            session,
                            case.id,
                            "message.draft_failed",
                            {"reason": "Draft generation failed; source analysis is preserved."},
                            now,
                        )
