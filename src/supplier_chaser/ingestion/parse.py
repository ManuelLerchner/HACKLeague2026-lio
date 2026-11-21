import csv
import io
import json
from html.parser import HTMLParser
from pathlib import PurePosixPath
from zipfile import ZipFile

from openpyxl import load_workbook
from pypdf import PdfReader

from supplier_chaser.domain.models import Confirmation, ParsedFact, POLine, Supplier
from supplier_chaser.ingestion.normalize import normalize_po


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
        rows = payload if isinstance(payload, list) else payload["records"]
        return [record(row, f"record:{i}") for i, row in enumerate(rows, 1)]
    if suffix == ".csv":
        return [
            record(row, f"row:{i}")
            for i, row in enumerate(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))), 2)
        ]
    if suffix == ".xlsx":
        with ZipFile(io.BytesIO(data)) as archive:
            if sum(member.file_size for member in archive.infolist()) > 50_000_000:
                raise ValueError("Workbook expands beyond the parsing limit")
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        facts = []
        try:
            for sheet in workbook:
                rows = sheet.iter_rows(values_only=True)
                headers = next(rows, ())
                for i, values in enumerate(rows, 2):
                    if any(v is not None for v in values):
                        facts.append(
                            record(dict(zip(headers, values, strict=True)), f"{sheet.title}:{i}")
                        )
        finally:
            workbook.close()
        return facts
    if suffix == ".pdf":
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > 100:
            raise ValueError("PDF exceeds the page limit")
        return [
            ParsedFact(kind="text", locator=f"page:{i}", data={"text": p.extract_text() or ""})
            for i, p in enumerate(reader.pages, 1)
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
