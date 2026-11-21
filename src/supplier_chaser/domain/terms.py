"""Delivery and payment conditions, parsed so wording and layout do not matter."""

import re

INCOTERM = re.compile(r"\b(EXW|FCA|FAS|FOB|CFR|CIF|CPT|CIP|DAP|DPU|DDP)\s+([A-Z][\w-]*)")
PAYMENT = re.compile(r"\bnet\s?(\d{1,3})\b|\b(\d{1,3})\s+days\s+net\b", re.IGNORECASE)
# Commercial content beyond delivery and payment terms is never verified as existing terms.
UNVERIFIABLE_TERMS = re.compile(
    r"fee|surcharge|price|cost|charge|deposit|prepay|advance|bank|iban|release|penalt|%|increase",
    re.IGNORECASE,
)


def incoterms(text: str) -> set[str]:
    # The named place is compared by its first word, so "FOB Busan port" equals "FOB Busan".
    return {f"{code} {place.capitalize()}" for code, place in INCOTERM.findall(text)}


def payment_terms(text: str) -> set[str]:
    return {f"NET{a or b}" for a, b in PAYMENT.findall(text)}


def stated_conditions(text: str) -> set[str]:
    return incoterms(text) | payment_terms(text)
