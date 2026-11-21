from supplier_chaser.ingestion.document_review import (
    baseline_mismatch,
    covering_passage,
    stated_conditions,
)

CONTRACT = (
    "§ 4 Delivery and transfer of risk\nDeliveries are made FOB Yantian (Incoterms 2020). "
    "Risk passes in accordance with the agreed Incoterm.\n§ 5 Prices\nPayment NET30.\n§ 6 Quality"
)
HEADER = (
    "Customer: Soony Interactive GmbH · Your order 4500318763 of 2026-10-06 · "
    "Incoterms FOB Yantian · Payment NET30"
)


def test_conditions_ignore_wording_and_layout():
    assert stated_conditions(HEADER) == {"FOB Yantian", "NET30"}
    assert stated_conditions("net 30 days, FOB YANTIAN port") == {"FOB Yantian", "NET30"}
    assert stated_conditions("Supply remains under the agreed FOB Busan terms") == {"FOB Busan"}
    assert stated_conditions("Please confirm the order") == set()


def test_document_header_matches_contract_passage():
    quote = covering_passage(CONTRACT, stated_conditions(HEADER))
    assert quote and quote.startswith("Deliveries are made FOB Yantian")
    assert quote.endswith("Payment NET30.")
    assert baseline_mismatch(HEADER, quote) is None


def test_changed_or_commercial_terms_never_match():
    assert "NET60" in baseline_mismatch("FOB Yantian, payment NET60", CONTRACT)
    assert "commercial" in baseline_mismatch("FOB Yantian plus 3% surcharge", CONTRACT)
    assert "commercial" in baseline_mismatch("FOB Yantian, release after deposit", CONTRACT)
    assert baseline_mismatch("Delivery as usual", CONTRACT) is not None
    assert covering_passage("Deliveries FOB Busan.", {"FOB Yantian"}) is None


def test_line_status_ignores_siblings_and_absent_items():
    from supplier_chaser.agent.discover import line_status

    def finding(attribution):
        return {"attribution": attribution, "reason": attribution}

    assert line_status([finding("matched")])["status"] == "complete"
    assert line_status([finding("unmatched")])["status"] == "not_covered"
    assert line_status([finding("ambiguous")])["status"] == "ambiguous"
    assert line_status([finding("matched"), finding("ambiguous")])["status"] == "ambiguous"
