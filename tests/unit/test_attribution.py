from datetime import date
from decimal import Decimal

import pytest

from supplier_chaser.domain.models import POLine
from supplier_chaser.ingestion.attribution import (
    DocumentItem,
    DocumentItems,
    attribute_items,
    normalize_item_reference,
)


def order(line: str, material: str = "PART-A", requested: str = "2027-01-04") -> POLine:
    return POLine(
        po_number="PO-100",
        line_number=line,
        supplier_id="SUP-A",
        part_number=material,
        ordered_qty=Decimal("2600"),
        unit="PC",
        requested_delivery=date.fromisoformat(requested),
        ordered_at=date(2026, 10, 20),
    )


def item(reference: str, material: str = "PART-A", promised: str = "2027-01-04") -> DocumentItem:
    return DocumentItem(
        item_id="row-" + reference,
        item_reference=reference,
        part_number=material,
        quantity=Decimal("2600"),
        promised_date=date.fromisoformat(promised),
        unit="PC",
        evidence_quote=f"{reference} {material} 2,600 PC {promised}",
    )


def document(*items: DocumentItem, complete: bool = False) -> DocumentItems:
    return DocumentItems(
        po_number="PO-100",
        supplier_id="SUP-A",
        items=list(items),
        complete_order_lines=complete,
    )


def test_unique_material_matches_without_using_dates_as_identity():
    result = attribute_items(document(item("00010")), [order("2", requested="2026-12-29")])
    assert result[0].status == "matched"
    assert result[0].method == "unique_material"
    assert result[0].item_ids == ["row-00010"]


def test_same_material_dates_do_not_resolve_ambiguous_numbering():
    result = attribute_items(
        document(item("00010"), item("00020")),
        [
            order("1", requested="2026-12-29"),
            order("2"),
        ],
    )
    assert [match.status for match in result] == ["ambiguous", "ambiguous"]
    assert all(not match.item_ids and not match.proposed_item_ids for match in result)
    assert all(match.candidate_item_ids == ["row-00010", "row-00020"] for match in result)


def test_numeric_padding_never_rescales_supplier_items():
    assert normalize_item_reference("00020") == "20"
    assert normalize_item_reference(" 002 ") == "2"
    assert normalize_item_reference("A002") == "A002"


def test_reference_equality_is_only_a_proposal_without_reviewed_mapping():
    result = attribute_items(document(item("00010"), item("00020")), [order("10"), order("20")])
    assert all(match.status == "ambiguous" for match in result)
    assert result[1].proposed_item_ids == ["row-00020"]
    reviewed = attribute_items(
        document(item("00010"), item("00020")),
        [order("10"), order("20")],
        item_reference_map={"10": "10", "20": "20"},
    )
    assert all(match.status == "matched" for match in reviewed)
    assert reviewed[1].item_ids == ["row-00020"]


def test_reviewed_reference_mapping_can_explain_different_numbering():
    result = attribute_items(
        document(item("00010"), item("00020")),
        [order("1"), order("2")],
        item_reference_map={"00010": "1", "00020": "2"},
    )
    assert [match.item_ids for match in result] == [["row-00010"], ["row-00020"]]


def test_different_material_or_supplier_never_matches():
    assert attribute_items(document(item("00010", "PART-B")), [order("1")])[0].status == "unmatched"
    other = order("1").model_copy(update={"supplier_id": "OTHER"})
    assert attribute_items(document(item("00010")), [other]) == []


def test_reviewed_equivalent_group_preserves_one_quantity_per_line_and_row_ambiguity():
    result = attribute_items(
        document(item("00010"), item("00020"), complete=True),
        [
            order("1", requested="2026-12-29"),
            order("2"),
        ],
    )
    assert all(match.method == "equivalent_group" for match in result)
    assert all(len(match.item_ids) == 1 for match in result)
    assert all(len(match.candidate_item_ids) == 2 for match in result)


def test_equivalent_group_rejects_cardinality_or_commitment_difference():
    one = attribute_items(document(item("00010"), complete=True), [order("1"), order("2")])
    assert all(match.status == "ambiguous" for match in one)
    different = attribute_items(
        document(item("00010"), item("00020", promised="2027-01-05"), complete=True),
        [order("1"), order("2")],
    )
    assert all(match.status == "ambiguous" for match in different)


def test_duplicate_row_ids_and_conflicting_normalized_references_rejected():
    with pytest.raises(ValueError, match="row identifiers"):
        document(item("00010"), item("00010"))
    with pytest.raises(ValueError, match="Conflicting"):
        attribute_items(
            document(item("00010")), [order("1")], item_reference_map={"10": "1", "00010": "2"}
        )
    with pytest.raises(ValueError, match="Normalized order"):
        attribute_items(document(item("00010")), [order("1"), order("01")])


def test_multiple_rows_for_one_material_do_not_assume_split_deliveries():
    result = attribute_items(document(item("00010"), item("00020")), [order("1")])
    assert result[0].status == "ambiguous"
