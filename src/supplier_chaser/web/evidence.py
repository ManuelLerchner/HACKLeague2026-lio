from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse


def preview_file(path: Path, filename: str) -> FileResponse:
    if not path.is_file():
        raise HTTPException(404, "Original evidence is unavailable")
    media = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".txt": "text/plain",
        ".csv": "text/plain",
        ".md": "text/plain",
        ".json": "text/plain",
        ".html": "text/plain",
    }.get(Path(filename).suffix.lower())
    if media is None:
        raise HTTPException(
            415, "This format has no browser preview; use the source table or download the original"
        )
    return FileResponse(
        path,
        media_type=media,
        filename=Path(filename).name,
        content_disposition_type="inline",
        headers={
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": (
                "default-src 'none'; frame-ancestors 'self'; sandbox allow-same-origin"
            ),
        },
    )
