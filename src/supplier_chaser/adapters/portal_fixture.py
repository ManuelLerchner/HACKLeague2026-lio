import hashlib
import mimetypes
from pathlib import Path, PurePosixPath

from supplier_chaser.domain.models import FileMetadata


def validate_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
        raise ValueError("Unsafe portal path")
    if not value or str(path) != value:
        raise ValueError("Noncanonical portal path")
    return value


class FixturePortalClient:
    complete_listing = True

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def list_files(self) -> list[FileMetadata]:
        return [
            FileMetadata(
                path=p.relative_to(self.root).as_posix(),
                checksum=hashlib.sha256(p.read_bytes()).hexdigest(),
                size=p.stat().st_size,
                media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream",
            )
            for p in sorted(self.root.rglob("*"))
            if p.is_file() and not p.is_symlink()
        ]

    def download(self, file: FileMetadata) -> bytes:
        path = (self.root / validate_path(file.path)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("File escapes fixture root")
        return path.read_bytes()
