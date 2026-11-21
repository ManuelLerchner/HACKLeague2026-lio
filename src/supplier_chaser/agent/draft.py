from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import ApprovalRow, CaseRow, MessageRow, SupplierRow, uid
from supplier_chaser.adapters.llm_fixture import FixtureLLM
from supplier_chaser.adapters.repositories import CommitmentRepository
from supplier_chaser.cases.service import move, record_event
from supplier_chaser.config import Settings
from supplier_chaser.domain.models import State, Supplier
from supplier_chaser.ports.llm import LLMDrafter
from supplier_chaser.workflow.jobs import enqueue


def create_draft(
    session: Session,
    case_id: str,
    drafter: LLMDrafter,
    settings: Settings,
    now: datetime,
    reminder: bool = False,
    require_approval: bool = False,
) -> None:
    case = session.scalar(select(CaseRow).where(CaseRow.id == case_id).with_for_update())
    if case is None or case.active_key is None or case.state in {State.ESCALATED, State.BLOCKED}:
        return
    if session.scalar(
        select(MessageRow).where(
            MessageRow.case_id == case.id, MessageRow.status.in_(["draft", "queued", "sending"])
        )
    ):
        return
    if case.state == State.AWAITING_REPLY and not reminder:
        return
    po = CommitmentRepository(session).get_po_line(case.po_line_id)
    contact = session.get(SupplierRow, po.supplier_id)
    if contact is None or not contact.verified:
        move(session, case, State.BLOCKED, now, "No verified supplier contact")
        return
    supplier = Supplier.model_validate(
        {
            "id": contact.id,
            "name": contact.name,
            "email": contact.email,
            "language": contact.language,
        }
    )
    if case.state == State.NEW:
        move(session, case, State.INVESTIGATING, now, "Assemble order and source evidence")
    draft = drafter.draft_supplier_question(po, supplier, case.evidence_summary["reason"])
    if any(value not in draft.body for value in (po.po_number, po.line_number, po.part_number)):
        raise ValueError("Draft omitted required order identifiers")
    message_id = uid()
    row = MessageRow(
        id=message_id,
        case_id=case.id,
        external_message_id=f"<{message_id}@supplier-chaser.local>",
        direction="outbound",
        sender=settings.mail_from,
        recipients=[supplier.email],
        subject=f"[SC:{case.id}] {draft.subject}",
        body=draft.body,
        status="draft",
        sent_or_received_at=now,
        correlation_key=case.id,
        metadata_json={
            "model": drafter.model_version,
            "prompt_version": "question-v1",
            "safe_template": isinstance(drafter, FixtureLLM),
            "reminder": reminder,
        },
    )
    session.add(row)
    move(session, case, State.DRAFT_READY, now, "Factual supplier question prepared")
    record_event(session, case, "message.draft_ready", {"message_id": message_id}, now)
    # Only deterministic, bounded templates qualify for automatic delivery.
    if (
        not require_approval
        and isinstance(drafter, FixtureLLM)
        and (
            settings.mail_mode == "fixture"
            or (settings.outbound_mail_enabled and not settings.dry_run)
        )
    ):
        row.status = "queued"
        enqueue(
            session,
            "send",
            {"case_id": case.id, "message_id": message_id},
            f"send:{message_id}",
            now,
        )
    else:
        session.add(
            ApprovalRow(
                case_id=case.id,
                action_type="request_information",
                proposed_payload={"message_id": message_id},
                status="pending",
            )
        )
        move(
            session, case, State.NEEDS_APPROVAL, now, "Buyer must review this draft before sending"
        )
