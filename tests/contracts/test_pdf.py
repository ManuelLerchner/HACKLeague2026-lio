from pathlib import Path

from supplier_chaser.adapters.llm_fixture import FixtureLLM
from supplier_chaser.agent.interpret_reply import extract_validated
from supplier_chaser.ingestion.parse import parse


def test_pdf_confirmation_preserves_page_and_quote():
    facts = parse("amendment.pdf", Path("tests/fixtures/demo/amendment.pdf").read_bytes())
    assert len(facts) == 1 and facts[0].locator == "page:1"
    confirmation = extract_validated(FixtureLLM(), facts[0].data["text"])
    assert confirmation.quantity == 1000
    assert confirmation.supersedes == ["DEMO-C1"]
