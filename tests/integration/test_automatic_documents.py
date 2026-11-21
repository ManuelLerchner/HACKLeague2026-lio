from datetime import timedelta

from sqlalchemy import func, select

from supplier_chaser.adapters.db import JobRow, PORow, SourceFact, SourceFile
from supplier_chaser.agent.discover import discover_documents
from supplier_chaser.domain.models import POLine


def source_text(session, now, text, checksum="v1", path="supplier/acknowledgment.pdf"):
    source = SourceFile(
        path=path,
        checksum=checksum,
        downloaded_at=now,
        media_type="application/pdf",
        parse_status="needs_review",
        blob_ref=checksum,
    )
    session.add(source)
    session.flush()
    fact = SourceFact(
        source_file_id=source.id,
        record_locator="page:1",
        fact_type="text",
        normalized_json={"text": text},
        extracted_at=now,
    )
    session.add(fact)
    session.flush()
    return fact


def order_line(session, fact, now, number="PO-701", line="1"):
    po = POLine(
        po_number=number,
        line_number=line,
        supplier_id="SYN-SUPPLIER",
        part_number="PART-Z",
        ordered_qty="80",
        unit="PC",
        requested_delivery="2026-11-23",
        ordered_at=now - timedelta(days=3),
    )
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
    session.flush()


def investigations(session):
    return list(session.scalars(select(JobRow).where(JobRow.job_type == "investigate_document")))


def test_document_discovery_after_late_order_import_is_idempotent(settings, factory, clock):
    settings.auto_investigate_documents = True
    settings.llm_provider = "openai"
    with factory.begin() as session:
        fact = source_text(session, clock.now(), "Order acknowledgement PO-701 material PART-Z")
        discover_documents(session, settings, clock.now())
        assert not investigations(session)
        order_line(session, fact, clock.now())
        discover_documents(session, settings, clock.now())
        jobs = investigations(session)
        assert len(jobs) == 1
        original_id = jobs[0].id
        discover_documents(session, settings, clock.now())
        assert [job.id for job in investigations(session)] == [original_id]
        jobs[0].status = "succeeded"
        discover_documents(session, settings, clock.now() + timedelta(minutes=2))
        assert [job.id for job in investigations(session)] == [original_id]


def test_document_discovery_handles_all_matching_lines_together(settings, factory, clock):
    settings.auto_investigate_documents = True
    settings.llm_provider = "openai"
    with factory.begin() as session:
        fact = source_text(
            session,
            clock.now(),
            "Order acknowledgement PO-701\nItem A PART-Z 80 PC\nItem B PART-Z 80 PC",
        )
        order_line(session, fact, clock.now(), line="1")
        order_line(session, fact, clock.now(), line="2")
        discover_documents(session, settings, clock.now())
        assert len(investigations(session)) == 1


def test_changed_document_is_investigated_without_retrying_failed_version(settings, factory, clock):
    settings.auto_investigate_documents = True
    settings.llm_provider = "openai"
    with factory.begin() as session:
        first = source_text(session, clock.now(), "Order acknowledgement PO-701 material PART-Z")
        order_line(session, first, clock.now())
        discover_documents(session, settings, clock.now())
        investigations(session)[0].status = "failed"
        discover_documents(session, settings, clock.now() + timedelta(minutes=2))
        assert len(investigations(session)) == 1
        source_text(
            session,
            clock.now() + timedelta(minutes=3),
            "Amended order acknowledgement PO-701 material PART-Z",
            checksum="v2",
        )
        discover_documents(session, settings, clock.now() + timedelta(minutes=3))
        assert len(investigations(session)) == 2
        assert session.scalar(select(func.count()).select_from(SourceFile)) == 2


def test_order_identifier_prefix_and_reference_document_do_not_trigger_extraction(
    settings, factory, clock
):
    settings.auto_investigate_documents = True
    settings.llm_provider = "openai"
    with factory.begin() as session:
        fact = source_text(session, clock.now(), "Order acknowledgement PO-701 material PART-Z")
        order_line(session, fact, clock.now(), number="PO-70")
        source_text(
            session, clock.now(), "Company policy for factual supplier questions", path="policy.pdf"
        )
        discover_documents(session, settings, clock.now())
        assert not investigations(session)


def test_document_discovery_can_be_disabled(settings, factory, clock):
    settings.auto_investigate_documents = False
    settings.llm_provider = "openai"
    with factory.begin() as session:
        fact = source_text(session, clock.now(), "Order acknowledgement PO-701 material PART-Z")
        order_line(session, fact, clock.now())
        discover_documents(session, settings, clock.now())
        assert not investigations(session)


def test_findings_only_claim_pending_when_a_job_exists(settings, factory, clock):
    from supplier_chaser.agent.discover import all_document_findings

    settings.auto_investigate_documents = True
    settings.llm_provider = "openai"
    with factory.begin() as session:
        fact = source_text(session, clock.now(), "Order acknowledgement PO-701 material PART-Z")
        order_line(session, fact, clock.now())

        def status():
            return all_document_findings(session)["PO-701/1"][0]["status"]

        assert status() == "needs_review"
        discover_documents(session, settings, clock.now())
        assert status() == "pending"
        job = investigations(session)[0]
        job.status = "running"
        session.flush()
        assert status() == "pending"
        job.status = "failed"
        session.flush()
        assert status() == "error"
        job.status = "succeeded"
        session.flush()
        assert status() == "needs_review"
