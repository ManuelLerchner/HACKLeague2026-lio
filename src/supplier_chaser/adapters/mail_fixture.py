import json
from pathlib import Path

from supplier_chaser.domain.models import Inbound, Outbound, SendResult


class FixtureMailGateway:
    def __init__(self, root: Path) -> None:
        self.root = root

    def send(self, message: Outbound) -> SendResult:
        outbox = self.root / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        import hashlib

        target = outbox / f"{hashlib.sha256(message.message_id.encode()).hexdigest()}.json"
        if not target.exists():
            target.write_text(message.model_dump_json(indent=2), encoding="utf-8")
        return SendResult(status="sent", detail="Synthetic fixture transport; no external email")

    def receive(self) -> list[Inbound]:
        inbox = self.root / "inbox"
        return [
            Inbound.model_validate(json.loads(p.read_text())) for p in sorted(inbox.glob("*.json"))
        ]
