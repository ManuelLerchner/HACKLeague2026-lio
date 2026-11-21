from datetime import UTC, datetime, timedelta

import pytest

from supplier_chaser.cases.state_machine import transition
from supplier_chaser.commitments.reconcile import reconcile
from supplier_chaser.domain.models import Claim, Confirmation, Delivery, IssueType, POLine, State

NOW = datetime(2026, 11, 21, 13, tzinfo=UTC)


def po(**changes):
    return POLine.model_validate(
        {
            "po_number": "P1",
            "line_number": "10",
            "supplier_id": "S1",
            "part_number": "A",
            "ordered_qty": "100",
            "unit": "EA",
            "requested_delivery": "2026-11-22",
            "ordered_at": "2026-11-19T10:00:00Z",
            **changes,
        }
    )


def claim(document_id="C1", quantity="100", day="2026-11-22", **changes):
    confirmation = Confirmation.model_validate(
        {
            "document_id": document_id,
            "po_number": "P1",
            "line_number": "10",
            "supplier_id": "S1",
            "unit": "EA",
            "intent": "confirmed",
            "deliveries": [{"quantity": quantity, "promised_date": day}],
            **changes,
        }
    )
    return Claim(
        id=document_id,
        evidence_id=document_id,
        received_at=NOW,
        source_kind="file",
        confirmation=confirmation,
    )


def test_match_is_confirmation_not_receipt():
    result = reconcile(po(), [claim()], NOW)
    assert result.status == "MATCH"
    assert "Physical receipt remains unverified" in result.reason


def test_missing_after_grace():
    assert reconcile(po(), [], NOW).issues[0].issue_type == IssueType.MISSING_CONFIRMATION
    assert reconcile(po(ordered_at=NOW), [], NOW).status == "GRACE"


def test_split_deliveries_sum_and_duplicates_do_not_add():
    c = claim(
        deliveries=[
            Delivery(quantity=40, promised_date="2026-11-22"),
            Delivery(quantity=60, promised_date="2026-11-22"),
        ]
    )
    duplicate = c.model_copy(update={"id": "copy"})
    assert reconcile(po(), [c, duplicate], NOW).status == "MATCH"


def test_partial_quantity_and_noncritical_priority():
    result = reconcile(po(), [claim(quantity="60")], NOW)
    assert result.issues[0].affected_quantity == 40
    assert result.issues[0].severity == "P2"


def test_late_launch_critical():
    result = reconcile(po(launch_critical=True, lead_time_hours=2), [claim(day="2026-11-23")], NOW)
    assert result.issues[0].issue_type == IssueType.LATE_COMMITMENT
    assert result.issues[0].severity == "P0"


def test_explicit_amendment_and_unordered_conflict():
    original, amended = claim(), claim("C2", quantity="80")
    assert (
        reconcile(po(), [original, amended], NOW).issues[0].issue_type
        == IssueType.CONTRADICTORY_EVIDENCE
    )
    amended = claim("C2", quantity="80", supersedes=["C1"])
    assert {i.issue_type for i in reconcile(po(), [original, amended], NOW).issues} == {
        IssueType.QUANTITY_MISMATCH,
        IssueType.CHANGED_COMMITMENT,
    }


@pytest.mark.parametrize(
    "changes", [{"unit": None}, {"intent": "considering"}, {"day": None}, {"risks": ["fee"]}]
)
def test_unknown_is_not_zero_or_match(changes):
    assert reconcile(po(), [claim(**changes)], NOW).status == "UNKNOWN"


def test_cutoff_uses_time_and_lead_time():
    c = claim(deliveries=[{"quantity": "100", "promised_at": "2026-11-22T18:00:00+01:00"}])
    assert (
        reconcile(po(launch_critical=True, lead_time_hours=3), [c], NOW).issues[0].issue_type
        == IssueType.LATE_COMMITMENT
    )


def test_state_machine_rejects_illegal_or_unproven_resolution():
    with pytest.raises(ValueError):
        transition(State.NEW, State.RESOLVED, adequate_evidence=True)
    with pytest.raises(ValueError):
        transition(State.RECONCILING, State.RESOLVED)
    assert transition(State.RECONCILING, State.RESOLVED, adequate_evidence=True) == State.RESOLVED


def test_clock_is_aware(clock):
    assert clock.now().utcoffset() == timedelta(0)
