import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated
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
from supplier_chaser.web.sources import register_source_routes
from supplier_chaser.workflow.jobs import enqueue

ROOT = Path(__file__).parent


class DraftEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str | None = Field(default=None, min_length=20, max_length=10000)


class Escalation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=1000)


def serialize_case(session: Session, case: CaseRow) -> dict:
    po = session.get(PORow, case.po_line_id)
    supplier = session.get(SupplierRow, po.supplier_id) if po else None
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
    return {
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
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'"
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
            return [
                serialize_case(session, c)
                for c in session.scalars(query.order_by(CaseRow.priority, CaseRow.created_at))
            ]

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
            }:
                raise HTTPException(409, "Case is not ready for a draft")
            job = enqueue(
                session,
                "draft",
                {"case_id": case.id},
                f"manual-draft:{case.id}:{case.updated_at.isoformat()}",
                clock.now(),
            )
            return {"job_id": job.id, "status": job.status}

    @app.post("/api/v1/cases/{case_id}/approve-send", status_code=202)
    def approve(case_id: str, edit: DraftEdit, admin: str = Depends(authorized)) -> dict:
        with factory.begin() as session:
            case = get_case(session, case_id, lock=True)
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

    register_source_routes(app, factory, settings, clock, authorized, templates, status)
    return app
