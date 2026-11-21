from datetime import date

from sqlalchemy import func, select

from supplier_chaser.adapters.db import (
    ApprovalRow,
    CaseRow,
    ClaimRow,
    JobRow,
    MessageRow,
    PORow,
    SourceFact,
    SourceFile,
    SupplierRow,
)
from supplier_chaser.adapters.llm_fixture import FixtureLLM
from supplier_chaser.agent.discover import discover_documents, relevant_documents
from supplier_chaser.agent.investigate_document import investigate_document
from supplier_chaser.cases.service import reconcile_line
from supplier_chaser.domain.documents import DocumentItem, DocumentItems
from supplier_chaser.domain.models import POLine


def seed_document(session, now, *, same_material=True):
    profile = SourceFile(
        path="suppliers/synthetic/profile.json",
        checksum="profile-v1",
        downloaded_at=now,
        media_type="application/json",
        parse_status="parsed",
        blob_ref="synthetic-profile",
    )
    source = SourceFile(
        path="suppliers/synthetic/confirmation.pdf",
        checksum="confirmation-v1",
        downloaded_at=now,
        media_type="application/pdf",
        parse_status="needs_review",
        blob_ref="synthetic-pdf",
    )
    session.add_all([profile, source])
    session.flush()
    supplier_fact = SourceFact(
        source_file_id=profile.id,
        record_locator="record:1",
        fact_type="supplier",
        normalized_json={
            "id": "SUP-TEST",
            "name": "Synthetic Supplier",
            "email": "supplier@example.test",
        },
        extracted_at=now,
    )
    session.add(supplier_fact)
    session.flush()
    session.add(
        SupplierRow(
            id="SUP-TEST",
            name="Synthetic Supplier",
            email="supplier@example.test",
            language="en",
            source_ref=supplier_fact.id,
            verified=True,
        )
    )
    items = []
    for number in (1, 2):
        material = "TEST-PART" if same_material else f"TEST-PART-{number}"
        po = POLine(
            po_number="TEST-ORDER",
            line_number=str(number),
            supplier_id="SUP-TEST",
            part_number=material,
            ordered_qty="2600",
            unit="PC",
            requested_delivery=date(2026, 12, 29) if number == 1 else date(2027, 1, 4),
            ordered_at=date(2026, 10, 20),
        )
        fact = SourceFact(
            source_file_id=profile.id,
            record_locator=f"po:{number}",
            fact_type="po",
            normalized_json=po.model_dump(mode="json"),
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
                data=po.model_dump(mode="json"),
                source_fact_id=fact.id,
            )
        )
        reference = f"{number * 10:05d}"
        items.append(
            DocumentItem(
                item_id=f"row-{number}",
                item_reference=reference,
                part_number=material,
                quantity="2600",
                promised_date=date(2027, 1, 4),
                unit="PC",
                evidence_quote=f"{reference} {material} 2,600 PC 2027-01-04",
            )
        )
    document = DocumentItems(
        po_number="TEST-ORDER",
        supplier_id="SUP-TEST",
        items=items,
        intent="confirmed",
        document_type="confirmation",
        complete_order_lines=True,
    )
    original = "Order TEST-ORDER\n" + "\n".join(item.evidence_quote for item in items)
    session.add(
        SourceFact(
            source_file_id=source.id,
            record_locator="page:1",
            fact_type="text",
            normalized_json={"text": original + "\nDOCUMENT_JSON:" + document.model_dump_json()},
            extracted_at=now,
        )
    )
    session.flush()
    return source.id


def analysis(session):
    return session.scalar(select(SourceFact).where(SourceFact.fact_type == "document_analysis"))


def run_investigation(session, settings, clock, model, source_id):
    _, _, _, fingerprint = next(
        row for row in relevant_documents(session) if row[0].id == source_id
    )
    investigate_document(session, source_id, fingerprint, settings, model, model, clock.now())


def test_ambiguous_items_generate_reviewable_findings_and_drafts_once(settings, factory, clock):
    settings.llm_provider = "openai"
    settings.auto_investigate_documents = True
    model = FixtureLLM()
    with factory.begin() as session:
        source_id = seed_document(session, clock.now())
        assert discover_documents(session, settings, clock.now()) == 1
        assert discover_documents(session, settings, clock.now()) == 1
        assert session.scalar(select(func.count()).select_from(JobRow)) == 1
        run_investigation(session, settings, clock, model, source_id)
        result = analysis(session).normalized_json
        assert result["status"] == "ambiguous"
        assert result["document"]["complete_order_lines"] is False
        findings = {line["po_line_id"]: line for line in result["line_findings"]}
        assert findings["TEST-ORDER/1"]["date_gap_days"] == 6
        assert findings["TEST-ORDER/2"]["date_gap_days"] == 0
        assert all(len(line["supplier_items"]) == 2 for line in findings.values())
        assert all(line["attribution"] == "ambiguous" for line in findings.values())
        assert session.scalar(select(func.count()).select_from(ClaimRow)) == 0
        assert set(session.scalars(select(CaseRow.state))) == {"NEEDS_APPROVAL"}
        assert set(session.scalars(select(MessageRow.status))) == {"draft"}
        assert session.scalar(select(func.count()).select_from(MessageRow)) == 2
        assert session.scalar(select(func.count()).select_from(ApprovalRow)) == 2
        assert not session.scalar(select(JobRow.id).where(JobRow.job_type == "send"))
        before = session.scalar(select(func.count()).select_from(SourceFact))
        run_investigation(session, settings, clock, model, source_id)
        assert session.scalar(select(func.count()).select_from(SourceFact)) == before
        assert session.scalar(select(func.count()).select_from(MessageRow)) == 2
        for line in ("TEST-ORDER/1", "TEST-ORDER/2"):
            reconcile_line(session, line, settings, clock.now())
        assert set(session.scalars(select(CaseRow.state))) == {"NEEDS_APPROVAL"}


def test_invalid_quote_produces_visible_error_and_no_claims_or_email(settings, factory, clock):
    settings.llm_provider = "openai"
    settings.auto_investigate_documents = True

    class InventedQuote(FixtureLLM):
        calls = 0

        def extract_document(self, text):
            self.calls += 1
            result = super().extract_document(text)
            return result.model_copy(
                update={
                    "items": [
                        result.items[0].model_copy(
                            update={"evidence_quote": "Invented unsupported evidence"}
                        )
                    ]
                }
            )

    model = InventedQuote()
    with factory.begin() as session:
        source_id = seed_document(session, clock.now())
        run_investigation(session, settings, clock, model, source_id)
        assert model.calls == 2
        assert analysis(session).normalized_json["status"] == "error"
        assert session.scalar(select(func.count()).select_from(ClaimRow)) == 0
        assert session.scalar(select(func.count()).select_from(MessageRow)) == 0
        assert set(session.scalars(select(CaseRow.state))) == {"ESCALATED"}
        run_investigation(session, settings, clock, model, source_id)
        assert model.calls == 2


def test_unique_material_items_create_individual_claims_and_only_late_case(
    settings, factory, clock
):
    settings.llm_provider = "openai"
    settings.auto_investigate_documents = True
    model = FixtureLLM()
    with factory.begin() as session:
        source_id = seed_document(session, clock.now(), same_material=False)
        run_investigation(session, settings, clock, model, source_id)
        assert analysis(session).normalized_json["status"] == "complete"
        claims = list(session.scalars(select(ClaimRow)))
        assert len(claims) == 2
        assert all(claim.data["deliveries"][0]["quantity"] == "2600" for claim in claims)
        assert set(session.scalars(select(CaseRow.issue_type))) == {"LATE_COMMITMENT"}
        assert session.scalar(select(func.count()).select_from(MessageRow)) == 1
        assert set(session.scalars(select(MessageRow.status))) == {"draft"}


def test_failed_draft_preserves_analysis_and_retry_reuses_validated_extraction(
    settings, factory, clock
):
    from supplier_chaser.agent.discover import request_document_investigation

    class CountingModel(FixtureLLM):
        calls = 0

        def extract_document(self, text):
            self.calls += 1
            return super().extract_document(text)

        def draft_supplier_question(self, *args):
            raise ValueError("Synthetic invalid draft")

    settings.llm_provider = "openai"
    settings.auto_investigate_documents = True
    model = CountingModel()
    with factory.begin() as session:
        source_id = seed_document(session, clock.now())
        first = request_document_investigation(session, source_id, clock.now())
        run_investigation(session, settings, clock, model, source_id)
        assert analysis(session).normalized_json["status"] == "ambiguous"
        assert session.scalar(select(func.count()).select_from(MessageRow)) == 0
        assert model.calls == 1
        first.status = "succeeded"
        retry = request_document_investigation(session, source_id, clock.now(), retry=True)
        assert retry.id != first.id
        assert (
            request_document_investigation(session, source_id, clock.now(), retry=True).id
            == retry.id
        )
        run_investigation(session, settings, clock, model, source_id)
        assert model.calls == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(SourceFact)
                .where(SourceFact.fact_type == "document_analysis")
            )
            == 2
        )


def test_held_inbound_reply_prevents_automatic_document_draft(settings, factory, clock):
    settings.llm_provider = "openai"
    settings.auto_investigate_documents = True
    model = FixtureLLM()
    with factory.begin() as session:
        source_id = seed_document(session, clock.now())
        for po in session.scalars(select(PORow)):
            reconcile_line(session, po.id, settings, clock.now())
        case = session.scalar(select(CaseRow))
        session.add(
            MessageRow(
                case_id=case.id,
                external_message_id="held@example.test",
                direction="inbound",
                sender="supplier@example.test",
                recipients=["buyer@example.test"],
                subject="Confirmation",
                body="Please clarify",
                status="needs_review",
                sent_or_received_at=clock.now(),
                metadata_json={},
            )
        )
        session.flush()
        run_investigation(session, settings, clock, model, source_id)
        assert (
            session.scalar(
                select(func.count())
                .select_from(MessageRow)
                .where(MessageRow.case_id == case.id, MessageRow.direction == "outbound")
            )
            == 0
        )


def test_failed_extraction_can_be_explicitly_retried_without_losing_history(
    settings, factory, clock
):
    from supplier_chaser.agent.discover import request_document_investigation

    class RecoveringModel(FixtureLLM):
        calls = 0

        def extract_document(self, text):
            self.calls += 1
            if self.calls <= 2:
                raise ValueError("Synthetic extraction failure")
            return super().extract_document(text)

    settings.llm_provider = "openai"
    model = RecoveringModel()
    with factory.begin() as session:
        source_id = seed_document(session, clock.now())
        job = request_document_investigation(session, source_id, clock.now())
        run_investigation(session, settings, clock, model, source_id)
        assert analysis(session).normalized_json["status"] == "error"
        job.status = "succeeded"
        request_document_investigation(session, source_id, clock.now(), retry=True)
        run_investigation(session, settings, clock, model, source_id)
        statuses = list(
            session.scalars(
                select(SourceFact).where(
                    SourceFact.source_file_id == source_id,
                    SourceFact.fact_type == "document_analysis",
                )
            )
        )
        assert sorted(fact.normalized_json["status"] for fact in statuses) == ["ambiguous", "error"]
        assert model.calls == 3


def test_new_evidence_replaces_stale_draft_on_escalated_case(settings, factory, clock):
    from supplier_chaser.agent.discover import request_document_investigation

    settings.llm_provider = "openai"
    model = FixtureLLM()
    with factory.begin() as session:
        source_id = seed_document(session, clock.now())
        run_investigation(session, settings, clock, model, source_id)
        stale = set(session.scalars(select(MessageRow.id)))
        for case in session.scalars(select(CaseRow)):
            case.state = "ESCALATED"
        request_document_investigation(session, source_id, clock.now(), retry=True)
        run_investigation(session, settings, clock, model, source_id)
        statuses = dict(session.execute(select(MessageRow.id, MessageRow.status)).all())
        assert {statuses[m] for m in stale} == {"superseded"}
        assert sorted(s for m, s in statuses.items() if m not in stale) == ["draft", "draft"]
        assert set(session.scalars(select(CaseRow.state))) == {"NEEDS_APPROVAL"}


def test_new_evidence_retires_rate_limited_inquiry(settings, factory, clock):
    from supplier_chaser.agent.discover import request_document_investigation

    settings.llm_provider = "openai"
    model = FixtureLLM()
    with factory.begin() as session:
        source_id = seed_document(session, clock.now())
        run_investigation(session, settings, clock, model, source_id)
        blocked = session.scalars(select(MessageRow)).first()
        blocked.status = "blocked"
        blocked.metadata_json = {
            **blocked.metadata_json,
            "block_reasons": ["Supplier send limit reached: 2 sends; limit 2."],
        }
        for case in session.scalars(select(CaseRow)):
            case.state = "ESCALATED"
        request_document_investigation(session, source_id, clock.now(), retry=True)
        run_investigation(session, settings, clock, model, source_id)
        assert blocked.status == "superseded"


def test_answered_missing_confirmation_gets_no_new_inquiry(settings, factory, clock):
    from supplier_chaser.agent.discover import request_document_investigation
    from supplier_chaser.domain.models import Confirmation
    from supplier_chaser.ingestion.poll import save_claim

    settings.llm_provider = "openai"
    model = FixtureLLM()
    with factory.begin() as session:
        source_id = seed_document(session, clock.now())
        run_investigation(session, settings, clock, model, source_id)
        save_claim(
            session,
            Confirmation(
                document_id="reply",
                po_number="TEST-ORDER",
                line_number="2",
                supplier_id="SUP-TEST",
                unit="PC",
                deliveries=[{"quantity": "2600", "promised_date": "2027-01-04"}],
                intent="confirmed",
                terms="FOB Busan",
            ),
            clock.now(),
        )
        before = set(session.scalars(select(MessageRow.id)))
        for case in session.scalars(select(CaseRow)):
            case.state = "ESCALATED"
        request_document_investigation(session, source_id, clock.now(), retry=True)
        run_investigation(session, settings, clock, model, source_id)
        new = session.scalars(select(MessageRow).where(MessageRow.id.not_in(before))).all()
        lines = {session.get(CaseRow, m.case_id).po_line_id for m in new}
        assert lines == {"TEST-ORDER/1"}
