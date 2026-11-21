from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from supplier_chaser.domain.models import POLine

LAUNCH_CUTOFF = datetime(2026, 11, 22, 20, tzinfo=ZoneInfo("Europe/Berlin"))


def priority(
    po: POLine, violation: bool, cutoff: datetime
) -> tuple[Literal["P0", "P1", "P2", "P3"], str]:
    if not po.launch_critical:
        return "P2", "Order is not marked launch-critical."
    if po.requested_delivery is None or po.lead_time_hours is None:
        return "P1", "Launch relevance known; date or packing lead time is unknown."
    if po.requested_delivery > cutoff.date():
        return "P2", "Order is scheduled after this launch."
    if violation:
        return "P0", "Confirmed violation on a launch-critical order due before cutoff."
    return "P1", "Launch-critical commitment needs clarification."
