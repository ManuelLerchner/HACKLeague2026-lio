import re
from decimal import Decimal, InvalidOperation

from supplier_chaser.domain.models import Confirmation
from supplier_chaser.ports.llm import LLMExtractor

PROMPT_VERSION = "confirmation-v1"


def source_quote(text: str, quote: str) -> str:
    if not quote.strip():
        raise ValueError("Extraction lacks a verbatim source quote")
    if quote in text:
        return quote
    # PDF line wrapping may differ; restore the original span rather than accepting paraphrases.
    pattern = r"\s+".join(re.escape(token) for token in quote.split())
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise ValueError("Extraction lacks a verbatim source quote")
    return matches[0].group()


def extract_validated(extractor: LLMExtractor, text: str) -> Confirmation:
    for attempt in range(2):
        try:
            result = extractor.extract_confirmation(text)
            result = result.model_copy(
                update={"evidence_quote": source_quote(text, result.evidence_quote)}
            )
            for delivery in result.deliveries:
                tokens = re.findall(r"(?<![\w.])\d[\d,.]*(?![\w.])", result.evidence_quote)
                quantities = set()
                for token in tokens:
                    if "," in token:
                        if not re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", token):
                            continue
                        token = token.replace(",", "")
                    try:
                        quantities.add(Decimal(token.rstrip(".,")))
                    except InvalidOperation:
                        continue
                if delivery.quantity not in quantities:
                    raise ValueError("Extracted quantity is not supported by the quoted span")
                if (
                    delivery.promised_date
                    and delivery.promised_date.isoformat() not in result.evidence_quote
                ):
                    raise ValueError(
                        "Extracted date requires an unambiguous ISO date in the quoted span"
                    )
                if (
                    delivery.promised_at
                    and delivery.promised_at.isoformat() not in result.evidence_quote
                ):
                    raise ValueError("Extracted timestamp is not supported by the quoted span")
            # Quotes establish provenance; they do not grant authority to supplier instructions.
            if any(
                term in text.casefold()
                for term in (
                    "ignore previous",
                    "system prompt",
                    "bank account",
                    "iban",
                    "additional fee",
                    "surcharge",
                    "price increase",
                    "accept terms",
                    "payment details",
                )
            ):
                result = result.model_copy(
                    update={"risks": sorted(set(result.risks + ["instructions"]))}
                )
            return result
        except (ValueError, TypeError):
            if attempt:
                raise
    raise ValueError("Extraction failed")
