"""Provider-neutral item extraction and order attribution values."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from supplier_chaser.domain.models import Value


class DocumentItem(Value):
    item_id: str = Field(min_length=1)
    item_reference: str | None = None
    part_number: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    promised_date: date | None = None
    unit: str | None = None
    terms: str = ""
    evidence_quote: str = Field(min_length=1)


class DocumentItems(Value):
    po_number: str
    supplier_id: str
    items: list[DocumentItem]
    intent: Literal["confirmed", "considering", "unknown"] = "unknown"
    document_type: Literal["confirmation", "other"] = "other"
    document_terms: str = ""
    risks: list[Literal["fee", "contract", "bank", "identity", "instructions"]] = []
    complete_order_lines: bool = False

    @model_validator(mode="after")
    def unique_item_ids(self) -> "DocumentItems":
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("Document row identifiers must be unique")
        return self


class LineAttribution(Value):
    po_line_id: str
    status: Literal["matched", "ambiguous", "unmatched"]
    item_ids: list[str] = []
    candidate_item_ids: list[str] = []
    proposed_item_ids: list[str] = []
    method: Literal["reviewed_reference", "unique_material", "equivalent_group", "none"]
    reason: str
