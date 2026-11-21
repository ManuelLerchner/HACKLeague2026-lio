import pytest
from sqlalchemy import func, select

from supplier_chaser.adapters.db import CaseEvent, CaseRow, MessageRow, SupplierRow
from supplier_chaser.communications.send import send_block_reasons, send_message
from supplier_chaser.domain.models import State


def contact():
    return SupplierRow(id="SYN-A", name="Synthetic", email="supplier@example.test", verified=True)


def draft(settings):
    return MessageRow(
        sender=settings.mail_from,
        recipients=["supplier@example.test"],
        metadata_json={"approved_by": "buyer"},
    )


@pytest.mark.parametrize(
    "change,expected",
    [
        ("missing", "Supplier contact is missing"),
        ("unverified", "Supplier contact is unverified"),
        ("recipient", "Draft recipient differs"),
        ("registration", "Team sender registration has not been verified"),
        ("mailbox", "Inbound mailbox has not been verified"),
        ("approval", "Draft has no buyer approval"),
        ("sender", "Draft sender differs"),
        ("supplier_cap", "Supplier send limit reached"),
        ("case_cap", "Case send limit reached"),
    ],
)
def test_exact_send_blocker(settings, change, expected):
    settings.mail_mode = "smtp_imap"
    settings.registered_sender_verified = True
    settings.mailbox_verified = True
    supplier, message = contact(), draft(settings)
    supplier_count = case_count = 0
    if change == "missing":
        supplier = None
    elif change == "unverified":
        supplier.verified = False
    elif change == "recipient":
        message.recipients = ["different@example.test"]
    elif change == "registration":
        settings.registered_sender_verified = False
    elif change == "mailbox":
        settings.mailbox_verified = False
    elif change == "approval":
        message.metadata_json = {}
    elif change == "sender":
        message.sender = "different@example.test"
    elif change == "supplier_cap":
        supplier_count = settings.max_supplier_sends_per_day
    elif change == "case_cap":
        case_count = settings.max_reminders + 1
    reasons = send_block_reasons(message, supplier, settings, supplier_count, case_count)
    assert len(reasons) == 1
    assert reasons[0].startswith(expected)


def test_fixture_skips_live_setup_but_keeps_identity_and_approval_checks(settings):
    message = draft(settings)
    assert not send_block_reasons(message, contact(), settings, 0, 0)
    message.metadata_json = {}
    assert send_block_reasons(message, contact(), settings, 0, 0) == [
        "Draft has no buyer approval or approved safe template."
    ]


@pytest.mark.parametrize("address", ["a@example.test", "b@another.test", "buyer@supplier.test"])
def test_all_verified_supplier_contacts_are_eligible_without_address_lists(settings, address):
    settings.mail_mode = "smtp_imap"
    settings.registered_sender_verified = settings.mailbox_verified = True
    supplier, message = contact(), draft(settings)
    supplier.email = address
    message.recipients = [address]
    assert send_block_reasons(message, supplier, settings, 0, 0) == []


def test_all_blockers_are_retained_in_deterministic_order(settings):
    settings.mail_mode = "smtp_imap"
    message = draft(settings)
    message.metadata_json = {}
    reasons = send_block_reasons(message, contact(), settings, 0, 0)
    assert len(reasons) == 3
    assert reasons[0].startswith("Team sender")
    assert reasons[1].startswith("Inbound mailbox")
    assert reasons[2].startswith("Draft has no buyer approval")


def test_blocked_send_records_reason_and_never_retries(seeded, factory, clock, worker):
    worker.tick()
    with factory.begin() as session:
        message = session.scalar(select(MessageRow).where(MessageRow.direction == "outbound"))
        message.status = "queued"
        message_id, case_id = message.id, message.case_id
        session.get(CaseRow, case_id).state = State.DRAFT_READY
    seeded.mail_mode = "smtp_imap"
    seeded.registered_sender_verified = True
    seeded.mailbox_verified = True
    seeded.registered_sender_verified = False

    class NoSend:
        def send(self, message):
            raise AssertionError("Blocked send must not call the mail gateway")

    send_message(factory, message_id, NoSend(), seeded, clock.now())
    with factory() as session:
        message = session.get(MessageRow, message_id)
        assert message.status == "blocked"
        assert len(message.metadata_json["block_reasons"]) == 1
        reason = message.metadata_json["block_reasons"][0]
        assert reason.startswith("Team sender registration has not been verified")
        case = session.get(CaseRow, case_id)
        assert case.state == State.BLOCKED
        event = session.scalar(
            select(CaseEvent).where(
                CaseEvent.case_id == case_id,
                CaseEvent.kind == "case.blocked",
            )
        )
        assert event.payload_json["payload"]["reason"] == reason
        event_count = session.scalar(select(func.count()).select_from(CaseEvent))
    send_message(factory, message_id, NoSend(), seeded, clock.now())
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(CaseEvent)) == event_count
