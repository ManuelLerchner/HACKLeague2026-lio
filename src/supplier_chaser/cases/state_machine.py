from supplier_chaser.domain.models import State

TRANSITIONS: dict[State, set[State]] = {
    State.NEW: {State.INVESTIGATING},
    State.INVESTIGATING: {State.DRAFT_READY, State.RECONCILING},
    State.DRAFT_READY: {State.NEEDS_APPROVAL, State.AWAITING_REPLY, State.RECONCILING},
    State.NEEDS_APPROVAL: {State.DRAFT_READY, State.AWAITING_REPLY, State.RECONCILING},
    State.AWAITING_REPLY: {State.RESPONSE_RECEIVED, State.DRAFT_READY, State.RECONCILING},
    State.RESPONSE_RECEIVED: {State.RECONCILING},
    State.RECONCILING: {State.RESOLVED, State.INVESTIGATING, State.DRAFT_READY},
    State.ESCALATED: {State.INVESTIGATING, State.RESPONSE_RECEIVED, State.RECONCILING},
    State.BLOCKED: {State.INVESTIGATING, State.RESPONSE_RECEIVED, State.RECONCILING},
    State.RESOLVED: set(),
    State.CANCELLED: set(),
}


def transition(current: State, target: State, *, adequate_evidence: bool = False) -> State:
    alternatives = {State.BLOCKED, State.ESCALATED, State.CANCELLED}
    allowed = TRANSITIONS[current] | (
        alternatives if current not in {State.RESOLVED, State.CANCELLED} else set()
    )
    if target not in allowed:
        raise ValueError(f"Illegal transition {current} -> {target}")
    if target == State.RESOLVED and not adequate_evidence:
        raise ValueError("Resolution requires adequate confirmation evidence")
    return target
