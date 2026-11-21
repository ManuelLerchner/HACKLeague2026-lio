from typing import Protocol

from supplier_chaser.domain.documents import DocumentItems
from supplier_chaser.domain.models import Confirmation, Draft, POLine, Supplier


class LLMExtractor(Protocol):
    model_version: str

    def extract_confirmation(self, text: str) -> Confirmation: ...

    def extract_document(self, text: str) -> DocumentItems: ...


class LLMDrafter(Protocol):
    model_version: str

    def draft_supplier_question(self, po: POLine, supplier: Supplier, reason: str) -> Draft: ...
