import hashlib
import json
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import CaseRow, MessageRow, PORow
from supplier_chaser.adapters.repositories import CommitmentRepository
from supplier_chaser.agent.interpret_reply import extract_validated
from supplier_chaser.cases.service import move, record_event
from supplier_chaser.communications.correlation import correlate
from supplier_chaser.domain.models import Inbound, State
from supplier_chaser.ingestion.poll import save_claim
from supplier_chaser.ports.llm import LLMExtractor
from supplier_chaser.workflow.jobs import enqueue


def receive(
    session: Session, message: Inbound, extractor: LLMExtractor, now: datetime, mailbox: str
) -> None:
    content_hash = hashlib.sha256(
        (message.sender.lower() + "\n" + message.body.strip()).encode()
    ).hexdigest()
    if session.scalar(
        select(MessageRow).where(
            or_(
                MessageRow.external_message_id == message.external_id,
                MessageRow.content_hash == content_hash,
            )
        )
    ):
        return
    outbound = session.execute(
        select(MessageRow, CaseRow, PORow)
        .join(CaseRow, MessageRow.case_id == CaseRow.id)
        .join(PORow, CaseRow.po_line_id == PORow.id)
        .where(MessageRow.direction == "outbound", MessageRow.status == "sent")
    ).all()
    candidate = correlate(
        message,
        [(m.external_message_id, c.id, m.recipients[0], p.po_number) for m, c, p in outbound],
    )
    if mailbox.lower() not in [r.lower() for r in message.recipients]:
        candidate = None
    row = MessageRow(
        case_id=candidate,
        external_message_id=message.external_id,
        content_hash=content_hash,
        direction="inbound",
        sender=message.sender,
        recipients=message.recipients,
        subject=message.subject,
        body=message.body,
        status="received" if candidate else "needs_review",
        sent_or_received_at=now,
        correlation_key=candidate,
        metadata_json={
            "supplier_timestamp": message.received_at.isoformat(),
            "model": extractor.model_version,
            "prompt_version": "confirmation-v1",
        },
    )
    session.add(row)
    session.flush()
    if candidate is None:
        return
    case = session.scalar(select(CaseRow).where(CaseRow.id == candidate).with_for_update())
    assert case is not None
    record_event(session, case, "message.received", {"message_id": row.id}, now)
    stale_drafts = list(
        session.scalars(
            select(MessageRow).where(
                MessageRow.case_id == case.id,
                MessageRow.direction == "outbound",
                MessageRow.status == "draft",
            )
        )
    )
    for draft in stale_drafts:
        draft.status = "superseded"
    if stale_drafts:
        record_event(
            session,
            case,
            "message.drafts_superseded",
            {
                "message_ids": [draft.id for draft in stale_drafts],
                "reason": "Supplier reply received; review it before another inquiry",
            },
            now,
        )
    if not message.identity_verified:
        row.status = "needs_review"
        if case.active_key is not None:
            move(
                session,
                case,
                State.ESCALATED,
                now,
                "Sender authentication is unverified; review identity",
            )
        return
    interpret_stored_reply(session, row, extractor, now)


def interpret_stored_reply(
    session: Session, row: MessageRow, extractor: LLMExtractor, now: datetime
) -> None:
    """Interpret a correlated reply after transport or recorded buyer identity verification."""
    if row.status == "rejected" or row.metadata_json.get("extraction"):
        return
    case = session.scalar(select(CaseRow).where(CaseRow.id == row.case_id).with_for_update())
    if case is None or case.active_key is None:
        row.status = "needs_review"
        return
    try:
        repository = CommitmentRepository(session)
        po = repository.get_po_line(case.po_line_id)
        identifiers = json.dumps(
            {
                "supplier_id": po.supplier_id,
                "po_number": po.po_number,
                "line_number": po.line_number,
            }
        )
        confirmation = extract_validated(
            extractor,
            "Trusted thread attribution (identifiers only, not evidence of quantities or dates): "
            + identifiers
            + "\nExtract only from the original supplier reply below.\n"
            + row.body,
        )
        if confirmation.evidence_quote not in row.body:
            raise ValueError("Reply evidence must come from the original message")
        if (confirmation.supplier_id, confirmation.po_number, confirmation.line_number) != (
            po.supplier_id,
            po.po_number,
            po.line_number,
        ):
            raise ValueError("Reply identifiers do not match the correlated order")
        if confirmation.replaces_previous and confirmation.intent == "confirmed":
            confirmation = confirmation.model_copy(
                update={
                    "supersedes": [
                        c.confirmation.document_id for c in repository.get_claims(case.po_line_id)
                    ]
                }
            )
        save_claim(session, confirmation, now, message_id=row.id)
        row.metadata_json = {
            **row.metadata_json,
            "extraction": confirmation.model_dump(mode="json"),
        }
        row.status = "received"
        if case.active_key is not None:
            if case.state in {State.AWAITING_REPLY, State.ESCALATED, State.BLOCKED}:
                move(
                    session,
                    case,
                    State.RESPONSE_RECEIVED,
                    now,
                    "Reply correlated with supplier and thread",
                )
            move(session, case, State.RECONCILING, now, "Validate supplier claims against order")
            case.next_action_at = None
        record_event(session, case, "commitment.claim_extracted", {"message_id": row.id}, now)
        enqueue(session, "reconcile", {"po_line_id": case.po_line_id}, f"reply:{row.id}", now)
    except (ValueError, TypeError):
        row.status = "needs_review"
        if case.active_key is not None:
            move(
                session,
                case,
                State.ESCALATED,
                now,
                "Ambiguous or invalid reply; manual review required",
            )
