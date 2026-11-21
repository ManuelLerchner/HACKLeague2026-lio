"""Rules that check a supplier's stated terms against our own reference data.

A check passes only when every reference we hold agrees; a disagreement becomes a named
buyer decision, and missing references leave the result unknown rather than passed.
"""

from datetime import date

from supplier_chaser.domain.models import Check, Confirmation, Value
from supplier_chaser.domain.terms import UNVERIFIABLE_TERMS, incoterms, payment_terms


class Agreement(Value):
    path: str
    effective: date | None = None
    expires: date | None = None
    incoterms: set[str] = set()
    payment_terms: set[str] = set()


class TermsReference(Value):
    po_incoterm: str | None = None
    po_source: str | None = None
    profile_incoterm: str | None = None
    profile_payment: str | None = None
    qualification: str | None = None
    profile_source: str | None = None
    agreement: Agreement | None = None


def _compare(
    name: str, stated: set[str], references: list[tuple[str, set[str], str]]
) -> list[Check]:
    checks = []
    for value in sorted(stated):
        known = [(label, values, source) for label, values, source in references if values]
        if not known:
            checks.append(
                Check(name=name, status="unknown", detail=f"No reference states {value}.")
            )
            continue
        conflicts = [
            f"{label} says {', '.join(sorted(values))}"
            for label, values, _ in known
            if value not in values
        ]
        sources = [source for _, _, source in known]
        if conflicts:
            checks.append(
                Check(
                    name=name,
                    status="fail",
                    decision="terms_conflict",
                    detail=f"Supplier states {value}; " + "; ".join(conflicts) + ".",
                    sources=sources,
                )
            )
        else:
            labels = ", ".join(label for label, _, _ in known)
            checks.append(
                Check(
                    name=name,
                    status="pass",
                    detail=f"{value} matches {labels}.",
                    sources=sources,
                )
            )
    return checks


def terms_checks(confirmation: Confirmation, reference: TermsReference) -> list[Check]:
    checks = []
    if reference.qualification:
        qualified = reference.qualification.casefold() == "qualified"
        checks.append(
            Check(
                name="supplier_qualification",
                status="pass" if qualified else "fail",
                decision=None if qualified else "supplier_qualification",
                detail=f"Supplier qualification is {reference.qualification}.",
                sources=[reference.profile_source] if reference.profile_source else [],
            )
        )
    agreement = reference.agreement
    days = [d.day for d in confirmation.deliveries if d.day]
    if agreement and (agreement.expires or agreement.effective) and days:
        outside = [
            day
            for day in days
            if (agreement.expires and day > agreement.expires)
            or (agreement.effective and day < agreement.effective)
        ]
        checks.append(
            Check(
                name="agreement_validity",
                status="fail" if outside else "pass",
                decision="agreement_expiry" if outside else None,
                detail=(
                    f"Delivery {outside[0]} falls outside the agreement "
                    f"({agreement.effective or '?'} to {agreement.expires or '?'})."
                    if outside
                    else f"Deliveries fall within the agreement, valid until {agreement.expires}."
                ),
                sources=[agreement.path],
            )
        )
    terms = confirmation.terms.strip()
    if not terms:
        return checks
    if UNVERIFIABLE_TERMS.search(terms):
        checks.append(
            Check(
                name="terms",
                status="fail",
                decision="proposed_change",
                detail="Stated terms include commercial conditions (fees, prices, release).",
            )
        )
        return checks
    stated_incoterms, stated_payment = incoterms(terms), payment_terms(terms)
    if not stated_incoterms and not stated_payment:
        checks.append(
            Check(
                name="terms",
                status="unknown",
                detail="The stated terms name no recognizable incoterm or payment condition.",
            )
        )
        return checks
    agreement_incoterms = agreement.incoterms if agreement else set()
    agreement_payment = agreement.payment_terms if agreement else set()
    agreement_path = agreement.path if agreement else ""
    checks += _compare(
        "incoterm",
        stated_incoterms,
        [
            (
                "our purchase order",
                incoterms(reference.po_incoterm or ""),
                reference.po_source or "",
            ),
            (
                "the supplier profile",
                incoterms(reference.profile_incoterm or ""),
                reference.profile_source or "",
            ),
            ("the agreement", agreement_incoterms, agreement_path),
        ],
    )
    checks += _compare(
        "payment_terms",
        stated_payment,
        [
            (
                "the supplier profile",
                payment_terms(reference.profile_payment or ""),
                reference.profile_source or "",
            ),
            ("the agreement", agreement_payment, agreement_path),
        ],
    )
    return checks


def terms_verified(confirmation: Confirmation, checks: list[Check]) -> bool:
    """Stated terms count as existing terms only when every terms check passed."""
    relevant = [c for c in checks if c.name in {"terms", "incoterm", "payment_terms"}]
    return (
        bool(confirmation.terms.strip())
        and bool(relevant)
        and all(c.status == "pass" for c in relevant)
    )
