"""Orchestration: walk an album, download what's new, write metadata.

Downloads start while pagination is still in flight (v6 walked every page first
and only then began fetching), which makes ``--limit`` cheap and gets bytes
moving on large albums immediately.
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp  # type: ignore

from .api import AlbumClient, normalize_album_url
from .cache import DatabaseCache
from .download import (
    DownloadResult,
    Outcome,
    _existing_media,
    _hash_existing,
    download_media,
)
from .errors import MiteneError
from .metadata import apply_metadata, sniff_extension, write_comments, write_sidecar
from .models import Album, MediaItem
from .options import Options

logger = logging.getLogger(__name__)


@dataclass
class Summary:
    """What a run actually did -- printed at the end and used for the exit code."""

    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    filtered: int = 0
    planned: int = 0
    renamed: int = 0
    bytes_written: int = 0
    comment_files: int = 0
    failures: List[str] = field(default_factory=list)
    # Populated only when index writing is deferred to the caller (sync mode),
    # so a shared destination gets one combined index instead of a race.
    entries: List[Tuple[MediaItem, Path]] = field(
        default_factory=list, repr=False, compare=False
    )

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
        self.planned += other.planned
        self.renamed += other.renamed
        self.bytes_written += other.bytes_written
        self.comment_files += other.comment_files
        self.failures.extend(other.failures)
        self.entries.extend(other.entries)

    def to_dict(self) -> Dict[str, Any]:
        """Machine-readable form used by ``--json-log``."""
        return {
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "filtered": self.filtered,
            "planned": self.planned,
            "renamed": self.renamed,
            "bytes_written": self.bytes_written,
            "comment_files": self.comment_files,
            "failures": list(self.failures),
        }

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
        if self.planned:
            lines.insert(2, "  Planned    : %d (dry run)" % self.planned)
        if self.renamed:
            lines.insert(2, "  Renamed    : %d" % self.renamed)
        for failure in self.failures[:20]:
            lines.append(f"      - {failure}")
        if len(self.failures) > 20:
            lines.append(f"      ... and {len(self.failures) - 20} more")
        return "\n".join(lines)


class ItemBudget:
    """A global cap shared by every album in ``--sync``/``--watch``.

    ``--limit`` used to be applied per album, so ``--sync --limit 10`` could
    fetch 10 items from each of ten albums. One budget passed to every
    ``process_album`` makes the cap mean what it says, even with
    ``--album-concurrency`` greater than one.
    """

    def __init__(self, limit: int) -> None:
        self.remaining = max(0, limit)
        self._lock = asyncio.Lock()

    async def take(self) -> bool:
        async with self._lock:
            if self.remaining <= 0:
                return False
            self.remaining -= 1
            return True


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
    album_url: str,
    options: Options,
    db_cache: Optional[DatabaseCache] = None,
    budget: Optional[ItemBudget] = None,
    defer_index: bool = False,
) -> Summary:
    """Download one album according to ``options``.

    ``budget`` shares the ``--limit`` across albums; ``defer_index`` leaves
    index generation to the caller so concurrent albums do not overwrite each
    other's ``index.html``.
    """
    album_url = normalize_album_url(album_url)
    db_cache = db_cache or DatabaseCache(options.db_path)
    if not options.dry_run:
        # A dry run should not create cache.db either.
        await db_cache.init_db()

    destination = Path(options.dest)
    if not options.dry_run:
        # A dry run must not create so much as a directory.
        destination.mkdir(parents=True, exist_ok=True)

    summary = Summary()
    # Items paired with the path they actually landed on. The index must not
    # re-derive filenames: the extension can come from the response headers,
    # which the item alone does not know.
    entries: List[Tuple[MediaItem, Path]] = []
    queue: asyncio.Queue = asyncio.Queue(maxsize=options.concurrency * 4)
    connector = aiohttp.TCPConnector(limit_per_host=options.concurrency)

    item_limit = options.limit if (options.limit or 0) > 0 else None
    if budget is None and item_limit:
        budget = ItemBudget(item_limit)
    complete_pass = True
    # A filtered pass skips whole categories of media, so it must not claim the
    # album is fully archived -- incremental mode would then skip them forever.
    filters_active = bool(
        options.start_date
        or options.end_date
        or options.uploader
        or options.media_type != "all"
    )

    async with aiohttp.ClientSession(connector=connector) as session:
        client = AlbumClient(
            session,
            album_url,
            options.password,
            max_retries=options.max_retries,
            request_timeout=options.request_timeout,
        )
        logger.info("Processing album %s", album_url)

        # Incremental mode is only safe once a previous pass reached the end of
        # the album; otherwise older pages may never have been fetched.
        can_stop_early = (
            options.incremental
            and not options.dry_run
            and await db_cache.is_album_complete(album_url)
        )

        async def page_is_archived(page: Album) -> bool:
            for candidate in page.items:
                entry = await db_cache.get_entry(
                    client.download_url(candidate.uuid)
                )
                if not entry or entry.get("status") != "complete":
                    return False
                directory = (
                    Path(options.dest) / candidate.kind / candidate.year_month
                )
                # Same rules as a normal skip: exists, right size, and tolerant
                # of a --repair rename.
                if (
                    _existing_media(
                        directory,
                        candidate,
                        options.filename_template,
                        entry,
                    )
                    is None
                ):
                    return False
            return True

        async def produce() -> None:
            nonlocal complete_pass
            try:
                async for page in client.pages():
                    if can_stop_early and page.items and await page_is_archived(page):
                        logger.info(
                            "Incremental: page %d is already archived; stopping.",
                            page.current_page,
                        )
                        return
                    for item in page.items:
                        if not _passes_filters(item, options):
                            summary.filtered += 1
                            continue
                        if budget is not None and not await budget.take():
                            complete_pass = False
                            return
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
                    path = await _handle_item(
                        session, client, db_cache, item, options, summary
                    )
                    if path is not None:
                        entries.append((item, path))
                finally:
                    queue.task_done()

        producer = asyncio.create_task(produce())
        consumers = [
            asyncio.create_task(consume()) for _ in range(options.concurrency)
        ]
        tasks = [producer, *consumers]
        try:
            # Wait for completion *or* the first failure. Awaiting the producer
            # first could block forever on a full queue if a consumer died.
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in done:
                task.result()  # re-raise the first consumer/producer failure
            if pending:
                await asyncio.gather(*pending)
        except BaseException:
            for task in tasks:
                task.cancel()
            raise

    if options.index and not options.dry_run and entries:
        if defer_index:
            # The caller writes one index covering every album in this run.
            summary.entries = entries
        else:
            write_index(destination, entries, options)

    if complete_pass and not options.dry_run and not filters_active:
        await db_cache.mark_album_complete(album_url)

    return summary


async def _handle_item(
    session: aiohttp.ClientSession,
    client: AlbumClient,
    db_cache: DatabaseCache,
    item: MediaItem,
    options: Options,
    summary: Summary,
) -> Optional[Path]:
    """Process one item; returns the path it occupies on disk, if any."""
    destination = Path(options.dest)
    directory = destination / item.kind / item.year_month
    url = client.download_url(item.uuid)
    planned_path = directory / item.filename(options.filename_template)

    if options.comment_format != "none" and item.comments:
        comment_dir = destination / "comments" / item.year_month
        stem = Path(item.filename(options.filename_template)).stem
        if not options.dry_run:
            summary.comment_files += len(
                write_comments(comment_dir, stem, item, options.comment_format)
            )

    if options.dry_run:
        logger.info("[dry-run] would download %s -> %s", item.uuid, planned_path)
        summary.planned += 1
        return planned_path

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
        timeout=options.timeout,
        on_auth_error=client.reauthenticate,
    )
    summary.record(result)

    if result.outcome is Outcome.FAILED or result.path is None:
        return None

    if result.outcome is Outcome.DOWNLOADED:
        apply_metadata(
            result.path,
            item,
            write_exif_data=options.write_exif,
            use_exiftool=options.use_exiftool,
        )
        if options.sidecar:
            write_sidecar(result.path, item)
        await _record_final_state(db_cache, url, result)
    return result.path


async def _record_final_state(
    db_cache: DatabaseCache, url: str, result: DownloadResult
) -> None:
    """Store the size/checksum of the file *after* metadata rewriting.

    piexif rewrites JPEGs while inserting EXIF, so the downloaded byte count is
    no longer the file's size. Recording that stale number made the size check
    re-download every photo on the next run.
    """
    if result.path is None:
        return
    try:
        final_size = result.path.stat().st_size
    except OSError as exc:
        logger.warning("Could not stat %s after metadata: %s", result.path, exc)
        return

    checksum = result.checksum
    if final_size != result.bytes_written:
        try:
            checksum = (await _hash_existing(result.path)).hexdigest()
        except OSError as exc:
            logger.debug("Could not re-hash %s: %s", result.path, exc)

    await db_cache.mark_metadata_applied(url, final_size, checksum)


# -- index ----------------------------------------------------------------


def _relative(destination: Path, path: Path) -> str:
    try:
        return path.relative_to(destination).as_posix()
    except ValueError:
        pass
    try:
        # A cache row written by an earlier run may hold an absolute path while
        # --dest is relative (or vice versa). Compare resolved forms before
        # giving up and embedding an absolute link.
        return path.resolve().relative_to(destination.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file so an interrupted run cannot truncate it."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_index(
    destination: Path,
    entries: Sequence[Tuple[MediaItem, Path]],
    options: Options,
) -> None:
    """Write ``index.csv`` and a self-contained ``index.html`` gallery."""
    rows = [(item, _relative(destination, path)) for item, path in entries]

    # Sort on the epoch value: a datetime key would compare None to datetime,
    # and could mix aware with naive values if a page ever omits the offset.
    def _sort_key(entry: Tuple[MediaItem, str]) -> float:
        item = entry[0]
        return item.took_at.timestamp() if item.took_at else float("-inf")

    rows.sort(key=_sort_key, reverse=True)

    csv_path = destination / "index.csv"
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "uuid", "took_at", "media_type", "content_type", "file",
            "width", "height", "device_model", "latitude", "longitude",
            "duration_ms", "user_id", "comments",
        ]
    )
    for item, relative in rows:
        writer.writerow(
            [
                item.uuid,
                item.took_at.isoformat() if item.took_at else "",
                item.media_type,
                item.content_type or "",
                relative,
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
    _atomic_write(csv_path, buffer.getvalue())

    figures = []
    for item, relative in rows:
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
        figures.append(
            f'<figure>{preview}<figcaption>'
            f'{html.escape(item.took_at.strftime("%Y-%m-%d %H:%M") if item.took_at else "unknown")}'
            f'<div class="meta">{html.escape(item.device_model or "")}</div>'
            f'<div class="comments">{comments}</div></figcaption></figure>'
        )

    _atomic_write(
        destination / "index.html",
        _INDEX_TEMPLATE.replace("__COUNT__", str(len(rows))).replace(
            "__ROWS__", "\n".join(figures)
        ),
    )
    logger.info("Wrote index.html and index.csv (%d items)", len(rows))


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


async def repair_directory(
    destination: Path,
    dry_run: bool = False,
    db_path: Optional[str] = None,
) -> Summary:
    """Fix files written by earlier versions with the wrong extension.

    v6 named files from the rendition URL, so photos landed as ``.webp`` and
    every video as ``.jpg`` even though the bytes were JPEG and MP4. This walks
    the tree, sniffs the magic bytes, and renames in place -- no re-downloading.

    The cache is updated as files are renamed. Without that, every repaired
    file would look missing on the next normal run and be downloaded again.
    """
    summary = Summary()
    renames: List[Tuple[str, str]] = []
    cache: Optional[DatabaseCache] = None
    if db_path and not dry_run:
        cache = DatabaseCache(db_path)
        await cache.init_db()

    for path in sorted(destination.rglob("*")):
        if not path.is_file():
            continue
        # Never touch sidecars or in-flight downloads: a ``.jpg.part`` still
        # has JPEG magic bytes and would otherwise be renamed mid-download.
        if path.suffix.lower() in {".json", ".part", ".tmp"}:
            continue
        actual = sniff_extension(path)
        if not actual:
            continue
        if actual == path.suffix.lower():
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
            if cache is not None:
                await cache.update_filename(str(path), str(target))
            renames.append(
                (_relative(destination, path), _relative(destination, target))
            )
            logger.info("Renamed %s -> %s", path.name, target.name)
        summary.renamed += 1
    if renames:
        # Keep an already-written gallery usable; otherwise every link in it
        # points at a filename that no longer exists.
        _update_index_paths(destination, renames)
    if summary.renamed:
        verb = "would rename" if dry_run else "renamed"
        logger.info("%s %d file(s).", verb.capitalize(), summary.renamed)
    return summary


def _update_index_paths(
    destination: Path, renames: Sequence[Tuple[str, str]]
) -> None:
    """Rewrite media paths in ``index.csv``/``index.html`` after a repair."""
    mapping = dict(renames)

    csv_path = destination / "index.csv"
    if csv_path.exists():
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
        except OSError as exc:
            logger.warning("Could not read %s: %s", csv_path, exc)
            rows = []
        if rows:
            header = rows[0]
            try:
                column = header.index("file")
            except ValueError:
                column = -1
            if column >= 0:
                for row in rows[1:]:
                    if len(row) > column and row[column] in mapping:
                        row[column] = mapping[row[column]]
                buffer = io.StringIO()
                csv.writer(buffer).writerows(rows)
                _atomic_write(csv_path, buffer.getvalue())

    html_path = destination / "index.html"
    if html_path.exists():
        try:
            text = html_path.read_text("utf-8")
        except OSError as exc:
            logger.warning("Could not read %s: %s", html_path, exc)
            return
        updated = text
        for old, new in mapping.items():
            updated = updated.replace(
                f'src="{html.escape(old)}"', f'src="{html.escape(new)}"'
            )
        if updated != text:
            _atomic_write(html_path, updated)


async def sync_albums(urls: List[str], options: Options) -> Summary:
    """Run several albums, sequentially or concurrently."""
    db_cache = DatabaseCache(options.db_path)
    await db_cache.init_db()
    total = Summary()
    item_limit = options.limit if (options.limit or 0) > 0 else None
    budget = ItemBudget(item_limit) if item_limit else None

    async def run(url: str) -> Summary:
        try:
            return await process_album(
                url, options, db_cache, budget=budget, defer_index=True
            )
        except MiteneError as exc:
            logger.error("Album %s failed: %s", url, exc)
            message = str(exc)
        except Exception as exc:  # noqa: BLE001 - one bad album must not stop the rest
            logger.exception("Album %s failed unexpectedly: %s", url, exc)
            message = f"{type(exc).__name__}: {exc}"
        failed = Summary()
        failed.failed += 1
        failed.failures.append(f"{url}: {message}")
        return failed

    semaphore = asyncio.Semaphore(max(1, options.album_concurrency))

    async def guarded(url: str) -> Summary:
        async with semaphore:
            return await run(url)

    results = await asyncio.gather(*(guarded(url) for url in urls))

    for result in results:
        total.merge(result)

    # One index for the whole run. Writing it per album let the last writer
    # silently replace every other album's gallery.
    if options.index and not options.dry_run and total.entries:
        write_index(Path(options.dest), total.entries, options)

    return total
