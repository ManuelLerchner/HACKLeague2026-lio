"""Discover relevant source documents independently of arrival order."""

import hashlib
import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import JobRow, PORow, SourceFact, SourceFile, uid
from supplier_chaser.config import Settings
from supplier_chaser.workflow.jobs import enqueue

INVESTIGATION_VERSION = "document-investigation-v2"


def mentions(text: str, identifier: str) -> bool:
    return bool(re.search(r"(?<![\w/-])" + re.escape(identifier) + r"(?![\w/-])", text))


def relevant_documents(session: Session) -> list[tuple[SourceFile, str, list[PORow], str]]:
    orders = list(session.scalars(select(PORow)))
    texts: dict[str, list[str]] = {}
    for fact in session.scalars(
        select(SourceFact).where(SourceFact.fact_type == "text").order_by(SourceFact.record_locator)
    ):
        texts.setdefault(fact.source_file_id, []).append(fact.normalized_json["text"])
    retries: dict[str, list[str]] = {}
    for fact in session.scalars(select(SourceFact).where(SourceFact.fact_type == "document_retry")):
        retries.setdefault(fact.source_file_id, []).append(fact.id)
    seen = set()
    result = []
    for source in session.scalars(
        select(SourceFile).order_by(SourceFile.downloaded_at.desc(), SourceFile.id)
    ):
        if source.path in seen:
            continue
        seen.add(source.path)
        if source.deleted or source.id not in texts:
            continue
        text = "\n\n".join(texts[source.id])
        # A material mismatch is evidence to investigate, not a reason to hide the document.
        matching = [po for po in orders if mentions(text, po.po_number)]
        if matching:
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "version": INVESTIGATION_VERSION,
                        "orders": [
                            {"data": po.data, "source_fact_id": po.source_fact_id}
                            for po in sorted(matching, key=lambda p: p.id)
                        ],
                        "retry_requests": sorted(retries.get(source.id, [])),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            result.append((source, text, matching, fingerprint))
    return result


def request_document_investigation(
    session: Session, source_id: str, now: datetime, *, retry: bool = False
) -> JobRow:
    source = session.scalar(select(SourceFile).where(SourceFile.id == source_id).with_for_update())
    if source is None:
        raise ValueError("Source not found")
    document = next((item for item in relevant_documents(session) if item[0].id == source_id), None)
    if document is None:
        raise ValueError("No current source text identifies an imported order")
    _, _, orders, fingerprint = document
    key = f"investigate-document:{source_id}:{fingerprint}"
    existing = session.scalar(select(JobRow).where(JobRow.idempotency_key == key))
    if existing and existing.status in {"queued", "running"}:
        return existing
    analysis = session.scalar(
        select(SourceFact).where(
            SourceFact.source_file_id == source_id,
            SourceFact.record_locator == f"investigation:{fingerprint}",
        )
    )
    if retry and (existing or analysis):
        session.add(
            SourceFact(
                source_file_id=source_id,
                record_locator=f"retry:{uid()}",
                fact_type="document_retry",
                normalized_json={"previous_projection": fingerprint},
                extracted_at=now,
                confidence="explicit_retry_request",
            )
        )
        session.flush()
        document = next(item for item in relevant_documents(session) if item[0].id == source_id)
        _, _, orders, fingerprint = document
        key = f"investigate-document:{source_id}:{fingerprint}"
    return enqueue(
        session,
        "investigate_document",
        {
            "source_file_id": source_id,
            "po_line_ids": [po.id for po in orders],
            "projection_hash": fingerprint,
        },
        key,
        now,
    )


def discover_documents(session: Session, settings: Settings, now: datetime) -> int:
    if not settings.auto_investigate_documents or settings.llm_provider == "fixture":
        return 0
    count = 0
    for source, _, orders, fingerprint in relevant_documents(session):
        enqueue(
            session,
            "investigate_document",
            {
                "source_file_id": source.id,
                "po_line_ids": [po.id for po in orders],
                "projection_hash": fingerprint,
            },
            f"investigate-document:{source.id}:{fingerprint}",
            now,
        )
        count += 1
    return count


def all_document_findings(session: Session) -> dict[str, list[dict]]:
    analyses = {
        (fact.source_file_id, fact.record_locator): fact.normalized_json
        for fact in session.scalars(
            select(SourceFact).where(SourceFact.fact_type == "document_analysis")
        )
    }
    jobs = {
        job.idempotency_key: job
        for job in session.scalars(select(JobRow).where(JobRow.job_type == "investigate_document"))
    }
    result: dict[str, list[dict]] = {}
    for source, text, orders, fingerprint in relevant_documents(session):
        job = jobs.get(f"investigate-document:{source.id}:{fingerprint}")
        if job and job.status in {"queued", "running"}:
            fallback = {
                "status": "pending",
                "summary": "Document investigation is queued or running.",
            }
        elif job and job.status == "failed":
            fallback = {
                "status": "error",
                "summary": "Investigation failed. Open the worker queue to inspect the source.",
            }
        else:
            fallback = {
                "status": "needs_review",
                "summary": "No result or queued work. Open the source to investigate.",
            }
        finding = {
            "source_id": source.id,
            "path": source.path,
            **analyses.get(
                (source.id, f"investigation:{fingerprint}"),
                fallback,
            ),
        }
        from supplier_chaser.ingestion.document_review import review_findings

        reviewed = review_findings(session, source.id, fingerprint)
        if reviewed:
            finding.update(reviewed)
        for po in orders:
            per_line = dict(finding)
            lines = [
                line for line in finding.get("line_findings", []) if line["po_line_id"] == po.id
            ]
            if lines:
                per_line.update(line_status(lines))
            elif per_line["status"] == "error" and not mentions(text, po.part_number):
                # A failed extraction says nothing about materials the document never names.
                per_line["status"] = "not_covered"
            result.setdefault(po.id, []).append(per_line)
    return result


def line_status(lines: list[dict]) -> dict:
    """A line's status comes from its own attribution, never from sibling lines."""
    attributions = {line["attribution"] for line in lines}
    if attributions == {"matched"}:
        status = "complete"
    elif attributions == {"unmatched"}:
        # The document has no item for this line: it neither confirms nor contradicts it.
        status = "not_covered"
    else:
        status = "ambiguous"
    return {"status": status, "summary": "\n".join(line["reason"] for line in lines)}


def document_findings(session: Session, line_id: str) -> list[dict]:
    return all_document_findings(session).get(line_id, [])
