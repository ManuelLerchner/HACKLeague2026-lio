"""Expand attribution context without importing filtered historical order lines."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import PORow, SourceFact
from supplier_chaser.domain.models import POLine
from supplier_chaser.ingestion.mapping import MappingSpec, mapped_records


def all_order_lines(session: Session, orders: list[POLine]) -> tuple[list[POLine], set[str]]:
    """Return sibling order context and original line keys with unproven coverage.

    Approved row filters select operational projections, not the set of candidate items
    in a supplier document. Reapply the same mapping without filters for attribution only.
    """
    expanded = {po.key: po for po in orders}
    missing: set[str] = set()
    wanted = {(po.po_number, po.supplier_id) for po in orders}
    failures: set[tuple[str, str]] = set()
    pools: dict[tuple[str, str, str], list[POLine] | None] = {}
    for po in orders:
        group = (po.po_number, po.supplier_id)
        row = session.get(PORow, po.key)
        origin = session.get(SourceFact, row.source_fact_id) if row else None
        if origin is None or origin.fact_type != "po":
            failures.add(group)
            continue
        key = (origin.source_file_id, po.po_number, po.supplier_id)
        if key not in pools:
            facts = list(
                session.scalars(
                    select(SourceFact).where(
                        SourceFact.source_file_id == origin.source_file_id,
                    )
                )
            )
            try:
                if origin.record_locator.startswith("mapped:"):
                    tables = [
                        fact
                        for fact in facts
                        if fact.fact_type == "table"
                        and origin.record_locator.startswith(f"mapped:{fact.record_locator}:")
                    ]
                    if len(tables) != 1:
                        raise ValueError("Mapped origin has no unique raw table")
                    table = tables[0]
                    approval = next(
                        (
                            fact
                            for fact in facts
                            if fact.fact_type == "mapping_review"
                            and fact.record_locator == f"mapped:{table.record_locator}:approval"
                        ),
                        None,
                    )
                    if approval is None:
                        raise ValueError("Mapping approval unavailable")
                    spec = MappingSpec.model_validate(approval.normalized_json["definition"])
                    if spec.kind != "po":
                        raise ValueError("Order mapping required")
                    filters = {
                        spec.columns[field]: [value]
                        for field, value in {
                            "po_number": po.po_number,
                            "supplier_id": po.supplier_id,
                        }.items()
                        if field in spec.columns
                    }
                    # Include closed siblings, while unrelated returns cannot invalidate this PO.
                    spec = spec.model_copy(update={"filters": filters})
                    records = mapped_records(table.normalized_json, spec)
                    pool = [POLine.model_validate(data) for _, data in records]
                else:
                    pool = [
                        POLine.model_validate(fact.normalized_json)
                        for fact in facts
                        if fact.fact_type == "po"
                    ]
                pools[key] = pool
            except (ValueError, TypeError, KeyError):
                pools[key] = None
        context_pool = pools[key]
        if context_pool is None:
            failures.add(group)
            continue
        if not any(candidate == po for candidate in context_pool):
            failures.add(group)
        for candidate in context_pool:
            candidate_group = (candidate.po_number, candidate.supplier_id)
            if candidate_group not in wanted:
                continue
            previous = expanded.get(candidate.key)
            if previous is not None and previous != candidate:
                failures.add(candidate_group)
                failures.add((previous.po_number, previous.supplier_id))
            else:
                expanded[candidate.key] = candidate
    missing.update(po.key for po in orders if (po.po_number, po.supplier_id) in failures)
    return sorted(expanded.values(), key=lambda po: po.key), missing
