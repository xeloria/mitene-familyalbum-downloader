"""SQLite-backed download state.

v6 declared fourteen columns but its ``REPLACE INTO`` wrote four of them, so
every progress update silently nulled ``checksum``, ``file_size``, ``error_log``
and the rest. This module writes what it declares and uses an upsert that only
touches the fields it was given.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite  # type: ignore

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

CACHE_COLUMNS = (
    "cache_filename",
    "status",
    "downloaded_size",
    "media_type",
    "content_type",
    "album_url",
    "uuid",
    "took_at",
    "creation_date",
    "last_modified",
    "file_size",
    "checksum",
    "expected_checksum",
    "download_count",
    "last_accessed",
    "error_log",
    "retry_count",
    "re_download",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DatabaseCache:
    """Tracks per-file download state, and the user's saved album URLs."""

    def __init__(self, db_path: str = "cache.db") -> None:
        self.db_path = db_path

    async def init_db(self) -> None:
        # sqlite will not create intermediate directories, and --db may point
        # somewhere that does not exist yet (e.g. inside a fresh --dest).
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS cache
                   (url TEXT PRIMARY KEY,
                    cache_filename TEXT,
                    status TEXT,
                    downloaded_size INTEGER DEFAULT 0,
                    media_type TEXT,
                    content_type TEXT,
                    album_url TEXT,
                    uuid TEXT,
                    took_at TEXT,
                    creation_date TEXT,
                    last_modified TEXT,
                    file_size INTEGER,
                    checksum TEXT,
                    expected_checksum TEXT,
                    download_count INTEGER DEFAULT 0,
                    last_accessed TEXT,
                    error_log TEXT,
                    retry_count INTEGER DEFAULT 0,
                    re_download BOOLEAN DEFAULT FALSE)"""
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS album_urls (url TEXT PRIMARY KEY)"
            )
            await db.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT)"
            )
            await db.commit()
            await self._migrate(db)

    async def _migrate(self, db: Any) -> None:
        """Bring a pre-existing v6 ``cache.db`` up to the current schema.

        v6 databases lack the columns added here; ``ALTER TABLE ADD COLUMN`` is
        cheap and preserves the download history, so nobody re-fetches an album
        just to upgrade.
        """
        async with db.execute("PRAGMA table_info(cache)") as cursor:
            existing = {row[1] for row in await cursor.fetchall()}
        added = []
        for column, ddl in (
            ("content_type", "TEXT"),
            ("album_url", "TEXT"),
            ("uuid", "TEXT"),
            ("took_at", "TEXT"),
            ("expected_checksum", "TEXT"),
        ):
            if column not in existing:
                await db.execute(f"ALTER TABLE cache ADD COLUMN {column} {ddl}")
                added.append(column)
        if added:
            logger.info("Migrated cache schema, added columns: %s", ", ".join(added))
        await db.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
            (str(SCHEMA_VERSION),),
        )
        await db.commit()

    # -- per-file state ---------------------------------------------------

    async def upsert(self, url: str, **fields: Any) -> None:
        """Insert or update a row, leaving unmentioned columns untouched."""
        payload = {k: v for k, v in fields.items() if k in CACHE_COLUMNS}
        payload.setdefault("last_accessed", _now())
        columns = list(payload)
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{c}=excluded.{c}" for c in columns)
        sql = (
            f"INSERT INTO cache (url, {', '.join(columns)}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(url) DO UPDATE SET {assignments}"
        )
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(sql, (url, *[payload[c] for c in columns]))
            await db.commit()

    async def mark_progress(self, url: str, filename: str, downloaded: int) -> None:
        await self.upsert(
            url, cache_filename=filename, status="partial", downloaded_size=downloaded
        )

    async def mark_complete(
        self,
        url: str,
        filename: str,
        size: int,
        checksum: Optional[str] = None,
        **extra: Any,
    ) -> None:
        await self.upsert(
            url,
            cache_filename=filename,
            status="complete",
            downloaded_size=size,
            file_size=size,
            checksum=checksum,
            last_modified=_now(),
            error_log=None,
            **extra,
        )

    async def mark_failed(self, url: str, error: str, retry_count: int) -> None:
        await self.upsert(
            url, status="failed", error_log=error[:2000], retry_count=retry_count
        )

    async def get_entry(self, url: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM cache WHERE url=?", (url,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def is_complete(self, url: str) -> bool:
        entry = await self.get_entry(url)
        return bool(entry and entry.get("status") == "complete")

    async def find_by_uuid(self, uuid: str) -> List[Dict[str, Any]]:
        """Used by ``--repair`` to locate files written by earlier versions."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM cache WHERE uuid=? OR url LIKE ?",
                (uuid, f"%/{uuid}/%"),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def reset(self, url: str) -> None:
        """Forget a file's progress so the next run starts it from scratch."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM cache WHERE url=?", (url,))
            await db.commit()

    # -- saved albums -----------------------------------------------------

    async def save_album_url(self, url: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO album_urls (url) VALUES (?)", (url,)
            )
            await db.commit()

    async def get_all_album_urls(self) -> List[str]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT url FROM album_urls") as cursor:
                return [str(row[0]) for row in await cursor.fetchall()]

    async def delete_album_url(self, url: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM album_urls WHERE url = ?", (url,))
            await db.commit()
        logger.info("Deleted URL: %s", url)
