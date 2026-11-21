from datetime import date

from supplier_chaser.commitments.checks import (
    Agreement,
    TermsReference,
    terms_checks,
    terms_verified,
)
from supplier_chaser.domain.models import Confirmation

AGREEMENT = Agreement(
    path="suppliers/S/contracts/lta.pdf",
    effective=date(2022, 1, 1),
    expires=date(2026, 12, 31),
    incoterms={"FOB Busan"},
    payment_terms={"NET45"},
)
REFERENCE = TermsReference(
    po_incoterm="FOB Busan",
    profile_incoterm="FOB Busan",
    profile_payment="NET45",
    qualification="qualified",
    agreement=AGREEMENT,
)


def claim(terms, day=date(2026, 12, 2)):
    return Confirmation(
        document_id="d",
        po_number="P",
        line_number="1",
        supplier_id="S",
        unit="PC",
        deliveries=[{"quantity": "10", "promised_date": day}],
        intent="confirmed",
        terms=terms,
    )


def outcome(confirmation, reference=REFERENCE):
    return {c.name: (c.status, c.decision) for c in terms_checks(confirmation, reference)}


def test_terms_agreeing_with_every_reference_verify():
    confirmation = claim("Incoterms FOB Busan · Payment NET45")
    checks = terms_checks(confirmation, REFERENCE)
    assert {c.status for c in checks} == {"pass"}
    assert terms_verified(confirmation, checks)


def test_order_and_supplier_disagree_on_incoterm():
    # Boise: our ERP orders DDP Hamburg, the supplier's contract and confirmation say EXW.
    reference = REFERENCE.model_copy(update={"po_incoterm": "DDP Hamburg"})
    confirmation = claim("EXW Busan")
    assert outcome(confirmation, reference)["incoterm"] == ("fail", "terms_conflict")
    assert not terms_verified(confirmation, terms_checks(confirmation, reference))


def test_delivery_after_agreement_expiry_needs_decision():
    # Hwaseong: confirmed delivery in 2027, agreement expires 2026-12-31.
    result = outcome(claim("FOB Busan", date(2027, 1, 11)))
    assert result["agreement_validity"] == ("fail", "agreement_expiry")
    assert result["incoterm"] == ("pass", None)


def test_conditional_supplier_qualification_blocks():
    # Pune: qualification conditional while PPAP is open.
    reference = REFERENCE.model_copy(update={"qualification": "conditional"})
    assert outcome(claim(""), reference)["supplier_qualification"] == (
        "fail",
        "supplier_qualification",
    )


def test_commercial_or_unreferenced_terms_never_verify():
    assert outcome(claim("FOB Busan plus 3% surcharge"))["terms"] == ("fail", "proposed_change")
    bare = TermsReference()
    confirmation = claim("FOB Busan")
    assert outcome(confirmation, bare)["incoterm"] == ("unknown", None)
    assert not terms_verified(confirmation, terms_checks(confirmation, bare))
    assert outcome(claim("Delivery as agreed"))["terms"] == ("unknown", None)


def test_missing_references_are_skipped_not_assumed():
    # Image-only agreements: order and profile still verify, validity stays unchecked.
    reference = REFERENCE.model_copy(update={"agreement": None})
    result = outcome(claim("FOB Busan, NET45"), reference)
    assert result == {
        "supplier_qualification": ("pass", None),
        "incoterm": ("pass", None),
        "payment_terms": ("pass", None),
    }


def decision(kind, choice, **extra):
    from datetime import UTC, datetime

    from supplier_chaser.domain.models import Decision

    return Decision(
        kind=kind,
        choice=choice,
        reason="Checked with the category manager.",
        decided_by="Buyer",
        decided_at=datetime(2026, 11, 21, tzinfo=UTC),
        **extra,
    )


def test_decision_settles_only_the_value_and_dates_it_covers():
    reference = REFERENCE.model_copy(update={"po_incoterm": "DDP Hamburg"})
    accept_exw = decision(
        "terms_conflict", "accept_supplier_terms", value={"accepted": "EXW Busan"}
    )
    confirmation = claim("EXW Busan")
    checks = terms_checks(confirmation, reference, [accept_exw])
    assert outcome_of(checks)["incoterm"] == ("pass", None)
    assert terms_verified(confirmation, checks)
    # A different stated term later is not covered by the earlier acceptance.
    other = terms_checks(claim("FCA Busan"), reference, [accept_exw])
    assert outcome_of(other)["incoterm"] == ("fail", "terms_conflict")
    renewal = decision("agreement_expiry", "renewal_confirmed", valid_until=date(2027, 6, 30))
    assert outcome_of(terms_checks(claim("", date(2027, 1, 11)), REFERENCE, [renewal]))[
        "agreement_validity"
    ] == ("pass", None)
    assert outcome_of(terms_checks(claim("", date(2027, 9, 1)), REFERENCE, [renewal]))[
        "agreement_validity"
    ] == ("fail", "agreement_expiry")


def test_later_rejection_overrides_earlier_acceptance():
    from datetime import UTC, datetime

    reference = REFERENCE.model_copy(update={"qualification": "conditional"})
    accepted = decision("supplier_qualification", "accept_conditional")
    blocked = decision("supplier_qualification", "block").model_copy(
        update={"decided_at": datetime(2026, 11, 22, tzinfo=UTC)}
    )
    checks = terms_checks(claim(""), reference, [accepted, blocked])
    assert outcome_of(checks)["supplier_qualification"] == ("fail", "supplier_qualification")


def outcome_of(checks):
    return {c.name: (c.status, c.decision) for c in checks}
