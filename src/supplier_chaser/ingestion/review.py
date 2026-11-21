from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import SourceFact, SourceFile, SourceMapping
from supplier_chaser.config import Settings
from supplier_chaser.ingestion.mapping import MappingSpec, mapped_records
from supplier_chaser.ports.llm import LLMExtractor
from supplier_chaser.workflow.events import append_event
from supplier_chaser.workflow.jobs import enqueue


def mapping_application(session: Session, table: SourceFact) -> SourceFact | None:
    return session.scalar(
        select(SourceFact).where(
            SourceFact.source_file_id == table.source_file_id,
            SourceFact.record_locator == f"mapped:{table.record_locator}:approval",
        )
    )


def apply_mapping(
    session: Session,
    table: SourceFact,
    spec: MappingSpec,
    settings: Settings,
    now: datetime,
    extractor: LLMExtractor,
    approved_by: str,
    mapping_id: str | None = None,
) -> int:
    from supplier_chaser.ingestion.poll import project

    source = session.scalar(
        select(SourceFile).where(SourceFile.id == table.source_file_id).with_for_update()
    )
    if source is None or table.fact_type != "table":
        raise ValueError("A stored source table is required")
    previous = mapping_application(session, table)
    definition = spec.model_dump(mode="json")
    if previous:
        if previous.normalized_json["definition"] != definition:
            raise ValueError("This source version already has an approved mapping")
        return previous.normalized_json["row_count"]
    records = mapped_records(table.normalized_json, spec)
    if mapping_id is None:
        mapping = SourceMapping(
            path=source.path,
            table_locator=table.record_locator,
            schema_fingerprint=table.normalized_json["schema_fingerprint"],
            definition=definition,
            source_file_id=source.id,
            approved_by=approved_by,
            approved_at=now,
        )
        session.add(mapping)
        session.flush()
        mapping_id = mapping.id
    for locator, data in records:
        fact = SourceFact(
            source_file_id=source.id,
            record_locator=f"mapped:{table.record_locator}:{locator}",
            fact_type=spec.kind,
            normalized_json=data,
            extracted_at=now,
            confidence="approved_mapping",
        )
        session.add(fact)
        session.flush()
        line_id = project(
            session,
            fact,
            settings,
            source.path,
            now,
            extractor,
            trusted_contacts=spec.trust_contacts,
        )
        if line_id:
            enqueue(
                session,
                "reconcile",
                {"po_line_id": line_id},
                f"mapping:{mapping_id}:{source.id}:{line_id}",
                now,
            )
    session.add(
        SourceFact(
            source_file_id=source.id,
            record_locator=f"mapped:{table.record_locator}:approval",
            fact_type="mapping_review",
            extracted_at=now,
            confidence="approved_mapping",
            normalized_json={
                "mapping_id": mapping_id,
                "definition": definition,
                "approved_by": approved_by,
                "row_count": len(records),
            },
        )
    )
    session.flush()
    tables = list(
        session.scalars(
            select(SourceFact).where(
                SourceFact.source_file_id == source.id, SourceFact.fact_type == "table"
            )
        )
    )
    if all(mapping_application(session, item) for item in tables):
        source.parse_status, source.error = "parsed", None
    append_event(
        session,
        source.id,
        "source.mapping_applied",
        {
            "mapping_id": mapping_id,
            "table": table.record_locator,
            "rows": len(records),
            "approved_by": approved_by,
        },
        now,
    )
    from supplier_chaser.agent.discover import discover_documents

    discover_documents(session, settings, now)
    return len(records)


def reuse_mapping(
    session: Session,
    table: SourceFact,
    path: str,
    settings: Settings,
    now: datetime,
    extractor: LLMExtractor,
) -> bool:
    mapping = session.scalar(
        select(SourceMapping)
        .where(
            SourceMapping.path == path,
            SourceMapping.table_locator == table.record_locator,
            SourceMapping.schema_fingerprint == table.normalized_json["schema_fingerprint"],
        )
        .order_by(SourceMapping.approved_at.desc(), SourceMapping.id.desc())
        .limit(1)
    )
    if mapping is None:
        return False
    apply_mapping(
        session,
        table,
        MappingSpec.model_validate(mapping.definition),
        settings,
        now,
        extractor,
        mapping.approved_by,
        mapping.id,
    )
    return True
