"""Discover structure without treating column-name guesses as approved business mappings."""

import hashlib
import json
from datetime import date, datetime

from supplier_chaser.domain.models import ParsedFact


def cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        raise ValueError("Nested cells require an explicit extraction adapter")
    return str(value)


def table_fact(rows: list[dict], locator: str, row_locators: list[str]) -> ParsedFact:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    if len(rows) > 10000 or len(columns) > 200:
        raise ValueError("Table exceeds review limits")
    if not all(isinstance(key, str) and key.strip() for key in columns):
        raise ValueError("Table requires named columns")
    fingerprint = hashlib.sha256(json.dumps(sorted(columns)).encode()).hexdigest()
    return ParsedFact(
        kind="table",
        locator=locator,
        data={
            "columns": columns,
            "schema_fingerprint": fingerprint,
            "rows": [
                {"locator": position, "values": {key: cell(row.get(key)) for key in columns}}
                for row, position in zip(rows, row_locators, strict=True)
            ],
        },
    )
