"""Approved mappings translate source columns into the existing domain contracts."""

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from supplier_chaser.domain.models import Confirmation, POLine, Supplier


class MappingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["supplier", "po", "confirmation"]
    columns: dict[str, str] = {}
    constants: dict[str, str | int | bool | None] = {}
    date_formats: dict[str, str] = {}
    value_maps: dict[str, dict[str, str]] = {}
    filters: dict[str, list[str]] = {}
    decimal_separator: Literal[".", ","] = "."
    thousands_separator: Literal[",", ".", " "] | None = None
    trust_contacts: bool = False
    note: str = Field(default="", max_length=2000)


ALIASES = {
    "po_number": {"ponumber", "purchaseorder", "ordernumber", "ebeln"},
    "line_number": {"linenumber", "poline", "orderline", "ebelp"},
    "supplier_id": {"supplierid", "vendorid", "lifnr"},
    "part_number": {"partnumber", "materialid", "componentid", "matnr"},
    "ordered_qty": {"orderedqty", "quantity", "qty", "menge"},
    "unit": {"unit", "uom", "meins"},
    "requested_delivery": {"requesteddelivery", "requesteddate", "eindt"},
    "ordered_at": {"orderedat", "orderdate", "createdat", "bedat"},
    "id": {"id", "supplierid", "vendorid"},
    "name": {"name", "suppliername", "vendorname"},
    "email": {"email", "contactemail", "supplieremail"},
    "language": {"language", "preferredlanguage"},
    "document_id": {"documentid", "confirmationid"},
    "quantity": {"quantity", "qty", "confirmedquantity"},
    "promised_date": {"promiseddate", "confirmeddate", "deliverydate"},
}
FIELDS = {
    "supplier": set(Supplier.model_fields),
    "po": set(POLine.model_fields),
    "confirmation": {
        "document_id",
        "po_number",
        "line_number",
        "supplier_id",
        "unit",
        "quantity",
        "promised_date",
        "intent",
        "terms",
    },
}


def suggest(columns: list[str], kind: Literal["supplier", "po", "confirmation"]) -> dict:
    mappings = {}
    for target in FIELDS[kind]:
        aliases = ALIASES.get(target, {target.replace("_", "")})
        candidates = [
            column for column in columns if re.sub(r"\W|_", "", column).lower() in aliases
        ]
        if len(candidates) == 1:
            mappings[target] = candidates[0]
    return MappingSpec(kind=kind, columns=mappings).model_dump()


def mapped_records(table: dict, spec: MappingSpec) -> list[tuple[str, dict]]:
    available = set(table["columns"])
    if not set(spec.columns.values()).union(spec.filters).issubset(available):
        raise ValueError("Mapping refers to missing source columns; review the changed schema")
    targets = set(spec.columns) | set(spec.constants)
    if not targets.issubset(FIELDS[spec.kind]) or set(spec.columns) & set(spec.constants):
        raise ValueError("Mapping has unknown or duplicate target fields")
    if not (set(spec.date_formats) | set(spec.value_maps)).issubset(targets):
        raise ValueError("Transforms must reference mapped fields")
    if spec.decimal_separator == spec.thousands_separator:
        raise ValueError("Decimal and thousands separators must differ")
    if spec.trust_contacts and spec.kind != "supplier":
        raise ValueError("Contact trust only applies to supplier metadata")
    records = []
    for row in table["rows"]:
        raw = row["values"]
        if any(raw.get(column) not in accepted for column, accepted in spec.filters.items()):
            continue
        data = {field: raw[column] for field, column in spec.columns.items()} | spec.constants
        try:
            for field, values in spec.value_maps.items():
                data[field] = values.get(str(data[field]), data[field])
            for field, pattern in spec.date_formats.items():
                if data[field] not in ("", None):
                    data[field] = datetime.strptime(str(data[field]), pattern).date().isoformat()
            for field in ("ordered_qty", "quantity"):
                if field in data:
                    number = str(data[field])
                    if spec.thousands_separator:
                        number = number.replace(spec.thousands_separator, "")
                    data[field] = str(Decimal(number.replace(spec.decimal_separator, ".")))
            for field in ("unit", "requested_delivery", "promised_date"):
                if data.get(field) == "":
                    data[field] = None
            value: POLine | Supplier | Confirmation
            if spec.kind == "po":
                data.setdefault("unit", None)
                data.setdefault("requested_delivery", None)
                value = POLine.model_validate(data)
            elif spec.kind == "supplier":
                value = Supplier.model_validate(data)
            else:
                quantity = data.pop("quantity", None)
                promised_date = data.pop("promised_date", None)
                data.setdefault("unit", None)
                value = Confirmation.model_validate(
                    {
                        **data,
                        "deliveries": [{"quantity": quantity, "promised_date": promised_date}],
                        "evidence_quote": json.dumps(raw, ensure_ascii=False),
                    }
                )
        except (ValueError, TypeError, InvalidOperation) as error:
            raise ValueError(
                f"Invalid mapped record at {row['locator']}; "
                "check required fields, dates and numbers"
            ) from error
        records.append((row["locator"], value.model_dump(mode="json")))
    if not records:
        raise ValueError("Mapping selects no records")
    return records
