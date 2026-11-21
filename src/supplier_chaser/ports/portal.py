from typing import Protocol

from supplier_chaser.domain.models import FileMetadata


class PortalClient(Protocol):
    complete_listing: bool

    def list_files(self) -> list[FileMetadata]: ...
    def download(self, file: FileMetadata) -> bytes: ...
