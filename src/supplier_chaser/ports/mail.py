from typing import Protocol

from supplier_chaser.domain.models import Inbound, Outbound, SendResult


class MailGateway(Protocol):
    def send(self, message: Outbound) -> SendResult: ...
    def receive(self) -> list[Inbound]: ...
