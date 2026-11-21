"""Supplier item numbering (00010 = line 1) learned from that supplier's own documents."""

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import PORow, SourceFact
from supplier_chaser.domain.documents import DocumentItems
from supplier_chaser.domain.models import POLine

MIN_OBSERVATIONS = 3
STEP = 10


def _number(value: str | None) -> int | None:
    text = (value or "").strip()
    return int(text) if text.isascii() and text.isdecimal() else None


def numbering_observations(
    session: Session, supplier_id: str, exclude_source_id: str
) -> list[tuple[int, int]]:
    """(item reference, order line) pairs that part number and quantity establish on their own."""
    pairs = []
    for fact in session.scalars(
        select(SourceFact).where(
            SourceFact.fact_type == "document_analysis",
            SourceFact.source_file_id != exclude_source_id,
        )
    ):
        raw = fact.normalized_json.get("document")
        if not raw or raw.get("supplier_id") != supplier_id:
            continue
        lines = list(
            session.scalars(
                select(PORow).where(
                    PORow.po_number == raw["po_number"], PORow.supplier_id == supplier_id
                )
            )
        )
        items = raw.get("items", [])
        for item in items:
            reference = _number(item.get("item_reference"))
            same_part = [po for po in lines if po.part_number == item.get("part_number")]
            # Imported order lines can be partial (closed lines missing), so the material must
            # also be unique within the document before it identifies a line.
            repeated = sum(other.get("part_number") == item.get("part_number") for other in items)
            if reference is None or len(same_part) != 1 or repeated != 1:
                continue
            line = _number(same_part[0].line_number)
            try:
                same_quantity = Decimal(str(same_part[0].data["ordered_qty"])) == Decimal(
                    str(item["quantity"])
                )
            except (InvalidOperation, KeyError):
                continue
            if line is not None and same_quantity:
                pairs.append((reference, line))
    return pairs


def learned_reference_map(
    session: Session, document: DocumentItems, source_id: str, pool: Sequence[POLine]
) -> dict[str, str] | None:
    """Map this document's item references to lines, only for a convention without exceptions.

    `pool` holds every line of the order, closed ones included, so each item can be checked.
    """
    observed = numbering_observations(session, document.supplier_id, source_id)
    if len(observed) < MIN_OBSERVATIONS or any(ref != STEP * line for ref, line in observed):
        return None
    lines = {
        _number(po.line_number): po
        for po in pool
        if (po.po_number, po.supplier_id) == (document.po_number, document.supplier_id)
    }
    mapping = {}
    for item in document.items:
        reference = _number(item.item_reference)
        if reference is None or reference % STEP:
            return None
        po = lines.get(reference // STEP)
        # Every item must fit; one that does not means this document follows another scheme.
        if po is None or po.part_number != item.part_number:
            return None
        mapping[str(item.item_reference)] = po.line_number
    return mapping or None
