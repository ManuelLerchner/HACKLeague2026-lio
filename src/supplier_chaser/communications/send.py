import hashlib
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from supplier_chaser.adapters.db import CaseRow, MessageRow, PORow, SupplierRow
from supplier_chaser.cases.service import move, record_event
from supplier_chaser.config import Settings
from supplier_chaser.domain.models import Outbound, SendResult, State
from supplier_chaser.ports.mail import MailGateway
from supplier_chaser.workflow.jobs import enqueue


def send_block_reasons(
    message: MessageRow,
    supplier: SupplierRow | None,
    settings: Settings,
    supplier_sent_count: int,
    case_sent_count: int,
) -> list[str]:
    reasons = []
    if supplier is None:
        reasons.append("Supplier contact is missing; import and verify trusted supplier metadata.")
    else:
        if not supplier.verified:
            reasons.append("Supplier contact is unverified; verify trusted supplier metadata.")
        if message.recipients != [supplier.email]:
            reasons.append("Draft recipient differs from the verified supplier contact.")
    if settings.mail_mode != "fixture":
        if not settings.registered_sender_verified:
            reasons.append("Team sender registration has not been verified.")
        if not settings.mailbox_verified:
            reasons.append("Inbound mailbox has not been verified.")
    if not (message.metadata_json.get("approved_by") or message.metadata_json.get("safe_template")):
        reasons.append("Draft has no buyer approval or approved safe template.")
    if (
        message.metadata_json.get("approved_by")
        and message.metadata_json.get("approved_body_sha256")
        != hashlib.sha256(message.body.encode()).hexdigest()
    ):
        reasons.append("Draft body differs from the approved version; review and approve it again.")
    if message.sender != settings.mail_from:
        reasons.append("Draft sender differs from the configured team sender.")
    if supplier_sent_count >= settings.max_supplier_sends_per_day:
        reasons.append(
            f"Supplier send limit reached: {supplier_sent_count} sends in the last 24 hours; "
            f"limit {settings.max_supplier_sends_per_day}."
        )
    if case_sent_count >= settings.max_reminders + 1:
        reasons.append(
            f"Case send limit reached: {case_sent_count} sends; limit {settings.max_reminders + 1}."
        )
    return reasons


def lock_case_and_message(session: Session, message_id: str) -> tuple[CaseRow, MessageRow] | None:
    # Every writer locks the case before its messages; case_events inserts also take a key
    # share on the case row, so locking the message first deadlocks against a concurrent send.
    case_id = session.scalar(select(MessageRow.case_id).where(MessageRow.id == message_id))
    if case_id is None:
        return None
    case = session.scalar(select(CaseRow).where(CaseRow.id == case_id).with_for_update())
    message = session.scalar(
        select(MessageRow).where(MessageRow.id == message_id).with_for_update()
    )
    if case is None or message is None:
        return None
    return case, message


def send_message(
    factory: sessionmaker, message_id: str, gateway: MailGateway, settings: Settings, now: datetime
) -> None:
    with factory.begin() as session:
        locked = lock_case_and_message(session, message_id)
        if locked is None:
            return
        case, message = locked
        if message.status in {
            "sent",
            "send_uncertain",
            "failed_before_send",
            "dry_run",
            "blocked",
            "cancelled",
        }:
            return
        if message.status == "sending":
            message.status = "send_uncertain"
            move(
                session,
                case,
                State.ESCALATED,
                now,
                "Worker stopped during send; reconcile transport before retry",
            )
            record_event(session, case, "message.send_uncertain", {"message_id": message.id}, now)
            return
        if case.active_key is None or case.state not in {State.DRAFT_READY, State.NEEDS_APPROVAL}:
            message.status = "cancelled"
            return
        if session.scalar(
            select(MessageRow.id).where(
                MessageRow.case_id == case.id,
                MessageRow.direction == "inbound",
                MessageRow.status.in_(["needs_review", "review_queued"]),
            )
        ):
            message.status = "blocked"
            reason = "A supplier reply awaits review; process it before sending another inquiry."
            message.metadata_json = {**message.metadata_json, "block_reasons": [reason]}
            move(session, case, State.BLOCKED, now, reason)
            return
        po = session.get(PORow, case.po_line_id)
        assert po is not None
        supplier = session.scalar(
            select(SupplierRow).where(SupplierRow.id == po.supplier_id).with_for_update()
        )
        live = settings.mail_mode != "fixture"
        recent_sends = list(
            session.scalars(
                select(MessageRow.sent_or_received_at)
                .join(CaseRow, MessageRow.case_id == CaseRow.id)
                .join(PORow, CaseRow.po_line_id == PORow.id)
                .where(
                    MessageRow.direction == "outbound",
                    MessageRow.status.in_(["sent", "sending", "send_uncertain"]),
                    MessageRow.sent_or_received_at >= now - timedelta(days=1),
                    PORow.supplier_id == po.supplier_id,
                )
                .order_by(MessageRow.sent_or_received_at)
            )
        )
        sent_count = len(recent_sends)
        case_count = (
            session.scalar(
                select(func.count())
                .select_from(MessageRow)
                .where(
                    MessageRow.case_id == case.id,
                    MessageRow.direction == "outbound",
                    MessageRow.status.in_(["sent", "sending", "send_uncertain"]),
                )
            )
            or 0
        )
        # A supplier rate limit defers an otherwise authorized inquiry; other guards stop it.
        reasons = send_block_reasons(message, supplier, settings, 0, case_count)
        if reasons:
            message.status = "blocked"
            message.metadata_json = {**message.metadata_json, "block_reasons": reasons}
            move(
                session,
                case,
                State.BLOCKED,
                now,
                " ".join(reasons),
            )
            return
        if sent_count >= settings.max_supplier_sends_per_day:
            run_after = recent_sends[-settings.max_supplier_sends_per_day] + timedelta(
                days=1, microseconds=1
            )
            deferred = enqueue(
                session,
                "send",
                {"case_id": case.id, "message_id": message.id},
                f"send-deferred:{message.id}:{run_after.isoformat()}",
                now,
            )
            deferred.run_after = run_after
            message.status = "queued"
            message.metadata_json = {
                **message.metadata_json,
                "scheduled_send_at": run_after.isoformat(),
                "rate_limit_reason": "Supplier daily send limit reached; inquiry scheduled.",
            }
            record_event(
                session,
                case,
                "message.send_deferred",
                {"message_id": message.id, "run_after": run_after.isoformat()},
                now,
            )
            return
        if live and (settings.dry_run or not settings.outbound_mail_enabled):
            message.status = "dry_run"
            record_event(session, case, "message.dry_run", {"message_id": message.id}, now)
            return
        message.status = "sending"
        message.sent_or_received_at = now
        record_event(session, case, "message.send_requested", {"message_id": message.id}, now)
        outbound = Outbound(
            message_id=message.external_message_id,
            sender=message.sender,
            recipient=message.recipients[0],
            subject=message.subject,
            body=message.body,
        )
    # Persist intent before the nontransactional side effect. Never retry an uncertain send.
    try:
        result = gateway.send(outbound)
    except Exception:
        result = SendResult(status="send_uncertain", detail="Transport raised after send intent")
    with factory.begin() as session:
        locked = lock_case_and_message(session, message_id)
        assert locked is not None
        case, message = locked
        if message.status != "sending":
            return
        message.status = result.status
        message.sent_or_received_at = now
        message.metadata_json = {**message.metadata_json, "transport_detail": result.detail}
        record_event(session, case, f"message.{result.status}", {"message_id": message.id}, now)
        if result.status == "sent":
            move(session, case, State.AWAITING_REPLY, now, "Supplier information request sent")
            case.next_action_at = now + timedelta(hours=settings.follow_up_hours)
        elif result.status in {"send_uncertain", "failed_before_send"}:
            move(
                session,
                case,
                State.ESCALATED,
                now,
                "Send uncertain; check provider logs"
                if result.status == "send_uncertain"
                else "Send failed before DATA; manual retry required",
            )
