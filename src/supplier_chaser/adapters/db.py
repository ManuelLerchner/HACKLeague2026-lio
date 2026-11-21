from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator


def uid() -> str:
    return str(uuid4())


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Naive timestamp rejected")
        return value.astimezone(UTC) if value else None

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class Base(DeclarativeBase):
    pass


class SourceFile(Base):
    __tablename__ = "source_files"
    __table_args__ = (UniqueConstraint("path", "checksum"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    path: Mapped[str]
    checksum: Mapped[str]
    downloaded_at: Mapped[datetime] = mapped_column(UTCDateTime)
    media_type: Mapped[str]
    parse_status: Mapped[str] = mapped_column(default="pending")
    error: Mapped[str | None]
    blob_ref: Mapped[str]
    deleted: Mapped[bool] = mapped_column(default=False)
    attempts: Mapped[int] = mapped_column(default=0)


class SourceFact(Base):
    __tablename__ = "source_facts"
    __table_args__ = (UniqueConstraint("source_file_id", "record_locator"),)
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    source_file_id: Mapped[str] = mapped_column(ForeignKey("source_files.id"))
    record_locator: Mapped[str]
    fact_type: Mapped[str]
    normalized_json: Mapped[dict] = mapped_column(JSON)
    extracted_at: Mapped[datetime] = mapped_column(UTCDateTime)
    confidence: Mapped[str] = mapped_column(default="deterministic")


class SourceMapping(Base):
    __tablename__ = "source_mappings"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    path: Mapped[str] = mapped_column(index=True)
    table_locator: Mapped[str]
    schema_fingerprint: Mapped[str]
    definition: Mapped[dict] = mapped_column(JSON)
    source_file_id: Mapped[str] = mapped_column(ForeignKey("source_files.id"))
    approved_by: Mapped[str]
    approved_at: Mapped[datetime] = mapped_column(UTCDateTime)


class SupplierRow(Base):
    __tablename__ = "suppliers"
    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    email: Mapped[str]
    language: Mapped[str]
    source_ref: Mapped[str] = mapped_column(ForeignKey("source_facts.id"))
    verified: Mapped[bool] = mapped_column(default=False)


class PORow(Base):
    __tablename__ = "purchase_order_lines"
    __table_args__ = (UniqueConstraint("po_number", "line_number"),)
    id: Mapped[str] = mapped_column(primary_key=True)
    po_number: Mapped[str]
    line_number: Mapped[str]
    supplier_id: Mapped[str] = mapped_column(index=True)
    part_number: Mapped[str]
    data: Mapped[dict] = mapped_column(JSON)
    source_fact_id: Mapped[str] = mapped_column(ForeignKey("source_facts.id"))


class ClaimRow(Base):
    __tablename__ = "commitment_claims"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    po_line_id: Mapped[str] = mapped_column(index=True)
    supplier_id: Mapped[str]
    fingerprint: Mapped[str] = mapped_column(unique=True)
    data: Mapped[dict] = mapped_column(JSON)
    source_fact_id: Mapped[str | None] = mapped_column(ForeignKey("source_facts.id"))
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)
    verification_status: Mapped[str] = mapped_column(default="validated")


class CaseRow(Base):
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    key: Mapped[str] = mapped_column(index=True)
    # NULL releases the logical key when a case closes; historical cases remain immutable.
    active_key: Mapped[str | None] = mapped_column(unique=True)
    po_line_id: Mapped[str] = mapped_column(ForeignKey("purchase_order_lines.id"))
    issue_type: Mapped[str]
    priority: Mapped[str]
    state: Mapped[str]
    evidence_summary: Mapped[dict] = mapped_column(JSON)
    next_action_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    owner: Mapped[str] = mapped_column(default="Procurement")
    reminders: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CaseEvent(Base):
    __tablename__ = "case_events"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"), index=True)
    kind: Mapped[str]
    payload_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class MessageRow(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"), index=True)
    external_message_id: Mapped[str] = mapped_column(unique=True)
    content_hash: Mapped[str | None] = mapped_column(unique=True)
    direction: Mapped[str]
    sender: Mapped[str]
    recipients: Mapped[list] = mapped_column(JSON)
    subject: Mapped[str]
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str]
    sent_or_received_at: Mapped[datetime] = mapped_column(UTCDateTime)
    correlation_key: Mapped[str | None]
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    job_type: Mapped[str]
    payload_json: Mapped[dict] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime)
    lease_owner: Mapped[str | None]
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None]
    __table_args__ = (Index("jobs_due", "status", "run_after"),)


class ApprovalRow(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"))
    action_type: Mapped[str]
    proposed_payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str]
    decided_by: Mapped[str | None]
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class DecisionRow(Base):
    """A buyer decision that settles a failed check, for a supplier or one order line."""

    __tablename__ = "decisions"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(40))
    supplier_id: Mapped[str] = mapped_column(String(100), index=True)
    po_line_id: Mapped[str | None] = mapped_column(String(200), index=True)
    choice: Mapped[str] = mapped_column(String(40))
    value: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(Text)
    decided_by: Mapped[str]
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime)
    valid_until: Mapped[date | None]
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class SystemRow(Base):
    __tablename__ = "system_status"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


def make_engine(url: str) -> Engine:
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    return create_engine(url, pool_pre_ping=True, **kwargs)


def sessions(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
