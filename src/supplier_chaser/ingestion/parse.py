import csv
import io
import json
from html.parser import HTMLParser
from itertools import islice
from pathlib import PurePosixPath
from zipfile import ZipFile

from openpyxl import load_workbook
from pypdf import PdfReader

from supplier_chaser.domain.models import Confirmation, ParsedFact, POLine, Supplier
from supplier_chaser.ingestion.normalize import normalize_po
from supplier_chaser.ingestion.tables import table_fact


class PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_html(text: str) -> str:
    parser = PlainText()
    parser.feed(text)
    return " ".join(parser.parts)


def record(row: dict, locator: str) -> ParsedFact:
    kind = row.get("kind", "po" if "EBELN" in row else None)
    data = {k: v for k, v in row.items() if k != "kind"}
    value: Supplier | POLine | Confirmation
    if kind == "supplier":
        value = Supplier.model_validate(data)
    elif kind == "po":
        value = normalize_po(data)
    elif kind == "confirmation":
        value = Confirmation.model_validate(data)
    else:
        raise ValueError("Unsupported record kind")
    return ParsedFact(kind=kind, locator=locator, data=value.model_dump(mode="json"))


def parse(path: str, data: bytes) -> list[ParsedFact]:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".json":
        payload = json.loads(data)
        rows = (
            payload
            if isinstance(payload, list)
            else next(
                (
                    payload[key]
                    for key in ("records", "data", "items")
                    if isinstance(payload.get(key), list)
                ),
                [payload],
            )
        )
        locators = [f"record:{i}" for i in range(1, len(rows) + 1)]
        if all("kind" in row or "EBELN" in row and "CREATED_AT" in row for row in rows):
            return [record(row, position) for row, position in zip(rows, locators, strict=True)]
        return [table_fact(rows, "table:json", locators)]
    if suffix == ".csv":
        text = data.decode("utf-8-sig")
        try:
            dialect = csv.Sniffer().sniff(text[:16384], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)):
            raise ValueError("Duplicate column names require review")
        rows = list(islice(reader, 10001))
        if len(rows) > 10000:
            raise ValueError("CSV exceeds review limits")
        if any(None in row for row in rows):
            raise ValueError("CSV row has more cells than headers")
        locators = [f"row:{i}" for i in range(2, len(rows) + 2)]
        if "kind" in headers or {"EBELN", "CREATED_AT"}.issubset(headers):
            return [record(row, position) for row, position in zip(rows, locators, strict=True)]
        return [table_fact(rows, "table:csv", locators)]
    if suffix == ".xlsx":
        with ZipFile(io.BytesIO(data)) as archive:
            if sum(member.file_size for member in archive.infolist()) > 50_000_000:
                raise ValueError("Workbook expands beyond the parsing limit")
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        facts = []
        try:
            for sheet in workbook:
                if sheet.max_row > 10000 or sheet.max_column > 200:
                    raise ValueError("Worksheet exceeds review limits")
                grid = list(sheet.iter_rows(values_only=True))
                if len(grid) > 10000 or any(len(row) > 200 for row in grid):
                    raise ValueError("Worksheet exceeds review limits")
                width = max(
                    (
                        max((i for i, value in enumerate(row, 1) if value is not None), default=0)
                        for row in grid
                    ),
                    default=0,
                )
                header_index = next(
                    (
                        i
                        for i, row in enumerate(grid[:20])
                        if width
                        and all(isinstance(value, str) and value.strip() for value in row[:width])
                        and len(set(row[:width])) == width
                    ),
                    None,
                )
                if header_index is None:
                    facts.append(
                        ParsedFact(
                            kind="text",
                            locator=f"sheet:{sheet.title}",
                            data={"text": "\n".join(str(row) for row in grid)},
                        )
                    )
                    continue
                headers = grid[header_index][:width]
                if header_index:
                    facts.append(
                        ParsedFact(
                            kind="text",
                            locator=f"{sheet.title}:preamble",
                            data={"text": "\n".join(str(row) for row in grid[:header_index])},
                        )
                    )
                discovered, locators = [], []
                for i, values in enumerate(grid[header_index + 1 :], header_index + 2):
                    if any(v is not None for v in values):
                        row = dict(zip(headers, values[:width], strict=True))
                        if "kind" in headers:
                            facts.append(record(row, f"{sheet.title}:{i}"))
                        else:
                            discovered.append(row)
                            locators.append(f"{sheet.title}:{i}")
                if discovered:
                    table = table_fact(discovered, f"sheet:{sheet.title}", locators)
                    table.data["header_row"] = header_index + 1
                    facts.append(table)
        finally:
            workbook.close()
        return facts
    if suffix == ".pdf":
        pdf = PdfReader(io.BytesIO(data))
        if len(pdf.pages) > 100:
            raise ValueError("PDF exceeds the page limit")
        return [
            ParsedFact(kind="text", locator=f"page:{i}", data={"text": p.extract_text() or ""})
            for i, p in enumerate(pdf.pages, 1)
        ]
    if suffix in {".txt", ".md", ".html"}:
        text = data.decode("utf-8")
        return [
            ParsedFact(
                kind="text",
                locator="text:1",
                data={"text": plain_html(text) if suffix == ".html" else text},
            )
        ]
    return []
