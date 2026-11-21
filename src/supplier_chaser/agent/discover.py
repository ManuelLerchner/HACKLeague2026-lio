"""Discover relevant source documents independently of arrival order."""

import hashlib
import json
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import JobRow, PORow, SourceFact, SourceFile
from supplier_chaser.config import Settings
from supplier_chaser.workflow.jobs import enqueue


def mentions(text: str, identifier: str) -> bool:
    return bool(re.search(r"(?<![\w/-])" + re.escape(identifier) + r"(?![\w/-])", text))


def relevant_documents(session: Session) -> list[tuple[SourceFile, str, list[PORow], str]]:
    orders = list(session.scalars(select(PORow)))
    texts: dict[str, list[str]] = {}
    for fact in session.scalars(
        select(SourceFact).where(SourceFact.fact_type == "text").order_by(SourceFact.record_locator)
    ):
        texts.setdefault(fact.source_file_id, []).append(fact.normalized_json["text"])
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
                    [
                        {"data": po.data, "source_fact_id": po.source_fact_id}
                        for po in sorted(matching, key=lambda p: p.id)
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            result.append((source, text, matching, fingerprint))
    return result


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
    for source, _, orders, fingerprint in relevant_documents(session):
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
        for po in orders:
            result.setdefault(po.id, []).append(finding)
    return result


def document_findings(session: Session, line_id: str) -> list[dict]:
    return all_document_findings(session).get(line_id, [])
