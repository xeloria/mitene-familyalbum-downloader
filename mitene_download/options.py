"""Run configuration, shared by the CLI and the library API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from .models import DEFAULT_FILENAME_TEMPLATE


@dataclass
class Options:
    dest: str = "files"
    db_path: str = "cache.db"
    password: Optional[str] = None

    # filters
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    media_type: str = "all"  # all | photos | videos
    uploader: List[str] = field(default_factory=list)
    limit: Optional[int] = None

    # behaviour
    concurrency: int = 4
    album_concurrency: int = 1
    max_retries: int = 4
    dry_run: bool = False
    quiet: bool = False
    verbose: bool = False

    # output
    filename_template: str = DEFAULT_FILENAME_TEMPLATE
    comment_format: str = "md"  # md | json | both | none
    sidecar: bool = False
    write_exif: bool = True
    index: bool = False
