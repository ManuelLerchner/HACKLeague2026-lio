from supplier_chaser.domain.models import POLine

# These are explicit synthetic contracts until genuine SAP exports are inspected.
SAP_LEGACY = {
    "EBELN": "po_number",
    "EBELP": "line_number",
    "LIFNR": "supplier_id",
    "MATNR": "part_number",
    "MENGE": "ordered_qty",
    "MEINS": "unit",
    "EINDT": "requested_delivery",
    "CREATED_AT": "ordered_at",
}


def normalize_po(row: dict) -> POLine:
    normalized = {SAP_LEGACY.get(k, k): v for k, v in row.items() if k != "kind"}
    for key in ("po_number", "line_number", "supplier_id", "part_number"):
        if key in normalized:
            normalized[key] = str(normalized[key])
    for key in ("unit", "requested_delivery", "lead_time_hours"):
        if normalized.get(key) == "":
            normalized[key] = None
    return POLine.model_validate(normalized)
