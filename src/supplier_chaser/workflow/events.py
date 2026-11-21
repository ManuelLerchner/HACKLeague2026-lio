from datetime import datetime

from sqlalchemy.orm import Session

from supplier_chaser.adapters.db import CaseEvent, uid
from supplier_chaser.domain.models import Event


def append_event(
    session: Session,
    aggregate_id: str,
    kind: str,
    payload: dict,
    now: datetime,
    case_id: str | None = None,
) -> None:
    event_id = uid()
    event = Event(
        id=event_id,
        type=kind,
        aggregate_id=aggregate_id,
        occurred_at=now,
        payload=payload,
        idempotency_key=event_id,
    )
    session.add(
        CaseEvent(
            id=event_id,
            case_id=case_id,
            kind=kind,
            payload_json=event.model_dump(mode="json"),
            created_at=now,
        )
    )
