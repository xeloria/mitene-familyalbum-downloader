"""Orchestration: walk an album, download what's new, write metadata.

Downloads start while pagination is still in flight (v6 walked every page first
and only then began fetching), which makes ``--limit`` cheap and gets bytes
moving on large albums immediately.
"""

from __future__ import annotations

import asyncio
import csv
import html
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, List, Optional

import aiohttp  # type: ignore

from .api import AlbumClient, normalize_album_url
from .cache import DatabaseCache
from .download import DownloadResult, Outcome, download_media
from .errors import MiteneError
from .metadata import apply_metadata, sniff_extension, write_comments, write_sidecar
from .models import MediaItem
from .options import Options

logger = logging.getLogger(__name__)


@dataclass
class Summary:
    """What a run actually did -- printed at the end and used for the exit code."""

    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    filtered: int = 0
    bytes_written: int = 0
    comment_files: int = 0
    failures: List[str] = field(default_factory=list)

    def record(self, result: DownloadResult) -> None:
        if result.outcome is Outcome.DOWNLOADED:
            self.downloaded += 1
            self.bytes_written += result.bytes_written
        elif result.outcome is Outcome.SKIPPED:
            self.skipped += 1
        else:
            self.failed += 1
            self.failures.append(f"{result.item.uuid}: {result.error}")

    def merge(self, other: "Summary") -> None:
        self.downloaded += other.downloaded
        self.skipped += other.skipped
        self.failed += other.failed
        self.filtered += other.filtered
        self.bytes_written += other.bytes_written
        self.comment_files += other.comment_files
        self.failures.extend(other.failures)

    def render(self) -> str:
        size_mb = self.bytes_written / (1024 * 1024)
        lines = [
            "",
            "  Downloaded : %d (%.1f MiB)" % (self.downloaded, size_mb),
            "  Skipped    : %d (already complete)" % self.skipped,
            "  Filtered   : %d" % self.filtered,
            "  Comments   : %d file(s)" % self.comment_files,
            "  Failed     : %d" % self.failed,
        ]
        for failure in self.failures[:20]:
            lines.append(f"      - {failure}")
        if len(self.failures) > 20:
            lines.append(f"      ... and {len(self.failures) - 20} more")
        return "\n".join(lines)


def _passes_filters(item: MediaItem, options: Options) -> bool:
    if options.media_type == "photos" and item.is_video:
        return False
    if options.media_type == "videos" and not item.is_video:
        return False
    if options.uploader and item.user_id not in options.uploader:
        return False
    if options.start_date or options.end_date:
        # Undated items are excluded only when a date filter is explicitly set,
        # since "no date" cannot satisfy a range either way.
        if item.took_at is None:
            return False
        taken: date = item.took_at.date()
        if options.start_date and taken < options.start_date:
            return False
        if options.end_date and taken > options.end_date:
            return False
    return True


async def process_album(
    album_url: str, options: Options, db_cache: Optional[DatabaseCache] = None
) -> Summary:
    """Download one album according to ``options``."""
    album_url = normalize_album_url(album_url)
    db_cache = db_cache or DatabaseCache(options.db_path)
    assert db_cache is not None
    await db_cache.init_db()

    destination = Path(options.dest)
    destination.mkdir(parents=True, exist_ok=True)

    summary = Summary()
    seen: List[MediaItem] = []
    queue: asyncio.Queue = asyncio.Queue(maxsize=options.concurrency * 4)
    connector = aiohttp.TCPConnector(limit_per_host=options.concurrency)

    async with aiohttp.ClientSession(connector=connector) as session:
        client = AlbumClient(session, album_url, options.password)
        logger.info("Processing album %s", album_url)

        async def produce() -> None:
            count = 0
            try:
                async for item in client.items():
                    if not _passes_filters(item, options):
                        summary.filtered += 1
                        continue
                    if options.limit and count >= options.limit:
                        break
                    seen.append(item)
                    count += 1
                    await queue.put(item)
            finally:
                for _ in range(options.concurrency):
                    await queue.put(None)

        async def consume() -> None:
            while True:
                item = await queue.get()
                try:
                    if item is None:
                        return
                    await _handle_item(session, client, db_cache, item, options, summary)
                finally:
                    queue.task_done()

        producer = asyncio.create_task(produce())
        consumers = [
            asyncio.create_task(consume()) for _ in range(options.concurrency)
        ]
        try:
            await producer
            await asyncio.gather(*consumers)
        except BaseException:
            for task in [producer, *consumers]:
                task.cancel()
            raise

    if options.index and seen:
        write_index(destination, seen, options)

    return summary


async def _handle_item(
    session: Any,
    client: AlbumClient,
    db_cache: DatabaseCache,
    item: MediaItem,
    options: Options,
    summary: Summary,
) -> None:
    destination = Path(options.dest)
    directory = destination / item.kind / item.year_month
    url = client.download_url(item.uuid)

    if options.comment_format != "none" and item.comments:
        comment_dir = destination / "comments" / item.year_month
        stem = Path(item.filename(options.filename_template)).stem
        if not options.dry_run:
            summary.comment_files += len(
                write_comments(comment_dir, stem, item, options.comment_format)
            )

    if options.dry_run:
        logger.info(
            "[dry-run] would download %s -> %s",
            item.uuid,
            directory / item.filename(options.filename_template),
        )
        summary.downloaded += 1
        return

    result = await download_media(
        session,
        db_cache,
        item,
        url,
        directory,
        options.filename_template,
        album_url=client.album_url,
        quiet=options.quiet,
        max_retries=options.max_retries,
    )
    summary.record(result)

    if result.outcome is Outcome.DOWNLOADED and result.path is not None:
        _path = result.path
        apply_metadata(_path, item, write_exif_data=options.write_exif)
        if options.sidecar:
            write_sidecar(_path, item)


# -- index ----------------------------------------------------------------


def write_index(destination: Path, items: List[MediaItem], options: Options) -> None:
    """Write ``index.csv`` and a self-contained ``index.html`` gallery."""
    csv_path = destination / "index.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "uuid", "took_at", "media_type", "content_type", "file",
                "width", "height", "device_model", "latitude", "longitude",
                "duration_ms", "user_id", "comments",
            ]
        )
        for item in items:
            relative = Path(item.kind) / item.year_month / item.filename(
                options.filename_template
            )
            writer.writerow(
                [
                    item.uuid,
                    item.took_at.isoformat() if item.took_at else "",
                    item.media_type,
                    item.content_type or "",
                    relative.as_posix(),
                    item.width or "",
                    item.height or "",
                    item.device_model or "",
                    item.latitude if item.has_location else "",
                    item.longitude if item.has_location else "",
                    item.video_duration_ms or "",
                    item.user_id or "",
                    len([c for c in item.comments if not c.is_deleted]),
                ]
            )

    # Sort on the epoch value: a datetime key would compare None to datetime,
    # and could mix aware with naive values if a page ever omits the offset.
    def _sort_key(item: MediaItem) -> float:
        return item.took_at.timestamp() if item.took_at else float("-inf")

    rows = []
    for item in sorted(items, key=_sort_key, reverse=True):
        relative = (
            Path(item.kind) / item.year_month / item.filename(options.filename_template)
        ).as_posix()
        comments = "<br>".join(
            f"<b>{html.escape(c.nickname)}</b>: {html.escape(c.body)}"
            for c in item.comments
            if not c.is_deleted
        )
        preview = (
            f'<video src="{html.escape(relative)}" controls preload="none"></video>'
            if item.is_video
            else f'<img loading="lazy" src="{html.escape(relative)}" alt="">'
        )
        rows.append(
            f'<figure>{preview}<figcaption>'
            f'{html.escape(item.took_at.strftime("%Y-%m-%d %H:%M") if item.took_at else "unknown")}'
            f'<div class="meta">{html.escape(item.device_model or "")}</div>'
            f'<div class="comments">{comments}</div></figcaption></figure>'
        )

    (destination / "index.html").write_text(
        _INDEX_TEMPLATE.replace("__COUNT__", str(len(items))).replace(
            "__ROWS__", "\n".join(rows)
        ),
        encoding="utf-8",
    )
    logger.info("Wrote index.html and index.csv (%d items)", len(items))


_INDEX_TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<title>FamilyAlbum archive</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; background: #111; color: #eee; }
  h1 { font-weight: 500; }
  .grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }
  figure { margin: 0; background: #1c1c1c; border-radius: 8px; overflow: hidden; }
  img, video { width: 100%; aspect-ratio: 1; object-fit: cover; display: block; background: #000; }
  figcaption { padding: .5rem .6rem; font-size: .8rem; }
  .meta { color: #999; font-size: .72rem; }
  .comments { margin-top: .4rem; color: #cfc; font-size: .75rem; }
</style>
<h1>FamilyAlbum archive &mdash; __COUNT__ items</h1>
<div class="grid">
__ROWS__
</div>
"""


# -- repair ---------------------------------------------------------------


def repair_directory(destination: Path, dry_run: bool = False) -> Summary:
    """Fix files written by earlier versions with the wrong extension.

    v6 named files from the rendition URL, so photos landed as ``.webp`` and
    every video as ``.jpg`` even though the bytes were JPEG and MP4. This walks
    the tree, sniffs the magic bytes, and renames in place -- no re-downloading.
    """
    summary = Summary()
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path.suffix.lower() == ".json":
            continue
        actual = sniff_extension(path)
        if not actual or actual == path.suffix.lower():
            summary.skipped += 1
            continue
        target = path.with_suffix(actual)
        counter = 1
        while target.exists() and target != path:
            target = path.with_name(f"{path.stem}-{counter}{actual}")
            counter += 1
        if dry_run:
            logger.info("[dry-run] would rename %s -> %s", path.name, target.name)
        else:
            path.rename(target)
            logger.info("Renamed %s -> %s", path.name, target.name)
        summary.downloaded += 1
    return summary


async def sync_albums(urls: List[str], options: Options) -> Summary:
    """Run several albums, sequentially or concurrently."""
    db_cache = DatabaseCache(options.db_path)
    await db_cache.init_db()
    total = Summary()

    async def run(url: str) -> Summary:
        try:
            return await process_album(url, options, db_cache)
        except MiteneError as exc:
            logger.error("Album %s failed: %s", url, exc)
            failed = Summary()
            failed.failed += 1
            failed.failures.append(f"{url}: {exc}")
            return failed

    semaphore = asyncio.Semaphore(max(1, options.album_concurrency))

    async def guarded(url: str) -> Summary:
        async with semaphore:
            return await run(url)

    results = await asyncio.gather(*(guarded(url) for url in urls))

    for result in results:
        total.merge(result)
    return total
