import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from supplier_chaser.adapters.db import (
    ApprovalRow,
    CaseEvent,
    CaseRow,
    JobRow,
    MessageRow,
    PORow,
    SourceFact,
    SourceFile,
    SupplierRow,
    SystemRow,
    make_engine,
    sessions,
)
from supplier_chaser.cases.service import move, record_event
from supplier_chaser.clock import Clock, SystemClock
from supplier_chaser.config import Settings
from supplier_chaser.domain.models import State
from supplier_chaser.web.document_reviews import register_document_review_routes
from supplier_chaser.web.evidence import preview_file
from supplier_chaser.web.job_retries import register_retry_routes
from supplier_chaser.web.sources import register_source_routes
from supplier_chaser.workflow.jobs import enqueue
from supplier_chaser.workflow.retry import RETRYABLE_JOB_TYPES, can_retry_extraction

ROOT = Path(__file__).parent


class DraftEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str | None = Field(default=None, min_length=20, max_length=10000)


class Escalation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=1000)


class ReplyReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str
    decision: Literal["verify", "reject"]
    reviewer: str = Field(min_length=2, max_length=100)
    note: str = Field(min_length=10, max_length=2000)
    identity_verified: bool = False


def serialize_case(session: Session, case: CaseRow, findings: list[dict] | None = None) -> dict:
    po = session.get(PORow, case.po_line_id)
    supplier = session.get(SupplierRow, po.supplier_id) if po else None
    from supplier_chaser.agent.discover import document_findings

    findings = document_findings(session, case.po_line_id) if findings is None else findings
    last = session.scalar(
        select(MessageRow)
        .where(
            MessageRow.case_id == case.id,
            MessageRow.direction == "outbound",
            MessageRow.status == "sent",
        )
        .order_by(MessageRow.sent_or_received_at.desc())
        .limit(1)
    )
    jobs = [
        j
        for j in session.scalars(
            select(JobRow).where(JobRow.status.in_(["queued", "running", "failed"]))
        )
        if j.payload_json["payload"].get("case_id") == case.id
        or j.payload_json["payload"].get("po_line_id") == case.po_line_id
    ]
    pending = any(j.status in {"queued", "running"} for j in jobs) or any(
        item["status"] == "pending" for item in findings
    )
    held = session.scalar(
        select(MessageRow.id)
        .where(
            MessageRow.case_id == case.id,
            MessageRow.direction == "inbound",
            MessageRow.status.in_(["needs_review", "review_queued"]),
        )
        .limit(1)
    )
    if case.state in {State.RESOLVED, State.CANCELLED}:
        group, action = "Completed", "View evidence"
    elif held and not any(
        j.job_type == "review_reply" and j.status in {"queued", "running"} for j in jobs
    ):
        group, action = "Needs your action", "Review supplier reply"
    elif pending:
        group, action = "In progress", "Worker action queued"
    elif any(j.status == "failed" for j in jobs) or any(
        item["status"] == "error" for item in findings
    ):
        group, action = "Needs your action", "Review failed action"
    elif any(item["status"] == "needs_review" for item in findings):
        group, action = "Needs your action", "Investigate source evidence"
    elif case.state == State.AWAITING_REPLY:
        group, action = "Waiting for supplier", "Await reply"
    else:
        group = "Needs your action"
        action = (
            "Draft supplier question"
            if case.state in {State.NEW, State.INVESTIGATING}
            else "Review and send draft"
            if case.state in {State.DRAFT_READY, State.NEEDS_APPROVAL}
            else "Review blocker"
        )
    return {
        "document_findings": findings,
        "action_group": group,
        "next_action": action,
        "pending_work": pending,
        "held_reply": bool(held),
        "jobs": [{"type": j.job_type, "status": j.status, "error": j.last_error} for j in jobs],
        "id": case.id,
        "state": case.state,
        "priority": case.priority,
        "issue_type": case.issue_type,
        "supplier": supplier.name if supplier else "Unknown supplier",
        "supplier_id": po.supplier_id if po else None,
        "supplier_email": supplier.email if supplier else None,
        "po": po.data if po else {},
        "why_open": case.evidence_summary.get("reason"),
        "assessment": case.evidence_summary,
        "owner": case.owner,
        "next_follow_up_at": case.next_action_at,
        "updated_at": case.updated_at,
        "last_contact": last.sent_or_received_at if last else None,
    }


def system_status(session: Session, settings: Settings, clock: Clock) -> dict:
    statuses: dict = {
        r.key: {**r.value, "updated_at": r.updated_at} for r in session.scalars(select(SystemRow))
    }
    statuses["mode"] = (
        "fixture"
        if settings.mail_mode == "fixture"
        else ("dry_run" if settings.dry_run or not settings.outbound_mail_enabled else "live")
    )
    statuses["portal_mode"] = settings.portal_mode
    statuses["llm_provider"] = settings.llm_provider
    heartbeat = session.get(SystemRow, "worker")
    statuses["worker_healthy"] = bool(
        heartbeat and heartbeat.updated_at > clock.now() - timedelta(seconds=120)
    )
    statuses["jobs"] = dict(
        session.execute(select(JobRow.status, func.count()).group_by(JobRow.status)).all()
    )
    statuses["unmatched_messages"] = session.scalar(
        select(func.count()).select_from(MessageRow).where(MessageRow.status == "needs_review")
    )
    return statuses


def create_app(
    settings: Settings | None = None,
    factory: sessionmaker | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    settings = settings or Settings()
    factory = factory or sessions(make_engine(settings.database_url))
    clock = clock or SystemClock()
    app = FastAPI(title="Supplier Chaser", docs_url=None, redoc_url=None, openapi_url=None)
    secret = settings.web_session_secret.get_secret_value() or secrets.token_urlsafe(32)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        same_site="strict",
        https_only=settings.cookie_secure,
        max_age=8 * 3600,
    )
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")

    def localtime(value: datetime | str | None) -> str:
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return (
            value.astimezone(ZoneInfo(settings.timezone)).strftime("%d %b · %H:%M")
            if value
            else "—"
        )

    templates.env.filters["localtime"] = localtime

    def authorized(request: Request) -> str:
        token = settings.admin_token.get_secret_value()
        if not token:
            raise HTTPException(503, "Set ADMIN_TOKEN in the private environment to enable access")
        bearer = request.headers.get("authorization", "")
        if bearer.startswith("Bearer ") and secrets.compare_digest(bearer[7:], token):
            return "admin"
        if request.session.get("admin"):
            if request.method not in {"GET", "HEAD"}:
                csrf = request.headers.get("x-csrf-token", "")
                if not csrf or not secrets.compare_digest(csrf, request.session.get("csrf", "")):
                    raise HTTPException(403, "CSRF token required")
            return "admin"
        raise HTTPException(401, "Admin authentication required")

    def get_case(session: Session, case_id: str, lock: bool = False) -> CaseRow:
        query = select(CaseRow).where(CaseRow.id == case_id)
        if lock:
            query = query.with_for_update()
        case = session.scalar(query)
        if case is None:
            raise HTTPException(404, "Case not found")
        return case

    @app.middleware("http")
    async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'"
            ),
        )
        return response

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "Database unavailable; check migrations and connection"},
        )

    @app.get("/health")
    def health() -> dict:
        with factory() as session:
            session.execute(text("SELECT 1"))
            session.execute(select(CaseRow.id).limit(1))
            status = system_status(session, settings, clock)
        return {"status": "ok", "database": "ready", "worker_healthy": status["worker_healthy"]}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Response:
        return templates.TemplateResponse(request=request, name="login.html", context={})

    @app.post("/login")
    def login(request: Request, token: Annotated[str, Form()]) -> Response:
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cross-origin login rejected")
        configured = settings.admin_token.get_secret_value()
        if not configured or not secrets.compare_digest(token, configured):
            raise HTTPException(401, "Invalid admin token")
        request.session.clear()
        request.session.update({"admin": True, "csrf": secrets.token_urlsafe(32)})
        return RedirectResponse("/", status_code=303)

    @app.get("/api/v1/cases", dependencies=[Depends(authorized)])
    def list_cases(
        state: State | None = None, priority: str | None = None, supplier_id: str | None = None
    ) -> list[dict]:
        with factory() as session:
            query = select(CaseRow).join(PORow, CaseRow.po_line_id == PORow.id)
            if state:
                query = query.where(CaseRow.state == state)
            if priority:
                if priority not in {"P0", "P1", "P2", "P3"}:
                    raise HTTPException(422, "Invalid priority")
                query = query.where(CaseRow.priority == priority)
            if supplier_id:
                query = query.where(PORow.supplier_id == supplier_id)
            from supplier_chaser.agent.discover import all_document_findings

            findings = all_document_findings(session)
            result = [
                serialize_case(session, c, findings.get(c.po_line_id, []))
                for c in session.scalars(query.order_by(CaseRow.priority, CaseRow.created_at))
            ]
            groups = {
                "Needs your action": 0,
                "In progress": 1,
                "Waiting for supplier": 2,
                "Completed": 3,
            }
            return sorted(
                result,
                key=lambda c: (groups[c["action_group"]], c["priority"], c["updated_at"], c["id"]),
            )

    @app.get("/api/v1/cases/{case_id}", dependencies=[Depends(authorized)])
    def detail(case_id: str) -> dict:
        with factory() as session:
            case = get_case(session, case_id)
            result = serialize_case(session, case)
            messages = session.scalars(
                select(MessageRow)
                .where(MessageRow.case_id == case.id)
                .order_by(MessageRow.sent_or_received_at, MessageRow.id)
            )
            result["messages"] = [
                {
                    "id": m.id,
                    "direction": m.direction,
                    "sender": m.sender,
                    "recipients": m.recipients,
                    "subject": m.subject,
                    "body": m.body,
                    "status": m.status,
                    "at": m.sent_or_received_at,
                    "extraction": m.metadata_json.get("extraction"),
                    "extraction_error": m.metadata_json.get("extraction_error"),
                    "can_retry_extraction": can_retry_extraction(m),
                    "review": m.metadata_json.get("buyer_review"),
                    "block_reasons": m.metadata_json.get("block_reasons", []),
                    "retry_after": m.metadata_json.get("scheduled_send_at"),
                    "can_retry_send": (
                        m.status == "blocked"
                        and len(m.metadata_json.get("block_reasons", [])) == 1
                        and m.metadata_json["block_reasons"][0].startswith(
                            "Supplier send limit reached"
                        )
                    ),
                }
                for m in messages
            ]
            result["evidence"] = [
                dict(
                    id=f.id,
                    locator=f.record_locator,
                    path=s.path,
                    checksum=s.checksum,
                    at=s.downloaded_at,
                    data=f.normalized_json,
                )
                for f, s in session.execute(
                    select(SourceFact, SourceFile)
                    .join(SourceFile)
                    .where(SourceFact.fact_type.in_(["po", "confirmation", "text"]))
                )
                if (
                    f.normalized_json.get("po_number") == result["po"].get("po_number")
                    and str(f.normalized_json.get("line_number"))
                    == str(result["po"].get("line_number"))
                )
                or f.id
                in [c.get("evidence_id") for c in [case.evidence_summary.get("latest_claim") or {}]]
            ]
            return result

    @app.get("/api/v1/cases/{case_id}/events", dependencies=[Depends(authorized)])
    def events(case_id: str) -> list[dict]:
        with factory() as session:
            get_case(session, case_id)
            return [
                e.payload_json
                for e in session.scalars(
                    select(CaseEvent)
                    .where(CaseEvent.case_id == case_id)
                    .order_by(CaseEvent.created_at, CaseEvent.id)
                )
            ]

    @app.post("/api/v1/cases/{case_id}/draft", status_code=202, dependencies=[Depends(authorized)])
    def draft(case_id: str) -> dict:
        with factory.begin() as session:
            case = get_case(session, case_id, lock=True)
            if case.state not in {
                State.NEW,
                State.INVESTIGATING,
                State.DRAFT_READY,
                State.NEEDS_APPROVAL,
                State.ESCALATED,
                State.BLOCKED,
            }:
                raise HTTPException(409, "Case is not ready for a draft")
            held_replies = session.scalars(
                select(MessageRow).where(
                    MessageRow.case_id == case.id,
                    MessageRow.direction == "inbound",
                    MessageRow.status.in_(["needs_review", "review_queued"]),
                )
            )
            if any(
                row.status == "review_queued" or not row.metadata_json.get("buyer_review")
                for row in held_replies
            ):
                raise HTTPException(
                    409, "Review the held supplier reply before drafting another inquiry"
                )
            if case.state in {State.ESCALATED, State.BLOCKED}:
                for stale in session.scalars(
                    select(MessageRow).where(
                        MessageRow.case_id == case.id, MessageRow.status == "draft"
                    )
                ):
                    stale.status = "superseded"
                session.flush()
                move(
                    session,
                    case,
                    State.INVESTIGATING,
                    clock.now(),
                    "Buyer requested a factual clarification; no terms accepted",
                )
            draft_revision = session.scalar(
                select(func.count())
                .select_from(MessageRow)
                .where(MessageRow.case_id == case.id, MessageRow.status == "superseded")
            )
            job = enqueue(
                session,
                "draft",
                {"case_id": case.id, "require_approval": True},
                f"manual-draft:{case.id}:{case.updated_at.isoformat()}:{draft_revision}",
                clock.now(),
            )
            return {"job_id": job.id, "status": job.status}

    @app.post("/api/v1/cases/{case_id}/review-reply", status_code=202)
    def review_reply(case_id: str, review: ReplyReview, admin: str = Depends(authorized)) -> dict:
        with factory.begin() as session:
            case = get_case(session, case_id, lock=True)
            row = session.get(MessageRow, review.message_id)
            if row is None or row.case_id != case.id or row.direction != "inbound":
                raise HTTPException(404, "Reply not found for this case")
            previous = row.metadata_json.get("buyer_review")
            if previous:
                if previous["request"] == review.model_dump():
                    return {"message_id": row.id, "status": row.status}
                if not (
                    previous["request"]["decision"] == "verify"
                    and review.decision == "reject"
                    and row.status == "needs_review"
                ):
                    raise HTTPException(409, "This reply already has a recorded review")
            if case.active_key is None or row.status != "needs_review":
                raise HTTPException(409, "Reply is not awaiting review on an active case")
            if review.decision == "verify" and not review.identity_verified:
                raise HTTPException(
                    422, "Verify sender identity and order attribution before processing"
                )
            row.metadata_json = {
                **row.metadata_json,
                "buyer_review_history": row.metadata_json.get("buyer_review_history", [])
                + ([previous] if previous else []),
                "buyer_review": {
                    "request": review.model_dump(),
                    "authenticated_by": admin,
                    "at": clock.now().isoformat(),
                },
            }
            row.status = "review_queued" if review.decision == "verify" else "rejected"
            session.add(
                ApprovalRow(
                    case_id=case.id,
                    action_type="verify_reply_identity",
                    proposed_payload={"message_id": row.id, **review.model_dump()},
                    status="approved" if review.decision == "verify" else "rejected",
                    decided_by=review.reviewer,
                    decided_at=clock.now(),
                )
            )
            record_event(
                session,
                case,
                "message.reviewed",
                {
                    "message_id": row.id,
                    "reason": review.note,
                    "decision": review.decision,
                    "reviewer": review.reviewer,
                },
                clock.now(),
            )
            if review.decision == "reject":
                return {"message_id": row.id, "status": row.status}
            job = enqueue(
                session,
                "review_reply",
                {"case_id": case.id, "message_id": row.id},
                f"review-reply:{row.id}",
                clock.now(),
            )
            return {"job_id": job.id, "status": job.status}

    @app.post("/api/v1/cases/{case_id}/approve-send", status_code=202)
    def approve(case_id: str, edit: DraftEdit, admin: str = Depends(authorized)) -> dict:
        with factory.begin() as session:
            case = get_case(session, case_id, lock=True)
            if session.scalar(
                select(MessageRow.id)
                .where(
                    MessageRow.case_id == case.id,
                    MessageRow.direction == "inbound",
                    MessageRow.status.in_(["needs_review", "review_queued"]),
                )
                .limit(1)
            ):
                raise HTTPException(
                    409, "Review the received supplier reply before sending another inquiry"
                )
            if case.state not in {State.DRAFT_READY, State.NEEDS_APPROVAL}:
                raise HTTPException(409, "Case is not awaiting send approval")
            message = session.scalar(
                select(MessageRow)
                .where(MessageRow.case_id == case.id, MessageRow.status == "draft")
                .with_for_update()
            )
            if message is None:
                raise HTTPException(409, "No pending draft")
            if edit.body:
                message.body = edit.body
            message.metadata_json = {
                **message.metadata_json,
                "approved_by": admin,
                "approved_body_sha256": hashlib.sha256(message.body.encode()).hexdigest(),
            }
            message.status = "queued"
            session.add(
                ApprovalRow(
                    case_id=case.id,
                    action_type="request_information",
                    proposed_payload={"message_id": message.id, "body": message.body},
                    status="approved",
                    decided_by=admin,
                    decided_at=clock.now(),
                )
            )
            record_event(
                session,
                case,
                "message.approved",
                {"message_id": message.id, "by": admin},
                clock.now(),
            )
            job = enqueue(
                session,
                "send",
                {"case_id": case.id, "message_id": message.id},
                f"send:{message.id}",
                clock.now(),
            )
            return {"job_id": job.id, "status": job.status}

    @app.post(
        "/api/v1/cases/{case_id}/reconcile", status_code=202, dependencies=[Depends(authorized)]
    )
    def reconcile_case(case_id: str) -> dict:
        with factory.begin() as session:
            case = get_case(session, case_id, lock=True)
            job = enqueue(
                session,
                "reconcile",
                {"po_line_id": case.po_line_id},
                f"manual-reconcile:{case.id}:{int(clock.now().timestamp()) // 60}",
                clock.now(),
            )
            return {"job_id": job.id, "status": job.status}

    @app.post("/api/v1/cases/{case_id}/escalate", dependencies=[Depends(authorized)])
    def escalate(case_id: str, request: Escalation) -> dict:
        with factory.begin() as session:
            case = get_case(session, case_id, lock=True)
            if case.active_key is None or case.state == State.ESCALATED:
                raise HTTPException(409, "Case is closed or already escalated")
            move(session, case, State.ESCALATED, clock.now(), request.reason)
            return {"case_id": case.id, "state": case.state}

    @app.get("/api/v1/system/status", dependencies=[Depends(authorized)])
    def status() -> dict:
        with factory() as session:
            return system_status(session, settings, clock)

    @app.get("/evidence/{fact_id}", dependencies=[Depends(authorized)])
    def evidence(fact_id: str) -> FileResponse:
        with factory() as session:
            fact = session.get(SourceFact, fact_id)
            source = session.get(SourceFile, fact.source_file_id) if fact else None
            if source is None:
                raise HTTPException(404, "Evidence not found")
            target = settings.data_dir / "blobs" / source.blob_ref
            if not target.is_file():
                raise HTTPException(404, "Evidence blob unavailable")
            return FileResponse(
                target, media_type="application/octet-stream", filename=Path(source.path).name
            )

    @app.get("/evidence/{fact_id}/preview", dependencies=[Depends(authorized)])
    def evidence_preview(fact_id: str) -> FileResponse:
        with factory() as session:
            fact = session.get(SourceFact, fact_id)
            source = session.get(SourceFile, fact.source_file_id) if fact else None
            if source is None:
                raise HTTPException(404, "Evidence not found")
            return preview_file(settings.data_dir / "blobs" / source.blob_ref, source.path)

    @app.get("/", response_class=HTMLResponse)
    def inbox(
        request: Request,
        state: State | None = None,
        priority: str | None = None,
        supplier_id: str | None = None,
    ) -> Response:
        try:
            authorized(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return RedirectResponse("/login", status_code=303)
            raise
        cases = list_cases(state, priority, supplier_id)
        return templates.TemplateResponse(
            request=request,
            name="inbox.html",
            context={
                "cases": cases,
                "status": status(),
                "states": list(State),
                "critical": sum(
                    c["priority"] in {"P0", "P1"} and c["state"] not in {"RESOLVED", "CANCELLED"}
                    for c in cases
                ),
                "action_count": sum(c["action_group"] == "Needs your action" for c in cases),
                "awaiting": sum(c["state"] == "AWAITING_REPLY" for c in cases),
                "resolved": sum(c["state"] == "RESOLVED" for c in cases),
            },
        )

    @app.get("/cases/{case_id}", response_class=HTMLResponse, dependencies=[Depends(authorized)])
    def case_page(request: Request, case_id: str) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="case.html",
            context={
                "case": detail(case_id),
                "events": events(case_id),
                "status": status(),
            },
        )

    @app.get("/worker-queue", response_class=HTMLResponse, dependencies=[Depends(authorized)])
    @app.get("/worker-queue/panel", response_class=HTMLResponse, dependencies=[Depends(authorized)])
    @app.get(
        "/worker-queue/summary", response_class=HTMLResponse, dependencies=[Depends(authorized)]
    )
    def worker_queue(request: Request) -> Response:
        with factory() as session:
            summary_only = request.url.path.endswith("/summary")
            jobs = (
                []
                if summary_only
                else list(
                    session.scalars(
                        select(JobRow)
                        .where(JobRow.status.in_(["running", "queued", "failed"]))
                        .order_by(JobRow.status.desc(), JobRow.run_after, JobRow.id)
                        .limit(100)
                    )
                )
            )
            queue_jobs = []
            for job in jobs:
                payload = job.payload_json.get("payload", {})
                source_id = payload.get("source_file_id")
                if not source_id and payload.get("source_fact_id"):
                    fact = session.get(SourceFact, payload["source_fact_id"])
                    source_id = fact.source_file_id if fact else None
                queue_jobs.append(
                    {
                        "job": job,
                        "source_id": source_id,
                        "case_id": payload.get("case_id"),
                        "retryable": job.status == "failed" and job.job_type in RETRYABLE_JOB_TYPES,
                    }
                )
            template = (
                "queue_summary.html"
                if summary_only
                else "queue_panel.html"
                if request.url.path.endswith("/panel")
                else "queue.html"
            )
            return templates.TemplateResponse(
                request=request,
                name=template,
                context={"status": system_status(session, settings, clock), "jobs": queue_jobs},
            )

    @app.get("/system", response_class=HTMLResponse, dependencies=[Depends(authorized)])
    def system_page(request: Request) -> Response:
        with factory() as session:
            files = list(
                session.scalars(select(SourceFile).order_by(SourceFile.downloaded_at.desc()))
            )
            jobs = list(
                session.scalars(
                    select(JobRow)
                    .where(JobRow.status.in_(["queued", "failed"]))
                    .order_by(JobRow.run_after)
                    .limit(50)
                )
            )
            return templates.TemplateResponse(
                request=request,
                name="system.html",
                context={
                    "status": status(),
                    "files": files,
                    "jobs": jobs,
                },
            )

    register_retry_routes(app, factory, clock, authorized)
    register_document_review_routes(app, factory, clock, authorized, templates, status)
    register_source_routes(app, factory, settings, clock, authorized, templates, status)
    return app
