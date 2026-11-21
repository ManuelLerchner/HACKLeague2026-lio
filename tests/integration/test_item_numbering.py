import pytest
from sqlalchemy import select
from test_document_investigation import run_investigation, seed_document

from supplier_chaser.adapters.db import PORow, SourceFact, SourceFile
from supplier_chaser.adapters.llm_fixture import FixtureLLM


def add_history(session, now, po_number, reference, line):
    """An earlier confirmation where material and quantity alone identify the line."""
    order_fact = session.scalar(select(SourceFact).where(SourceFact.fact_type == "po"))
    session.add(
        PORow(
            id=f"{po_number}/{line}",
            po_number=po_number,
            line_number=str(line),
            supplier_id="SUP-TEST",
            part_number="HIST-PART",
            data={"ordered_qty": "500", "unit": "PC"},
            source_fact_id=order_fact.id,
        )
    )
    source = SourceFile(
        path=f"suppliers/synthetic/history-{po_number}.pdf",
        checksum=po_number,
        media_type="application/pdf",
        parse_status="parsed",
        blob_ref=po_number,
        downloaded_at=now,
    )
    session.add(source)
    session.flush()
    item = {
        "item_id": "a",
        "item_reference": reference,
        "part_number": "HIST-PART",
        "quantity": "500",
    }
    session.add(
        SourceFact(
            source_file_id=source.id,
            record_locator="investigation:history",
            fact_type="document_analysis",
            normalized_json={
                "status": "complete",
                "document": {"po_number": po_number, "supplier_id": "SUP-TEST", "items": [item]},
            },
            extracted_at=now,
        )
    )
    session.flush()


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ([("00010", 1), ("00020", 2), ("00010", 1)], "matched"),
        ([("00010", 1), ("00020", 2), ("00010", 2)], "ambiguous"),
        ([("00010", 1), ("00020", 2)], "ambiguous"),
    ],
)
def test_learned_numbering_resolves_identical_items(settings, factory, clock, history, expected):
    settings.llm_provider = "openai"
    with factory.begin() as session:
        source_id = seed_document(session, clock.now())
        for index, (reference, line) in enumerate(history):
            add_history(session, clock.now(), f"HIST-{index}", reference, line)
        run_investigation(session, settings, clock, FixtureLLM(), source_id)
        result = session.scalar(
            select(SourceFact).where(
                SourceFact.source_file_id == source_id,
                SourceFact.fact_type == "document_analysis",
            )
        )
        findings = {f["po_line_id"]: f for f in result.normalized_json["line_findings"]}
        assert {f["attribution"] for f in findings.values()} == {expected}
        if expected == "matched":
            assert findings["TEST-ORDER/1"]["method"] == "learned_reference"
