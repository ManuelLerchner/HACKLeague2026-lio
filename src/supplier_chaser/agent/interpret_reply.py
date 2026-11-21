from supplier_chaser.domain.models import Confirmation
from supplier_chaser.ports.llm import LLMExtractor

PROMPT_VERSION = "confirmation-v1"


def extract_validated(extractor: LLMExtractor, text: str) -> Confirmation:
    for attempt in range(2):
        try:
            result = extractor.extract_confirmation(text)
            if not result.evidence_quote or result.evidence_quote not in text:
                raise ValueError("Extraction lacks a verbatim source quote")
            for delivery in result.deliveries:
                quantity = format(delivery.quantity, "f")
                if quantity not in result.evidence_quote:
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
