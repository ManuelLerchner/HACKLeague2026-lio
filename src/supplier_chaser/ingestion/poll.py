import hashlib
import re
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import ClaimRow, PORow, SourceFact, SourceFile, SupplierRow
from supplier_chaser.adapters.portal_fixture import validate_path
from supplier_chaser.agent.interpret_reply import extract_validated
from supplier_chaser.config import Settings
from supplier_chaser.domain.models import Confirmation, POLine, Supplier
from supplier_chaser.ingestion.ocr import OCR
from supplier_chaser.ingestion.parse import parse
from supplier_chaser.ports.llm import LLMExtractor
from supplier_chaser.ports.portal import PortalClient
from supplier_chaser.workflow.events import append_event
from supplier_chaser.workflow.jobs import enqueue


def save_claim(
    session: Session,
    confirmation: Confirmation,
    now: datetime,
    fact_id: str | None = None,
    message_id: str | None = None,
) -> bool:
    digest = hashlib.sha256(confirmation.model_dump_json().encode()).hexdigest()
    if session.scalar(select(ClaimRow).where(ClaimRow.fingerprint == digest)):
        return False
    session.add(
        ClaimRow(
            po_line_id=f"{confirmation.po_number}/{confirmation.line_number}",
            supplier_id=confirmation.supplier_id,
            fingerprint=digest,
            data=confirmation.model_dump(mode="json"),
            source_fact_id=fact_id,
            message_id=message_id,
            received_at=now,
        )
    )
    return True


def project(
    session: Session,
    fact: SourceFact,
    settings: Settings,
    path: str,
    now: datetime,
    extractor: LLMExtractor,
    trusted_contacts: bool = False,
) -> str | None:
    data = fact.normalized_json
    if fact.fact_type == "supplier":
        supplier = Supplier.model_validate(data)
        trusted = path in settings.trusted_supplier_paths or trusted_contacts
        if not trusted:
            raise ValueError("Supplier contacts require an explicitly trusted metadata path")
        existing = session.get(SupplierRow, supplier.id)
        if existing and existing.email != supplier.email:
            raise ValueError("Supplier email change requires manual identity verification")
        session.merge(SupplierRow(**supplier.model_dump(), source_ref=fact.id, verified=True))
    elif fact.fact_type == "po":
        po = POLine.model_validate(data)
        previous = session.get(PORow, po.key)
        if previous:
            old = POLine.model_validate(previous.data)
            if old.document_version > po.document_version:
                return None
            if old.document_version == po.document_version and old != po:
                raise ValueError("Conflicting PO snapshots without an explicit new version")
            if (old.supplier_id, old.part_number) != (po.supplier_id, po.part_number):
                raise ValueError("Order supplier or part changed; identity review required")
        session.merge(
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
        return po.key
    elif fact.fact_type in {"confirmation", "text"}:
        confirmation = (
            Confirmation.model_validate(data)
            if fact.fact_type == "confirmation"
            else extract_validated(extractor, data["text"])
        )
        save_claim(session, confirmation, now, fact_id=fact.id)
        return f"{confirmation.po_number}/{confirmation.line_number}"
    return None


def poll_portal(
    session: Session,
    portal: PortalClient,
    settings: Settings,
    now: datetime,
    extractor: LLMExtractor,
) -> dict:
    listing = portal.list_files()
    if portal.complete_listing:
        present = {metadata.path for metadata in listing}
        for historical in session.scalars(select(SourceFile)):
            historical.deleted = historical.path not in present
    changed, failed = 0, 0
    for metadata in listing:
        validate_path(metadata.path)
        existing = session.scalar(
            select(SourceFile).where(
                SourceFile.path == metadata.path, SourceFile.checksum == metadata.checksum
            )
        )
        if existing and existing.parse_status != "retry":
            continue
        blob_ref = ""
        source = existing or SourceFile(
            path=metadata.path,
            checksum=metadata.checksum,
            downloaded_at=now,
            media_type=metadata.media_type,
            blob_ref="",
            parse_status="pending",
        )
        session.add(source)
        session.flush()
        source.attempts += 1
        if source.attempts == 1:
            append_event(
                session,
                source.id,
                "source.file_changed",
                {
                    "path": metadata.path,
                    "checksum": metadata.checksum,
                },
                now,
            )
        try:
            if metadata.size > settings.max_file_bytes:
                raise ValueError("File exceeds configured maximum size")
            content = portal.download(metadata)
            if len(content) != metadata.size or len(content) > settings.max_file_bytes:
                raise ValueError("Downloaded size differs from metadata or exceeds limit")
            digest = hashlib.sha256(content).hexdigest()
            if (
                re.fullmatch(r"[0-9a-fA-F]{64}", metadata.checksum)
                and digest != metadata.checksum.lower()
            ):
                raise ValueError("SHA-256 checksum mismatch")
            directory = settings.data_dir / "blobs"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            blob_ref = digest
            (directory / digest).write_bytes(content)
            source.blob_ref = blob_ref
            parsed = parse(metadata.path, content, OCR.from_settings(settings))
            review_required = False
            with session.begin_nested():
                for item in parsed:
                    fact = SourceFact(
                        source_file_id=source.id,
                        record_locator=item.locator,
                        fact_type=item.kind,
                        normalized_json=item.data,
                        extracted_at=now,
                        confidence="ocr" if "ocr" in item.data else "deterministic",
                    )
                    session.add(fact)
                    session.flush()
                    if item.kind == "table":
                        from supplier_chaser.ingestion.review import reuse_mapping

                        try:
                            with session.begin_nested():
                                applied = reuse_mapping(
                                    session, fact, metadata.path, settings, now, extractor
                                )
                            review_required = review_required or not applied
                        except (ValueError, TypeError):
                            review_required = True
                        continue
                    if item.kind == "text":
                        if settings.llm_provider != "fixture":
                            review_required = True
                            continue
                        try:
                            with session.begin_nested():
                                line_id = project(
                                    session, fact, settings, metadata.path, now, extractor
                                )
                        except (ValueError, TypeError):
                            review_required = True
                            continue
                    else:
                        line_id = project(session, fact, settings, metadata.path, now, extractor)
                    if line_id:
                        enqueue(
                            session,
                            "reconcile",
                            {"po_line_id": line_id},
                            f"reconcile:{source.id}:{line_id}",
                            now,
                        )
                source.parse_status = (
                    "needs_review" if review_required else ("parsed" if parsed else "unhandled")
                )
                source.error = (
                    "Review source structure or extraction before importing"
                    if review_required
                    else None
                )
                append_event(
                    session,
                    source.id,
                    "source.document_parsed",
                    {
                        "status": source.parse_status,
                        "facts": len(parsed),
                    },
                    now,
                )
            changed += 1
        except Exception as exc:
            source.parse_status = (
                "retry"
                if isinstance(exc, httpx.HTTPError) and source.attempts < 5
                else "needs_review"
            )
            # Exception text from providers may include private payloads or credentials.
            source.error = f"{type(exc).__name__}: import requires review"
            source.blob_ref = blob_ref
            failed += 1
    from supplier_chaser.agent.discover import discover_documents

    discover_documents(session, settings, now)
    return {
        "files": len(listing),
        "changed": changed,
        "failed": failed,
        "last_success": now.isoformat(),
    }
