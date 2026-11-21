import re

from supplier_chaser.domain.models import Inbound


def case_marker(subject: str) -> str | None:
    markers = set(re.findall(r"\[SC:([0-9a-f-]{36})\]", subject))
    return next(iter(markers)) if len(markers) == 1 else None


def correlate(message: Inbound, outbound: list[tuple[str, str, str, str]]) -> str | None:
    references = set(message.references + ([message.in_reply_to] if message.in_reply_to else []))
    marker = case_marker(message.subject)
    referenced = {case_id for external_id, case_id, _, _ in outbound if external_id in references}
    if referenced and marker and marker not in referenced:
        return None
    candidates = {
        case_id
        for external_id, case_id, supplier_email, po in outbound
        if supplier_email.lower() == message.sender.lower()
        and ((external_id in references) or (case_id == marker and po in message.body))
    }
    return next(iter(candidates)) if len(candidates) == 1 else None
