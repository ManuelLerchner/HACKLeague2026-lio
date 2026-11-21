from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from supplier_chaser.adapters.db import PORow, SourceFact, SourceFile
from supplier_chaser.clock import Clock
from supplier_chaser.ingestion.document_review import (
    ClaimReview,
    DocumentReview,
    review_claim,
    review_document,
)


def register_document_review_routes(
    app: FastAPI,
    factory: sessionmaker,
    clock: Clock,
    authorized: Callable,
    templates: Jinja2Templates,
    status: Callable,
) -> None:
    @app.get("/api/v1/review-evidence", dependencies=[Depends(authorized)])
    def evidence_choices(q: str = "") -> list[dict]:
        with factory() as session:
            rows = session.execute(
                select(SourceFact, SourceFile)
                .join(SourceFile)
                .where(
                    SourceFact.fact_type.in_(["text", "table", "po", "supplier"]),
                    SourceFile.path.contains(q, autoescape=True),
                )
                .order_by(SourceFile.path, SourceFact.record_locator)
                .limit(100)
            ).all()
            return [
                {"id": f.id, "path": s.path, "locator": f.record_locator, "fact_type": f.fact_type}
                for f, s in rows
            ]

    @app.get("/sources/{source_id}/review", dependencies=[Depends(authorized)])
    def review_page(request: Request, source_id: str) -> HTMLResponse:
        from supplier_chaser.agent.discover import relevant_documents

        with factory() as session:
            current = next((d for d in relevant_documents(session) if d[0].id == source_id), None)
            if not current:
                raise HTTPException(404, "Current source not found")
            analysis = session.scalar(
                select(SourceFact).where(
                    SourceFact.source_file_id == source_id,
                    SourceFact.record_locator == f"investigation:{current[3]}",
                )
            )
            if not analysis or not analysis.normalized_json.get("document"):
                raise HTTPException(409, "Investigate this document before reviewing its items")
            document = analysis.normalized_json["document"]
            orders = list(
                session.scalars(
                    select(PORow).where(
                        PORow.po_number == document["po_number"],
                        PORow.supplier_id == document["supplier_id"],
                    )
                )
            )
            return templates.TemplateResponse(
                request=request,
                name="document_review.html",
                context={
                    "status": status(),
                    "source": current[0],
                    "analysis": analysis,
                    "document": document,
                    "orders": orders,
                },
            )

    @app.post("/api/v1/documents/review", status_code=202)
    def document_review(body: DocumentReview, actor: str = Depends(authorized)) -> dict:
        with factory.begin() as session:
            try:
                jobs = review_document(session, body, actor, clock.now())
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return {"job_ids": [j.id for j in jobs], "status": "queued"}

    @app.post("/api/v1/claims/{claim_id}/review", status_code=202)
    def claim_review(claim_id: str, body: ClaimReview, actor: str = Depends(authorized)) -> dict:
        with factory.begin() as session:
            try:
                job = review_claim(session, claim_id, body, actor, clock.now())
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return {"job_id": job.id, "status": job.status}
