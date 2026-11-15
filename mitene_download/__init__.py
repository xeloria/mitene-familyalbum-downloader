"""Download and archive media from Mitene (FamilyAlbum) share links."""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "7.1.0"

from .errors import (
    AlbumNotFoundError,
    AuthError,
    DownloadError,
    InvitationLinkError,
    MiteneError,
    ParseError,
    PasswordRequiredError,
    TruncatedDownload,
)
from .models import Album, MediaItem

# Heavy modules (aiohttp, aiosqlite) are imported on first use, so ``--help``
# and ``--version`` stay fast. ``from mitene_download import process_album``
# works exactly as if these were eager imports.
_LAZY_EXPORTS = {
    "DatabaseCache": ".cache",
    "Options": ".options",
    "Summary": ".album",
    "process_album": ".album",
    "repair_directory": ".album",
    "sync_albums": ".album",
}

__all__ = [
    "__version__",
    "Album",
    "AlbumNotFoundError",
    "AuthError",
    "DatabaseCache",
    "DownloadError",
    "InvitationLinkError",
    "MediaItem",
    "MiteneError",
    "Options",
    "ParseError",
    "PasswordRequiredError",
    "Summary",
    "TruncatedDownload",
    "main",
    "process_album",
    "repair_directory",
    "sync_albums",
]


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name, __name__), name)
    globals()[name] = value  # cache for subsequent lookups
    return value


def main() -> int:
    """Console-script entry point (imported lazily to keep startup light).

    Returns the process exit code; the generated wrapper passes it to
    ``sys.exit``, so a run with failures no longer reports success.
    """
    from .cli import main as _main

    return _main()
