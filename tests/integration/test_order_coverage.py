from datetime import date

from sqlalchemy import func, select

from supplier_chaser.adapters.db import PORow, SourceFact, SourceFile
from supplier_chaser.domain.models import POLine
from supplier_chaser.ingestion.mapping import MappingSpec
from supplier_chaser.ingestion.order_coverage import all_order_lines
from supplier_chaser.ingestion.tables import table_fact


def setup_pool(session, now, *, missing_approval=False, invalid_closed=False):
    source = SourceFile(
        path="orders.csv",
        checksum="test",
        downloaded_at=now,
        media_type="text/csv",
        blob_ref="test",
        parse_status="parsed",
    )
    session.add(source)
    session.flush()
    po = POLine(
        po_number="TEST-ORDER",
        line_number="2",
        supplier_id="TEST-SUPPLIER",
        part_number="TEST-PART",
        ordered_qty="10",
        unit="PC",
        ordered_at=date(2026, 1, 1),
        requested_delivery=date(2026, 11, 22),
    )
    data = po.model_dump(mode="json")
    closed = {**data, "line_number": "1", "status": "closed"}
    if invalid_closed:
        closed["ordered_qty"] = "unknown"
    unrelated = {**data, "po_number": "ANOTHER-ORDER", "status": "open"}
    table = table_fact(
        [closed, {**data, "status": "open"}, unrelated], "table:csv", ["row:2", "row:3", "row:4"]
    )
    session.add(
        SourceFact(
            source_file_id=source.id,
            record_locator=table.locator,
            fact_type="table",
            normalized_json=table.data,
            extracted_at=now,
        )
    )
    spec = MappingSpec(
        kind="po",
        columns={key: key for key, value in data.items() if value is not None},
        filters={"status": ["open"]},
    )
    if not missing_approval:
        session.add(
            SourceFact(
                source_file_id=source.id,
                record_locator="mapped:table:csv:approval",
                fact_type="mapping_review",
                normalized_json={"definition": spec.model_dump()},
                extracted_at=now,
            )
        )
    fact = SourceFact(
        source_file_id=source.id,
        record_locator="mapped:table:csv:row:3",
        fact_type="po",
        normalized_json=data,
        extracted_at=now,
    )
    session.add(fact)
    session.flush()
    session.add(
        PORow(
            id=po.key,
            po_number=po.po_number,
            line_number=po.line_number,
            supplier_id=po.supplier_id,
            part_number=po.part_number,
            data=data,
            source_fact_id=fact.id,
        )
    )
    session.flush()
    return po, source


def test_expands_filtered_closed_lines_only_as_attribution_context(factory, clock):
    with factory.begin() as session:
        po, _ = setup_pool(session, clock.now())
        expanded, missing = all_order_lines(session, [po])
        assert missing == set()
        assert [line.line_number for line in expanded] == ["1", "2"]
        assert session.scalar(select(func.count()).select_from(PORow)) == 1
        assert all(line.po_number == po.po_number for line in expanded)


def test_missing_mapping_and_invalid_hidden_rows_mark_coverage_unknown(factory, clock):
    with factory.begin() as session:
        po, _ = setup_pool(session, clock.now(), missing_approval=True)
        _, missing = all_order_lines(session, [po])
        assert missing == {po.key}


def test_invalid_filtered_row_cannot_establish_unique_material(factory, clock):
    with factory.begin() as session:
        po, _ = setup_pool(session, clock.now(), invalid_closed=True)
        _, missing = all_order_lines(session, [po])
        assert missing == {po.key}


def test_canonical_same_file_po_facts_supply_full_context(factory, clock):
    with factory.begin() as session:
        po, source = setup_pool(session, clock.now())
        fact = session.scalar(select(SourceFact).where(SourceFact.fact_type == "po"))
        fact.record_locator = "record:1"
        sibling = po.model_copy(update={"line_number": "1"})
        session.add(
            SourceFact(
                source_file_id=source.id,
                record_locator="record:2",
                fact_type="po",
                normalized_json=sibling.model_dump(mode="json"),
                extracted_at=clock.now(),
            )
        )
        expanded, missing = all_order_lines(session, [po])
        assert missing == set()
        assert len(expanded) == 2


def test_unrelated_invalid_return_does_not_poison_order_coverage(factory, clock):
    with factory.begin() as session:
        po, source = setup_pool(session, clock.now())
        table = session.scalar(
            select(SourceFact).where(
                SourceFact.source_file_id == source.id, SourceFact.fact_type == "table"
            )
        )
        data = dict(table.normalized_json)
        rows = [dict(row) for row in data["rows"]]
        rows[-1] = {**rows[-1], "values": {**rows[-1]["values"], "ordered_qty": "-23000"}}
        table.normalized_json = {**data, "rows": rows}
        session.flush()
        expanded, missing = all_order_lines(session, [po])
        assert not missing
        assert {item.key for item in expanded} == {"TEST-ORDER/1", "TEST-ORDER/2"}
