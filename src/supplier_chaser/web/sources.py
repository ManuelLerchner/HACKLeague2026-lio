import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from supplier_chaser.adapters.db import JobRow, PORow, SourceFact, SourceFile
from supplier_chaser.clock import Clock
from supplier_chaser.config import Settings
from supplier_chaser.ingestion.mapping import FIELDS, MappingSpec, mapped_records, suggest
from supplier_chaser.ingestion.review import mapping_application
from supplier_chaser.web.evidence import preview_file
from supplier_chaser.workflow.jobs import enqueue


class MappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table_fact_id: str
    mapping: MappingSpec


class ExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_fact_id: str
    po_line_id: str


def register_source_routes(
    app: FastAPI,
    factory: sessionmaker,
    settings: Settings,
    clock: Clock,
    authorized: Callable[[Request], str],
    templates: Jinja2Templates,
    status: Callable[[], dict],
) -> None:
    def source_file(session: Session, identifier: str) -> SourceFile:
        source = session.get(SourceFile, identifier)
        if source is None:
            raise HTTPException(404, "Source file not found")
        return source

    def table_fact(session: Session, identifier: str) -> SourceFact:
        table = session.get(SourceFact, identifier)
        if table is None or table.fact_type != "table":
            raise HTTPException(404, "Source table not found")
        return table

    def validate(table: SourceFact, mapping: MappingSpec) -> list:
        try:
            return mapped_records(table.normalized_json, mapping)
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error)) from error

    @app.get("/sources", response_class=HTMLResponse, dependencies=[Depends(authorized)])
    def source_inbox(request: Request, q: str = "") -> HTMLResponse:
        with factory() as session:
            from supplier_chaser.agent.discover import all_document_findings

            investigations = {
                item["source_id"]: item
                for items in all_document_findings(session).values()
                for item in items
            }
            query = select(SourceFile).order_by(SourceFile.downloaded_at.desc(), SourceFile.path)
            if q:
                query = query.where(SourceFile.path.contains(q, autoescape=True))
            return templates.TemplateResponse(
                request=request,
                name="sources.html",
                context={
                    "status": status(),
                    "files": list(session.scalars(query.limit(500))),
                    "q": q,
                    "investigations": investigations,
                },
            )

    @app.get(
        "/sources/{identifier}", response_class=HTMLResponse, dependencies=[Depends(authorized)]
    )
    def source_page(
        request: Request, identifier: str, kind: Literal["supplier", "po", "confirmation"] = "po"
    ) -> HTMLResponse:
        with factory() as session:
            source = source_file(session, identifier)
            facts = list(
                session.scalars(select(SourceFact).where(SourceFact.source_file_id == identifier))
            )
            tables = [
                {
                    "id": fact.id,
                    "locator": fact.record_locator,
                    "data": fact.normalized_json,
                    "suggestion": suggest(fact.normalized_json["columns"], kind),
                    "applied": mapping_application(session, fact),
                }
                for fact in facts
                if fact.fact_type == "table"
            ]
            po_lines = list(session.scalars(select(PORow).order_by(PORow.id)))
            matching_orders = {
                fact.id: [po for po in po_lines if po.po_number in fact.normalized_json["text"]]
                for fact in facts
                if fact.fact_type == "text"
            }
            return templates.TemplateResponse(
                request=request,
                name="source.html",
                context={
                    "status": status(),
                    "source": source,
                    "tables": tables,
                    "kind": kind,
                    "fields": sorted(FIELDS[kind]),
                    "texts": [fact for fact in facts if fact.fact_type == "text"],
                    "has_imported_orders": bool(po_lines),
                    "matching_orders": matching_orders,
                    "extractions": [fact for fact in facts if fact.fact_type == "confirmation"],
                    "document_analyses": [
                        fact for fact in facts if fact.fact_type == "document_analysis"
                    ],
                    "source_jobs": [
                        job
                        for job in session.scalars(
                            select(JobRow).where(
                                JobRow.job_type.in_(
                                    ["extract_source", "apply_mapping", "investigate_document"]
                                )
                            )
                        )
                        if job.payload_json["payload"].get("source_file_id") == identifier
                        or job.payload_json["payload"].get(
                            "source_fact_id", job.payload_json["payload"].get("table_fact_id")
                        )
                        in {fact.id for fact in facts}
                    ],
                },
            )

    @app.get("/sources/{identifier}/download", dependencies=[Depends(authorized)])
    def download(identifier: str) -> FileResponse:
        with factory() as session:
            source = source_file(session, identifier)
            path = settings.data_dir / "blobs" / source.blob_ref
            if not source.blob_ref or not path.is_file():
                raise HTTPException(404, "Source blob unavailable")
            return FileResponse(
                path, media_type="application/octet-stream", filename=Path(source.path).name
            )

    @app.get("/api/v1/sources/{identifier}/progress", dependencies=[Depends(authorized)])
    def source_progress(identifier: str) -> dict:
        with factory() as session:
            source = source_file(session, identifier)
            facts = list(
                session.scalars(select(SourceFact).where(SourceFact.source_file_id == identifier))
            )
            ids = {fact.id for fact in facts}
            jobs = [
                job
                for job in session.scalars(select(JobRow))
                if (
                    job.payload_json["payload"].get("source_file_id") == identifier
                    or job.payload_json["payload"].get("source_fact_id") in ids
                    or job.payload_json["payload"].get("table_fact_id") in ids
                )
            ]
            revision = hashlib.sha256(
                json.dumps(
                    {
                        "facts": sorted(fact.id for fact in facts),
                        "status": source.parse_status,
                        "jobs": sorted((job.id, job.status) for job in jobs),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            return {
                "pending": any(job.status in {"queued", "running"} for job in jobs),
                "revision": revision,
                "status": "failed"
                if any(job.status == "failed" for job in jobs)
                else source.parse_status,
            }

    @app.get("/sources/{identifier}/preview", dependencies=[Depends(authorized)])
    def preview_original(identifier: str) -> FileResponse:
        with factory() as session:
            source = source_file(session, identifier)
            return preview_file(settings.data_dir / "blobs" / source.blob_ref, source.path)

    @app.post("/api/v1/sources/preview", dependencies=[Depends(authorized)])
    def preview(body: MappingRequest) -> dict:
        with factory() as session:
            rows = validate(table_fact(session, body.table_fact_id), body.mapping)
            return {"valid": True, "row_count": len(rows), "sample": rows[:5]}

    @app.post("/api/v1/sources/extract", status_code=202)
    def extract(body: ExtractionRequest, admin: str = Depends(authorized)) -> dict:
        if settings.llm_provider == "fixture":
            raise HTTPException(409, "Configure a model provider for document extraction")
        with factory.begin() as session:
            fact = session.get(SourceFact, body.source_fact_id)
            po = session.get(PORow, body.po_line_id)
            if fact is None or fact.fact_type != "text" or po is None:
                raise HTTPException(404, "Source text or order line not found")
            text = fact.normalized_json["text"]
            if po.po_number not in text:
                raise HTTPException(422, "Source must identify the selected PO")
            from supplier_chaser.agent.discover import relevant_documents

            document = next(
                (item for item in relevant_documents(session) if item[0].id == fact.source_file_id),
                None,
            )
            if document is None:
                raise HTTPException(409, "This source is no longer the current document version")
            from supplier_chaser.agent.discover import request_document_investigation

            job = request_document_investigation(
                session, fact.source_file_id, clock.now(), retry=True
            )
            return {"job_id": job.id, "status": job.status}

    @app.post("/api/v1/sources/apply", status_code=202)
    def approve(body: MappingRequest, admin: str = Depends(authorized)) -> dict:
        with factory.begin() as session:
            table = table_fact(session, body.table_fact_id)
            source = source_file(session, table.source_file_id)
            if body.mapping.kind == "supplier" and not (
                body.mapping.trust_contacts or source.path in settings.trusted_supplier_paths
            ):
                raise HTTPException(
                    409,
                    "Verify that this is trusted supplier master data before importing contacts",
                )
            previous = mapping_application(session, table)
            if previous and previous.normalized_json["definition"] != body.mapping.model_dump(
                mode="json"
            ):
                raise HTTPException(409, "This source version already has an approved mapping")
            rows = validate(table, body.mapping)
            digest = hashlib.sha256(body.mapping.model_dump_json().encode()).hexdigest()
            job = enqueue(
                session,
                "apply_mapping",
                {
                    "table_fact_id": table.id,
                    "mapping": body.mapping.model_dump(mode="json"),
                    "approved_by": admin,
                },
                f"mapping:{table.id}:{digest}",
                clock.now(),
            )
            return {"job_id": job.id, "status": job.status, "rows": len(rows)}
