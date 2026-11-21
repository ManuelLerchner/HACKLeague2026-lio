import re
from datetime import date
from decimal import Decimal, InvalidOperation

from supplier_chaser.domain.models import Confirmation
from supplier_chaser.domain.units import UNIT_ALIASES, canonical_unit
from supplier_chaser.ports.llm import LLMExtractor

PROMPT_VERSION = "confirmation-v1"


def supports_date(quote: str, value: date) -> bool:
    if re.search(r"(?<!\d)" + re.escape(value.isoformat()) + r"(?!\d)", quote):
        return True
    if re.search(rf"(?<!\d)0?{value.day}\.0?{value.month}\.{value.year}(?!\d)", quote):
        return True
    polish = (
        "stycznia lutego marca kwietnia maja czerwca lipca sierpnia "
        "września października listopada grudnia"
    ).split()
    if re.search(
        rf"(?<!\w)0?{value.day}\s+{polish[value.month - 1]}\s+{value.year}(?!\d)",
        quote,
        re.IGNORECASE,
    ):
        return True
    # CJK dates: 2026年12月2日 (zh/ja) and 2026년 12월 2일 (ko).
    if re.search(
        rf"(?<!\d){value.year}\s*[年년]\s*0?{value.month}\s*[月월]\s*0?{value.day}\s*[日일](?!\d)",
        quote,
    ):
        return True
    months = (
        "January February March April May June July August September October November December"
    ).split()
    month = months[value.month - 1]
    return bool(
        re.search(
            rf"(?<!\w)0?{value.day}\s+(?:{month}|{month[:3]})\s+{value.year}(?!\d)",
            quote,
            re.IGNORECASE,
        )
    )


def _span(text: str, quote: str, start: int = 0) -> re.Match[str] | None:
    # PDF line wrapping may differ; restore the original span rather than accepting paraphrases.
    pattern = r"\s+".join(re.escape(token) for token in quote.split())
    matches = list(re.finditer(pattern, text))
    return matches[0] if len(matches) == 1 and matches[0].start() >= start else None


def source_quote(text: str, quote: str, *, segments: bool = False) -> str:
    if not quote.strip():
        raise ValueError("Extraction lacks a verbatim source quote")
    if quote in text:
        return quote
    whole = _span(text, quote)
    if whole:
        return whole.group()
    if not segments:
        raise ValueError("Extraction lacks a verbatim source quote")
    # Replies concern one order line, so non-adjacent sentences may be joined; document rows
    # may not, since joining rows could pair one item's part number with another's quantity.
    spans: list[str] = []
    position = 0
    for part in re.split(r"\n\s*\n", quote):
        if not part.strip():
            continue
        match = _span(text, part, position)
        if match is None:
            raise ValueError("Extraction lacks a verbatim source quote")
        spans.append(match.group())
        position = match.end()
    if len(spans) < 2:
        raise ValueError("Extraction lacks a verbatim source quote")
    return "\n\n".join(spans)


def quantities_in_quote(quote: str) -> set[Decimal]:
    quantities = set()
    # Horizontal grouping is explicit; never join numbers from different PDF rows.
    grouped = r"(?<![\w.,])\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:,\d{1,2})?(?![\w.,\d])"
    for token in re.findall(grouped, quote):
        quantities.add(Decimal(re.sub(r"[ \u00a0\u202f]", "", token).replace(",", ".")))
    remaining = re.sub(grouped, " ", quote)
    for token in re.findall(r"(?<![\w.])\d[\d,.]*(?![\w.])", remaining):
        if "," in token:
            if not re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", token):
                continue
            token = token.replace(",", "")
        try:
            quantities.add(Decimal(token.rstrip(".,")))
        except InvalidOperation:
            continue
    return quantities


CURRENCY_CODES = frozenset("USD EUR GBP CHF PLN CNY RMB INR KRW JPY CAD AUD HKD SGD".split())


def mentions_unit(quote: str, word: str) -> bool:
    # CJK units attach directly to digits ("460,000件"), so word boundaries apply to Latin only.
    pattern = (
        re.escape(word) if not word.isascii() else r"(?<![a-z])" + re.escape(word) + r"(?![a-z])"
    )
    return bool(re.search(pattern, quote, re.IGNORECASE))


def validate_quantity_unit(unit: str | None, quote: str) -> str | None:
    """Return the canonical unit code once the quote evidences the unit."""
    if unit is None:
        return None
    if unit.upper() in CURRENCY_CODES:
        raise ValueError("Currency is not a quantity unit")
    canonical = canonical_unit(unit) or unit
    words = UNIT_ALIASES.get(canonical, (unit,))
    if not any(mentions_unit(quote, word) for word in words):
        raise ValueError("Quantity unit lacks source evidence")
    return canonical


def extract_validated(extractor: LLMExtractor, text: str) -> Confirmation:
    for attempt in range(2):
        try:
            result = extractor.extract_confirmation(text)
            result = result.model_copy(
                update={"evidence_quote": source_quote(text, result.evidence_quote, segments=True)}
            )
            result = result.model_copy(
                update={"unit": validate_quantity_unit(result.unit, result.evidence_quote)}
            )
            for delivery in result.deliveries:
                if delivery.quantity not in quantities_in_quote(result.evidence_quote):
                    raise ValueError("Extracted quantity is not supported by the quoted span")
                if delivery.promised_date and not supports_date(
                    result.evidence_quote, delivery.promised_date
                ):
                    raise ValueError(
                        "Extracted date requires an unambiguous supported date in the quoted span"
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
                    "release is pending",
                    "exw",
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
