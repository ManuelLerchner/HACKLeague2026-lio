"""Reference data for checking supplier terms: our order row, supplier profile, agreement."""

import re
from datetime import date
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import PORow, SourceFact, SourceFile, SupplierRow
from supplier_chaser.commitments.checks import Agreement, TermsReference
from supplier_chaser.domain.terms import incoterms, payment_terms

AGREEMENT_PATH = re.compile(r"/contracts?/|agreement|contract", re.IGNORECASE)
EFFECTIVE = re.compile(r"effective(?:\s+from)?\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
EXPIRES = re.compile(r"expires(?:\s+on)?\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)


def _key(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.casefold())


def raw_values(session: Session, fact: SourceFact | None) -> dict[str, str]:
    """Columns of the original table row a mapped fact was projected from.

    Mapping keeps only model fields; columns such as the ERP incoterm stay in the row.
    """
    if fact is None or not fact.record_locator.startswith("mapped:"):
        return {}
    table_locator, row_kind, row_number = fact.record_locator.removeprefix("mapped:").rsplit(":", 2)
    table = session.scalar(
        select(SourceFact).where(
            SourceFact.source_file_id == fact.source_file_id,
            SourceFact.fact_type == "table",
            SourceFact.record_locator == table_locator,
        )
    )
    row = next(
        (
            r
            for r in (table.normalized_json.get("rows", []) if table else [])
            if r["locator"] == f"{row_kind}:{row_number}"
        ),
        None,
    )
    return {_key(k): str(v) for k, v in row["values"].items() if v} if row else {}


def _source_path(session: Session, fact: SourceFact | None) -> str | None:
    source = session.get(SourceFile, fact.source_file_id) if fact else None
    return f"{source.path} ({fact.record_locator})" if source and fact else None


def agreement(session: Session, root: str) -> Agreement | None:
    """The supplier's agreement in its verified folder; the most recently effective wins."""
    texts: dict[str, list[tuple[str, str]]] = {}
    for fact, path in session.execute(
        select(SourceFact, SourceFile.path)
        .join(SourceFile)
        .where(SourceFact.fact_type == "text", SourceFile.path.startswith(root + "/"))
    ):
        if AGREEMENT_PATH.search(path.removeprefix(root)):
            texts.setdefault(path, []).append(
                (fact.record_locator, fact.normalized_json.get("text", ""))
            )
    found = []
    for path, pages in texts.items():
        text = "\n".join(page for _, page in sorted(pages))
        if not text.strip():
            continue
        effective, expires = EFFECTIVE.search(text), EXPIRES.search(text)
        found.append(
            Agreement(
                path=path,
                effective=date.fromisoformat(effective.group(1)) if effective else None,
                expires=date.fromisoformat(expires.group(1)) if expires else None,
                incoterms=incoterms(text),
                payment_terms=payment_terms(text),
            )
        )
    return max(found, key=lambda a: (a.effective or date.min, a.path), default=None)


def terms_reference(session: Session, po_line_id: str) -> TermsReference:
    po = session.get(PORow, po_line_id)
    if po is None:
        return TermsReference()
    order_fact = session.get(SourceFact, po.source_fact_id)
    order = raw_values(session, order_fact)
    supplier = session.get(SupplierRow, po.supplier_id)
    # Only a verified supplier's own profile and folder count as reference data.
    profile_fact = (
        session.get(SourceFact, supplier.source_ref) if supplier and supplier.verified else None
    )
    profile = raw_values(session, profile_fact)
    profile_source = session.get(SourceFile, profile_fact.source_file_id) if profile_fact else None
    root = str(PurePosixPath(profile_source.path).parent) if profile_source else None
    return TermsReference(
        po_incoterm=order.get("incoterm") or order.get("incoterms"),
        po_source=_source_path(session, order_fact),
        profile_incoterm=profile.get("incoterm") or profile.get("incoterms"),
        profile_payment=profile.get("paymentterms"),
        qualification=profile.get("qualificationstatus"),
        profile_source=_source_path(session, profile_fact),
        agreement=agreement(session, root) if root and root != "." else None,
    )
