import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import PORow, SourceFact, SourceFile
from supplier_chaser.agent.interpret_reply import extract_validated
from supplier_chaser.domain.models import POLine
from supplier_chaser.ingestion.poll import save_claim
from supplier_chaser.ports.llm import LLMExtractor
from supplier_chaser.workflow.events import append_event
from supplier_chaser.workflow.jobs import enqueue


def extract_source(
    session: Session,
    source_fact_id: str,
    line_id: str,
    extractor: LLMExtractor,
    now: datetime,
    requested_by: str,
) -> None:
    fact = session.get(SourceFact, source_fact_id)
    row = session.get(PORow, line_id)
    if fact is None or fact.fact_type != "text" or row is None:
        raise ValueError("A source text and existing order line are required")
    source = session.scalar(
        select(SourceFile).where(SourceFile.id == fact.source_file_id).with_for_update()
    )
    assert source is not None
    locator = f"{fact.record_locator}:extraction:{line_id}"
    if session.scalar(
        select(SourceFact).where(
            SourceFact.source_file_id == source.id, SourceFact.record_locator == locator
        )
    ):
        return
    po = POLine.model_validate(row.data)
    original = fact.normalized_json["text"]
    if po.po_number not in original or po.part_number not in original:
        raise ValueError("Document does not identify the selected order and material")
    context = json.dumps(
        {"po_number": po.po_number, "line_number": po.line_number, "supplier_id": po.supplier_id}
    )
    request = (
        "The operator linked this document to the following existing order identifiers. "
        "Use these identifiers for attribution, never as evidence of quantity or date. "
        f"Identifiers: {context}\nQuote only from the original document below.\n\n{original}"
    )
    confirmation = extract_validated(extractor, request)
    if confirmation.evidence_quote not in original:
        raise ValueError("Evidence quote must come from the original document")
    if (confirmation.po_number, confirmation.line_number, confirmation.supplier_id) != (
        po.po_number,
        po.line_number,
        po.supplier_id,
    ):
        raise ValueError("Extracted identifiers differ from the selected order")
    extracted = SourceFact(
        source_file_id=source.id,
        record_locator=locator,
        fact_type="confirmation",
        normalized_json=confirmation.model_dump(mode="json"),
        extracted_at=now,
        confidence="model_with_operator_attribution",
    )
    session.add(extracted)
    session.flush()
    save_claim(session, confirmation, now, fact_id=extracted.id)
    append_event(
        session,
        source.id,
        "commitment.claim_extracted",
        {
            "fact_id": extracted.id,
            "source_fact_id": fact.id,
            "po_line_id": line_id,
            "model": extractor.model_version,
            "requested_by": requested_by,
        },
        now,
    )
    enqueue(session, "reconcile", {"po_line_id": line_id}, f"source-extraction:{extracted.id}", now)
