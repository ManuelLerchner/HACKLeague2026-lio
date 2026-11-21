import io
from datetime import UTC, date, datetime

import pytest
from openpyxl import Workbook

from supplier_chaser.commitments.reconcile import reconcile
from supplier_chaser.domain.models import POLine
from supplier_chaser.ingestion.mapping import MappingSpec, mapped_records, suggest
from supplier_chaser.ingestion.parse import parse


def test_unknown_delimited_table_preserves_values_without_importing():
    fact = parse("arbitrary.csv", b"Reference;Amount;Date\n000123;1,25;21/11/2026\n")[0]
    assert fact.kind == "table"
    assert fact.data["rows"][0]["values"] == {
        "Reference": "000123",
        "Amount": "1,25",
        "Date": "21/11/2026",
    }


def test_mapping_preserves_date_precision_and_requires_explicit_number_format():
    table = parse("unknown.csv", b"Reference;Amount;Date\n000123;1,25;21/11/2026\n")[0].data
    spec = MappingSpec(
        kind="po",
        columns={"po_number": "Reference", "ordered_qty": "Amount", "ordered_at": "Date"},
        constants={"line_number": "1", "supplier_id": "S", "part_number": "P"},
        date_formats={"ordered_at": "%d/%m/%Y"},
        decimal_separator=",",
    )
    _, row = mapped_records(table, spec)[0]
    po = POLine.model_validate(row)
    assert po.po_number == "000123"
    assert po.ordered_at == date(2026, 11, 21)
    assert str(po.ordered_qty) == "1.25"
    assert po.unit is None
    assert reconcile(po, [], datetime(2026, 11, 23, tzinfo=UTC)).status == "UNKNOWN"
    with pytest.raises(ValueError, match="Invalid mapped record"):
        mapped_records(table, spec.model_copy(update={"decimal_separator": "."}))


def test_suggestions_do_not_choose_between_ambiguous_columns():
    assert "ordered_qty" not in suggest(["quantity", "qty"], "po")["columns"]
    assert suggest(["contact_email"], "supplier")["columns"]["email"] == "contact_email"


def test_mapping_rejects_missing_columns_unknown_targets_and_empty_quantities():
    table = parse("unknown.csv", b'Qty\n""\n')[0].data
    with pytest.raises(ValueError, match="missing source columns"):
        mapped_records(table, MappingSpec(kind="po", columns={"ordered_qty": "Absent"}))
    with pytest.raises(ValueError, match="unknown or duplicate"):
        mapped_records(table, MappingSpec(kind="po", constants={"execute": "ignore previous"}))
    with pytest.raises(ValueError, match="Invalid mapped record"):
        mapped_records(table, MappingSpec(kind="po", columns={"ordered_qty": "Qty"}))


def test_date_only_order_uses_conservative_grace_bound():
    po = POLine(
        po_number="P",
        line_number="1",
        supplier_id="S",
        part_number="X",
        ordered_qty=1,
        unit="EA",
        requested_delivery=date(2026, 11, 25),
        ordered_at=date(2026, 11, 21),
    )
    assert reconcile(po, [], datetime(2026, 11, 22, 12, tzinfo=UTC)).status == "GRACE"
    assert reconcile(po, [], datetime(2026, 11, 23, 0, tzinfo=UTC)).status == "DISCREPANCY"


def test_duplicate_csv_columns_are_not_silently_overwritten():
    with pytest.raises(ValueError, match="Duplicate"):
        parse("unknown.csv", b"amount,amount\n2,3\n")


def test_workbook_discovers_headers_below_title_and_preserves_preamble():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Confidential launch snapshot"])
    sheet.append([])
    sheet.append(["Edition", None, "Totals"])
    sheet.append(["Name", "Product", "Quantity"])
    sheet.append(["Example", "0001", "1.250"])
    output = io.BytesIO()
    workbook.save(output)
    facts = parse("arbitrary.xlsx", output.getvalue())
    assert facts[0].kind == "text"
    assert "Confidential launch snapshot" in facts[0].data["text"]
    assert facts[1].kind == "table"
    assert facts[1].data["header_row"] == 4
    assert facts[1].data["rows"][0]["values"]["Quantity"] == "1.250"
    assert facts[1].data["rows"][0]["locator"] == "Sheet:5"
