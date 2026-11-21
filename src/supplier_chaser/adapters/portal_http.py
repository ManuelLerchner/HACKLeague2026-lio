"""Contract-configured adapter. No presumed challenge authentication or response shape."""

import mimetypes
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from supplier_chaser.adapters.portal_fixture import validate_path
from supplier_chaser.domain.models import FileMetadata


class PortalContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    openapi_sha256: str
    auth_header: str
    auth_prefix: str = ""
    list_key: str | None = None
    path_key: str
    checksum_key: str
    size_key: str
    media_type_key: str | None = None
    checksum_algorithm: str = "sha256"
    complete_listing: bool = False


class PortalHTTPClient:
    def __init__(self, client: httpx.Client, contract: PortalContract, max_bytes: int) -> None:
        self.client = client
        self.contract = contract
        self.max_bytes = max_bytes
        self.complete_listing = contract.complete_listing

    def list_files(self) -> list[FileMetadata]:
        response = self.client.get("/api/v1/files")
        response.raise_for_status()
        payload = response.json()
        if self.contract.list_key:
            payload = payload[self.contract.list_key]
        if not isinstance(payload, list):
            raise ValueError("Portal listing does not match inspected contract")
        result = []
        for item in payload:
            result.append(
                FileMetadata(
                    path=validate_path(item[self.contract.path_key]),
                    checksum=item[self.contract.checksum_key],
                    size=item[self.contract.size_key],
                    media_type=(
                        item[self.contract.media_type_key]
                        if self.contract.media_type_key
                        else mimetypes.guess_type(item[self.contract.path_key])[0]
                        or "application/octet-stream"
                    ),
                )
            )
        return result

    def download(self, file: FileMetadata) -> bytes:
        path = quote(validate_path(file.path), safe="/")
        with self.client.stream("GET", f"/api/v1/files/{path}") as response:
            response.raise_for_status()
            actual_type = response.headers.get("content-type", "").split(";")[0]
            if actual_type and actual_type not in {file.media_type, "application/octet-stream"}:
                raise ValueError("Downloaded content type differs from listing")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > self.max_bytes:
                    raise ValueError("Portal file exceeds byte limit")
        return bytes(data)
