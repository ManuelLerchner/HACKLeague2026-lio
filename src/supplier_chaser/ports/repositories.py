from typing import Protocol

from supplier_chaser.domain.models import Claim, POLine


class CommitmentReader(Protocol):
    def get_po_line(self, line_id: str) -> POLine: ...
    def get_claims(self, line_id: str) -> list[Claim]: ...
