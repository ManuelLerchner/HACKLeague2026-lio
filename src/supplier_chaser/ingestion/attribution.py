"""Match document items without inferring supplier-to-ERP numbering conventions."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Literal

from supplier_chaser.domain.documents import (
    DocumentItem as DocumentItem,
)
from supplier_chaser.domain.documents import (
    DocumentItems as DocumentItems,
)
from supplier_chaser.domain.documents import (
    LineAttribution as LineAttribution,
)
from supplier_chaser.domain.models import POLine


def normalize_item_reference(value: str) -> str:
    """Remove numeric padding, never rescale item numbers (00020 stays 20)."""
    stripped = value.strip()
    return str(int(stripped)) if stripped.isascii() and stripped.isdecimal() else stripped


def _signature(item: DocumentItem) -> tuple:
    return item.quantity, item.promised_date, item.unit, item.terms


def attribute_items(
    document: DocumentItems,
    orders: Sequence[POLine],
    *,
    item_reference_map: Mapping[str, str] | None = None,
    reference_method: Literal["reviewed_reference", "learned_reference"] = "reviewed_reference",
) -> list[LineAttribution]:
    """Return line attribution; quantity/date similarity never establishes identity.

    A reviewed reference map relates document references to ERP line numbers. The caller
    may assert complete_order_lines only after establishing one item per order line.
    """
    applicable = [
        po
        for po in orders
        if po.po_number == document.po_number and po.supplier_id == document.supplier_id
    ]
    if len({po.key for po in applicable}) != len(applicable):
        raise ValueError("Order line identifiers must be unique")
    if len({normalize_item_reference(po.line_number) for po in applicable}) != len(applicable):
        raise ValueError("Normalized order line references must be unique")
    references: dict[str, str] = {}
    for reference, line in (item_reference_map or {}).items():
        normalized = normalize_item_reference(reference)
        target = normalize_item_reference(line)
        if normalized in references and references[normalized] != target:
            raise ValueError("Conflicting reviewed item references")
        references[normalized] = target

    by_part: dict[str, list[DocumentItem]] = defaultdict(list)
    orders_by_part: dict[str, list[POLine]] = defaultdict(list)
    for item in document.items:
        by_part[item.part_number].append(item)
    for po in applicable:
        orders_by_part[po.part_number].append(po)
    result = []
    for po in sorted(applicable, key=lambda value: value.key):
        items = sorted(by_part[po.part_number], key=lambda item: item.item_id)
        peers = orders_by_part[po.part_number]
        candidates = [item.item_id for item in items]
        normalized_line = normalize_item_reference(po.line_number)
        proposed = [
            item.item_id
            for item in items
            if item.item_reference is not None
            and normalize_item_reference(item.item_reference) == normalized_line
        ]
        reviewed = [
            item.item_id
            for item in items
            if item.item_reference is not None
            and references.get(normalize_item_reference(item.item_reference)) == normalized_line
        ]
        if reviewed:
            result.append(
                LineAttribution(
                    po_line_id=po.key,
                    status="matched",
                    item_ids=reviewed,
                    candidate_item_ids=candidates,
                    proposed_item_ids=proposed,
                    method=reference_method,
                    reason=(
                        "A reviewed reference mapping attributes these rows to the order line."
                        if reference_method == "reviewed_reference"
                        else "This supplier numbers items as ten times the order line in all "
                        "earlier documents; the item reference and material identify the line."
                    ),
                )
            )
        elif len(peers) == 1 and len(items) == 1 and not references:
            result.append(
                LineAttribution(
                    po_line_id=po.key,
                    status="matched",
                    item_ids=candidates,
                    candidate_item_ids=candidates,
                    proposed_item_ids=proposed,
                    method="unique_material",
                    reason=(
                        "One document item and one order line share this material "
                        "within the supplier and PO."
                    ),
                )
            )
        elif (
            document.complete_order_lines
            and not references
            and len(peers) == len(items)
            and items
            and len({_signature(item) for item in items}) == 1
        ):
            result.append(
                LineAttribution(
                    po_line_id=po.key,
                    status="matched",
                    item_ids=[items[0].item_id],
                    candidate_item_ids=candidates,
                    proposed_item_ids=proposed,
                    method="equivalent_group",
                    reason=(
                        "Reviewed one-item-per-order-line coverage and identical commitments "
                        "make every permutation equivalent. Candidate row identity remains "
                        "ambiguous; do not sum quantities."
                    ),
                )
            )
        else:
            result.append(
                LineAttribution(
                    po_line_id=po.key,
                    status="ambiguous" if items else "unmatched",
                    candidate_item_ids=candidates,
                    proposed_item_ids=proposed,
                    method="none",
                    reason=(
                        "Several material-compatible items or order lines need explicit "
                        "attribution. Matching dates or quantities cannot establish item identity."
                        if items
                        else "No document item identifies this order line's material."
                    ),
                )
            )
    return result
