import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_document_investigation import analysis, run_investigation, seed_document

from supplier_chaser.adapters.db import ClaimRow, SourceFact, SourceFile
from supplier_chaser.adapters.llm_fixture import FixtureLLM
from supplier_chaser.adapters.repositories import CommitmentRepository
from supplier_chaser.agent.discover import all_document_findings
from supplier_chaser.commitments.reconcile import reconcile
from supplier_chaser.domain.models import Confirmation
from supplier_chaser.ingestion.document_review import (
    ClaimReview,
    DocumentReview,
    EvidenceReference,
    ItemReview,
    review_claim,
    review_document,
    reviewed_values,
)
from supplier_chaser.web.app import create_app


def setup_review(session, settings, clock):
    settings.llm_provider = "openai"
    source_id = seed_document(session, clock.now())
    run_investigation(session, settings, clock, FixtureLLM(), source_id)
    result = analysis(session)
    evidence = session.scalar(
        select(SourceFact).where(
            SourceFact.source_file_id == source_id, SourceFact.fact_type == "text"
        )
    )
    item = ItemReview(
        item_id="row-2",
        po_line_id="TEST-ORDER/2",
        reviewer="Buyer",
        note="Verified supplier item correspondence.",
        attribution_evidence=EvidenceReference(
            fact_id=evidence.id, quote="00020 TEST-PART 2,600 PC 2027-01-04"
        ),
    )
    return result, item


def test_reviewed_line_can_match_while_sibling_remains_ambiguous(settings, factory, clock):
    with factory.begin() as session:
        result, item = setup_review(session, settings, clock)
        request = DocumentReview(analysis_id=result.id, items=[item])
        jobs = review_document(session, request, "admin", clock.now())
        assert len(jobs) == 1
        assert review_document(session, request, "admin", clock.now())[0].id == jobs[0].id
        assert session.scalar(select(func.count()).select_from(ClaimRow)) == 1
        findings = all_document_findings(session)
        assert findings["TEST-ORDER/2"][0]["status"] == "complete"
        assert findings["TEST-ORDER/1"][0]["status"] == "ambiguous"
        repository = CommitmentRepository(session)
        assert (
            reconcile(
                repository.get_po_line("TEST-ORDER/2"),
                repository.get_claims("TEST-ORDER/2"),
                clock.now(),
            ).status
            == "MATCH"
        )
        conflict = item.model_copy(update={"po_line_id": "TEST-ORDER/1"})
        with pytest.raises(ValueError, match="different reviewed"):
            review_document(
                session,
                DocumentReview(analysis_id=result.id, items=[conflict]),
                "admin",
                clock.now(),
            )


def test_wrong_order_and_unproven_alias_rejected(settings, factory, clock):
    with factory.begin() as session:
        result, item = setup_review(session, settings, clock)
        with pytest.raises(ValueError, match="same supplier"):
            review_document(
                session,
                DocumentReview(
                    analysis_id=result.id, items=[item.model_copy(update={"po_line_id": "OTHER/1"})]
                ),
                "admin",
                clock.now(),
            )
        bad = item.model_copy(
            update={
                "attribution_evidence": EvidenceReference(
                    fact_id=item.attribution_evidence.fact_id, quote="Order TEST-ORDER"
                )
            }
        )
        with pytest.raises(ValueError, match="identify the supplier item"):
            review_document(
                session, DocumentReview(analysis_id=result.id, items=[bad]), "admin", clock.now()
            )


def test_unit_and_baseline_require_attributable_independent_evidence(settings, factory, clock):
    with factory.begin() as session:
        result, item = setup_review(session, settings, clock)
        source = SourceFile(
            path="suppliers/synthetic/contract.txt",
            checksum="contract",
            media_type="text/plain",
            parse_status="needs_review",
            blob_ref="contract",
            downloaded_at=clock.now(),
        )
        session.add(source)
        session.flush()
        baseline = SourceFact(
            source_file_id=source.id,
            record_locator="text",
            fact_type="text",
            normalized_json={"text": "TEST-PART PC. Existing terms FOB Busan."},
            extracted_at=clock.now(),
        )
        session.add(baseline)
        session.flush()
        confirmation = Confirmation(
            document_id="test",
            po_number="TEST-ORDER",
            line_number="2",
            supplier_id="SUP-TEST",
            unit=None,
            deliveries=[{"quantity": "2600", "promised_date": "2027-01-04"}],
            intent="confirmed",
            terms="FOB Busan",
            risks=["fee"],
        )
        reference = EvidenceReference(
            fact_id=baseline.id, quote="TEST-PART PC. Existing terms FOB Busan."
        )
        review = ClaimReview(
            reviewer="Buyer",
            note="Compared original contract and material.",
            unit="PC",
            unit_evidence=reference,
            baseline_terms_evidence=reference,
        )
        confirmed, verified = reviewed_values(session, confirmation, review, result.source_file_id)
        assert verified and confirmed.unit == "PC" and confirmed.risks == ["fee"]
        bad = review.model_copy(
            update={"unit_evidence": EvidenceReference(fact_id=baseline.id, quote="PC.")}
        )
        with pytest.raises(ValueError, match="material"):
            reviewed_values(session, confirmation, bad, result.source_file_id)
        source.path = "suppliers/other/contract.txt"
        with pytest.raises(ValueError, match="verified supplier"):
            reviewed_values(session, confirmation, review, result.source_file_id)


def test_claim_review_preserves_risks_and_audit(settings, factory, clock):
    with factory.begin() as session:
        result, item = setup_review(session, settings, clock)
        review_document(
            session, DocumentReview(analysis_id=result.id, items=[item]), "admin", clock.now()
        )
        claim = session.scalar(select(ClaimRow))
        reference = item.attribution_evidence
        request = ClaimReview(
            reviewer="Buyer",
            note="Checked the unit against original material evidence.",
            unit="PC",
            unit_evidence=reference,
        )
        job = review_claim(session, claim.id, request, "admin", clock.now())
        assert review_claim(session, claim.id, request, "admin", clock.now()).id == job.id
        assert session.scalar(select(func.count()).select_from(ClaimRow)) == 2
        claims = CommitmentRepository(session).get_claims("TEST-ORDER/2")
        assert len(claims) == 2
        assert all(not claim.baseline_terms_verified for claim in claims)


def test_review_routes_are_served_behind_session_and_csrf(settings, factory, clock):
    with factory.begin() as session:
        result, item = setup_review(session, settings, clock)
        source_id = result.source_file_id
        body = DocumentReview(analysis_id=result.id, items=[item]).model_dump(mode="json")
    with TestClient(create_app(settings, factory, clock)) as client:
        assert client.get(f"/sources/{source_id}/review").status_code == 401
        assert client.post("/api/v1/documents/review", json=body).status_code == 401
        assert client.post("/login", data={"token": "test-secret"}).status_code == 200
        page = client.get(f"/sources/{source_id}/review")
        assert page.status_code == 200
        assert f'data-analysis="{result.id}"' in page.text
        assert client.get("/api/v1/review-evidence?q=confirmation").json()
        assert client.post("/api/v1/documents/review", json=body).status_code == 403
        token = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        client.headers["X-CSRF-Token"] = token
        assert client.post("/api/v1/documents/review", json=body).status_code == 202

        with factory() as session:
            claim_id = session.scalar(select(ClaimRow.id))
        no_evidence = {"reviewer": "Buyer", "note": "No supporting evidence given."}
        assert client.post(f"/api/v1/claims/{claim_id}/review", json=no_evidence).status_code == 422
        assert client.post("/api/v1/claims/missing/review", json=no_evidence).status_code == 404


def add_contract(session, now, text="§ 4 Deliveries are made FOB Busan (Incoterms 2020)."):
    source = SourceFile(
        path="suppliers/synthetic/contracts/framework.pdf",
        checksum="framework",
        media_type="application/pdf",
        parse_status="parsed",
        blob_ref="framework",
        downloaded_at=now,
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


def reply_claim(session, now, risks):
    from supplier_chaser.ingestion.poll import save_claim

    confirmation = Confirmation(
        document_id="reply-1",
        po_number="TEST-ORDER",
        line_number="2",
        supplier_id="SUP-TEST",
        unit="PC",
        deliveries=[{"quantity": "2600", "promised_date": "2027-01-04"}],
        intent="confirmed",
        terms="Supply remains under the applicable agreement and the agreed FOB Busan terms",
        risks=risks,
    )
    save_claim(session, confirmation, now)
    session.flush()
    return session.scalar(select(ClaimRow))


def test_rules_verify_terms_and_acknowledged_flag_resolves_reply_claim(settings, factory, clock):
    from supplier_chaser.ingestion.document_review import claim_review_context

    with factory.begin() as session:
        seed_document(session, clock.now())
        contract = add_contract(session, clock.now())
        claim = reply_claim(session, clock.now(), ["contract"])
        repository = CommitmentRepository(session)
        before = reconcile(
            repository.get_po_line("TEST-ORDER/2"),
            repository.get_claims("TEST-ORDER/2"),
            clock.now(),
        )
        assert before.status == "UNKNOWN"
        # The agreement states FOB Busan, so the rules verify the terms without a buyer.
        assert before.latest_claim.baseline_terms_verified
        incoterm = next(c for c in before.latest_claim.checks if c.name == "incoterm")
        assert incoterm.status == "pass" and contract.source_file_id in {
            session.scalar(select(SourceFile.id).where(SourceFile.path == path))
            for path in incoterm.sources
        }
        context = claim_review_context(session, before.latest_claim.model_dump(mode="json"))
        assert not context["terms"] and context["suggestion"] is None
        assert list(context["acknowledgeable"]) == ["contract"] and not context["blocking"]
        review = ClaimReview(
            reviewer="Buyer",
            note="Supplier restates the framework agreement; no change.",
            acknowledged_risks=["contract"],
        )
        review_claim(session, claim.id, review, "admin", clock.now())
        after = reconcile(
            repository.get_po_line("TEST-ORDER/2"),
            repository.get_claims("TEST-ORDER/2"),
            clock.now(),
        )
        assert after.status == "MATCH"


def test_fee_flag_cannot_be_acknowledged(settings, factory, clock):
    from pydantic import ValidationError

    from supplier_chaser.ingestion.document_review import claim_review_context

    with factory.begin() as session:
        seed_document(session, clock.now())
        add_contract(session, clock.now())
        claim = reply_claim(session, clock.now(), ["fee", "instructions"])
        latest = CommitmentRepository(session).get_claims("TEST-ORDER/2")[0]
        context = claim_review_context(session, latest.model_dump(mode="json"))
        assert list(context["blocking"]) == ["fee"]
        with pytest.raises(ValidationError):
            ClaimReview(reviewer="Buyer", note="Fee is fine, trust me.", acknowledged_risks=["fee"])
        not_recorded = ClaimReview(
            reviewer="Buyer",
            note="Acknowledging a flag that is absent.",
            acknowledged_risks=["contract"],
        )
        with pytest.raises(ValueError, match="Not a recorded risk"):
            review_claim(session, claim.id, not_recorded, "admin", clock.now())
        review_claim(
            session,
            claim.id,
            ClaimReview(
                reviewer="Buyer",
                note="Carrier note is informational.",
                acknowledged_risks=["instructions"],
            ),
            "admin",
            clock.now(),
        )
        repository = CommitmentRepository(session)
        assert (
            reconcile(
                repository.get_po_line("TEST-ORDER/2"),
                repository.get_claims("TEST-ORDER/2"),
                clock.now(),
            ).status
            == "UNKNOWN"
        )


def test_missing_unit_prefills_from_order_and_validates(settings, factory, clock):
    from supplier_chaser.ingestion.document_review import suggest_unit

    with factory.begin() as session:
        seed_document(session, clock.now())
        claim = reply_claim(session, clock.now(), [])
        suggestion = suggest_unit(session, "TEST-ORDER/2")
        assert suggestion["unit"] == "PC"
        review = ClaimReview(
            reviewer="Buyer",
            note="Supplier quantity matches our order in pieces.",
            unit=suggestion["unit"],
            unit_evidence=EvidenceReference(
                fact_id=suggestion["fact_id"], quote=suggestion["quote"]
            ),
        )
        original = Confirmation.model_validate(claim.data).model_copy(update={"unit": None})
        confirmed, _ = reviewed_values(session, original, review, None)
        assert confirmed.unit == "PC"


def add_material_master(session, now, uom="PC", path="soony/master/components.csv"):
    source = SourceFile(
        path=path,
        checksum=f"master-{path}",
        media_type="text/csv",
        parse_status="needs_review",
        blob_ref=f"master-{path}",
        downloaded_at=now,
    )
    session.add(source)
    session.flush()
    session.add(
        SourceFact(
            source_file_id=source.id,
            record_locator="table:csv",
            fact_type="table",
            normalized_json={
                "columns": ["component_id", "uom"],
                "rows": [{"locator": "row:2", "values": {"component_id": "TEST-PART", "uom": uom}}],
            },
            extracted_at=now,
        )
    )
    session.flush()


@pytest.mark.parametrize(
    ("uom", "path", "expected"),
    [
        ("PC", "soony/master/components.csv", "PC"),
        ("KG", "soony/master/components.csv", None),
        ("PC", "suppliers/synthetic/components.csv", None),
    ],
)
def test_unitless_claim_takes_unit_only_from_agreeing_material_master(
    settings, factory, clock, uom, path, expected
):
    with factory.begin() as session:
        seed_document(session, clock.now())
        add_material_master(session, clock.now(), uom, path)
        claim = reply_claim(session, clock.now(), [])
        claim.data = {**claim.data, "unit": None, "terms": ""}
        claims = CommitmentRepository(session).get_claims("TEST-ORDER/2")
        assert claims[0].confirmation.unit == expected
        assert claims[0].unit_source == ("soony/master/components.csv" if expected else None)
