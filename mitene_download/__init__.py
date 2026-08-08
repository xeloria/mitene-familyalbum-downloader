"""Download and archive media from Mitene (FamilyAlbum) share links."""

from __future__ import annotations

__version__ = "7.0.0"

from .errors import (
    AuthError,
    DownloadError,
    MiteneError,
    ParseError,
    PasswordRequiredError,
    TruncatedDownload,
)
from .models import Album, MediaItem

__all__ = [
    "__version__",
    "Album",
    "AuthError",
    "DownloadError",
    "MediaItem",
    "MiteneError",
    "ParseError",
    "PasswordRequiredError",
    "TruncatedDownload",
    "main",
]


def main() -> int:
    """Console-script entry point (imported lazily to keep startup light).

    Returns the process exit code; the generated wrapper passes it to
    ``sys.exit``, so a run with failures no longer reports success.
    """
    from .cli import main as _main

    return _main()
