import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IssueType(StrEnum):
    MISSING_CONFIRMATION = "MISSING_CONFIRMATION"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    LATE_COMMITMENT = "LATE_COMMITMENT"
    CHANGED_COMMITMENT = "CHANGED_COMMITMENT"
    CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
    NO_RESPONSE = "NO_RESPONSE"


class State(StrEnum):
    NEW = "NEW"
    INVESTIGATING = "INVESTIGATING"
    DRAFT_READY = "DRAFT_READY"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    AWAITING_REPLY = "AWAITING_REPLY"
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
    RECONCILING = "RECONCILING"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


TERMINAL = {State.RESOLVED, State.CANCELLED}


class Supplier(Value):
    id: str
    name: str
    email: str
    language: Literal["en", "pl", "ko", "zh"] = "en"

    @field_validator("name", "id")
    @classmethod
    def single_line(cls, value: str) -> str:
        if not value.strip() or len(value) > 150 or any(ord(c) < 32 for c in value):
            raise ValueError("Supplier name and identifier must be bounded single-line values")
        return value

    @field_validator("email")
    @classmethod
    def email_address(cls, value: str) -> str:
        if any(c in value for c in "\r\n ,;<>") or value.count("@") != 1:
            raise ValueError("Expected a single email address")
        return value.lower()


class POLine(Value):
    po_number: str
    line_number: str
    supplier_id: str
    part_number: str
    ordered_qty: Decimal = Field(gt=0)
    unit: str | None
    requested_delivery: date | None
    ordered_at: AwareDatetime | date
    document_version: int = Field(default=1, ge=1)
    launch_critical: bool = False
    lead_time_hours: int | None = Field(default=None, ge=0)

    @field_validator("po_number", "line_number", "supplier_id", "part_number")
    @classmethod
    def identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[\w./-]{1,100}", value):
            raise ValueError("Invalid order identifier")
        return value

    @property
    def key(self) -> str:
        return f"{self.po_number}/{self.line_number}"


class Delivery(Value):
    quantity: Decimal = Field(gt=0)
    promised_date: date | None = None
    promised_at: AwareDatetime | None = None

    @property
    def day(self) -> date | None:
        if self.promised_at is not None:
            from zoneinfo import ZoneInfo

            return self.promised_at.astimezone(ZoneInfo("Europe/Berlin")).date()
        return self.promised_date


class Confirmation(Value):
    document_id: str
    po_number: str
    line_number: str
    supplier_id: str
    unit: str | None
    deliveries: list[Delivery]
    terms: str = ""
    intent: Literal["confirmed", "considering", "unknown"] = "unknown"
    supersedes: list[str] = []
    replaces_previous: bool = False
    risks: list[Literal["fee", "contract", "bank", "identity", "instructions"]] = []
    evidence_quote: str = ""

    @property
    def quantity(self) -> Decimal:
        return sum((d.quantity for d in self.deliveries), Decimal(0))


class Check(Value):
    """One rule applied to a supplier assertion against our reference data."""

    name: str
    status: Literal["pass", "fail", "unknown"]
    detail: str
    sources: list[str] = []
    # The buyer decision a failure turns into; a pass or unknown needs none.
    decision: (
        Literal["terms_conflict", "agreement_expiry", "supplier_qualification", "proposed_change"]
        | None
    ) = None


class Claim(Value):
    id: str
    evidence_id: str
    received_at: AwareDatetime
    source_kind: Literal["file", "message"]
    confirmation: Confirmation
    baseline_terms_verified: bool = False
    acknowledged_risks: list[str] = []
    unit_source: str | None = None
    checks: list[Check] = []


class Discrepancy(Value):
    po_line_id: str
    issue_type: IssueType
    affected_quantity: Decimal | None = None
    requested_date: date | None = None
    committed_date: date | None = None
    evidence_fact_ids: list[str]
    explanation: str
    severity: Literal["P0", "P1", "P2", "P3"]


class Assessment(Value):
    status: Literal["MATCH", "DISCREPANCY", "UNKNOWN", "GRACE"]
    issues: list[Discrepancy] = []
    reason: str
    latest_claim: Claim | None = None


class Draft(Value):
    subject: str
    body: str
    language: Literal["en", "pl", "ko", "zh"]
    action: Literal["request_information"] = "request_information"


class Inbound(Value):
    external_id: str
    sender: str
    recipients: list[str]
    subject: str
    body: str
    received_at: AwareDatetime
    in_reply_to: str | None = None
    references: list[str] = []
    identity_verified: bool = False
    authentication: str = ""


class Outbound(Value):
    message_id: str
    sender: str
    recipient: str
    subject: str
    body: str


class SendResult(Value):
    status: Literal["sent", "failed_before_send", "send_uncertain", "dry_run"]
    detail: str = ""


class FileMetadata(Value):
    path: str
    checksum: str
    size: int = Field(ge=0)
    media_type: str


class ParsedFact(Value):
    kind: Literal["supplier", "po", "confirmation", "text", "table"]
    locator: str
    data: dict


class Event(Value):
    id: str
    type: str
    schema_version: int = 1
    aggregate_id: str
    occurred_at: datetime
    payload: dict
    idempotency_key: str
