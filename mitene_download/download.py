"""Streaming downloads with safe resume and integrity verification.

Partial files are discovered by name pattern rather than reconstructed from the
item alone. The extension is only known after the server's ``Content-Type`` is
seen, so a name rebuilt from the payload can point at a different ``.part``
file than the one actually being written -- which is how an interrupted
download could leave an orphaned partial behind.
"""

from __future__ import annotations

import asyncio
import errno
import glob
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import aiofiles  # type: ignore
import aiohttp  # type: ignore
from tqdm import tqdm  # type: ignore

from .cache import DatabaseCache
from .errors import MiteneError, TruncatedDownload
from .models import MediaItem
from .retry import backoff_delay

logger = logging.getLogger(__name__)

PART_SUFFIX = ".part"
CHUNK_SIZE = 1 << 16  # 64 KiB
CACHE_FLUSH_BYTES = 1 << 20  # persist progress about once per MiB

_CONTENT_RANGE_START = re.compile(r"bytes\s+(\d+)-", re.IGNORECASE)

# Repeating these can never succeed; retrying only delays the summary.
# 403 is only fatal when no re-authentication callback is available; the
# caller checks for that callback before consulting this set.
_FATAL_HTTP_STATUSES = frozenset({400, 401, 403, 404})
_FATAL_ERRNOS = frozenset({errno.ENOSPC, errno.EACCES, errno.EROFS})


def _is_fatal(exc: BaseException) -> bool:
    if getattr(exc, "status", None) in _FATAL_HTTP_STATUSES:
        return True
    return isinstance(exc, OSError) and exc.errno in _FATAL_ERRNOS


class Outcome(str, Enum):
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class DownloadResult:
    item: MediaItem
    outcome: Outcome
    path: Optional[Path] = None
    bytes_written: int = 0
    checksum: Optional[str] = None
    error: Optional[str] = None


async def _hash_existing(path: Path) -> "hashlib._Hash":
    """Seed a SHA-1 hasher with the bytes already on disk (resume case)."""
    digest = hashlib.sha1()  # noqa: S324 - matches the server's originalHash
    async with aiofiles.open(path, "rb") as handle:
        while True:
            chunk = await handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest


def _part_pattern(item: MediaItem, filename_template: str) -> str:
    """Glob matching every partial for this item, whatever its extension.

    The template only decides the stem; the extension comes from the response,
    so ``*`` is required between the two. ``glob.escape`` keeps a template that
    contains ``[`` or ``?`` from being interpreted as a pattern.
    """
    stem = Path(item.filename(filename_template)).stem
    return f"{glob.escape(stem)}.*{PART_SUFFIX}"


def _find_partials(
    directory: Path, item: MediaItem, filename_template: str
) -> list[Path]:
    """Partials for this item, largest first (the best resume candidate)."""
    if not directory.exists():
        return []
    matches = [
        path
        for path in directory.glob(_part_pattern(item, filename_template))
        if path.is_file()
    ]

    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return -1

    return sorted(matches, key=_size, reverse=True)


def _current_partial(
    directory: Path, item: MediaItem, filename_template: str
) -> tuple[int, Optional[Path]]:
    partials = _find_partials(directory, item, filename_template)
    if not partials:
        return 0, None
    partial = partials[0]
    return partial.stat().st_size, partial


def _discard_partials(
    directory: Path, item: MediaItem, filename_template: str
) -> None:
    """Remove every partial for ``item``; used when resume is untrustworthy."""
    for path in _find_partials(directory, item, filename_template):
        try:
            os.remove(path)
        except OSError as exc:
            logger.debug("Could not remove %s: %s", path, exc)


def _existing_media(
    directory: Path,
    item: MediaItem,
    filename_template: str,
    entry: Optional[dict],
) -> Optional[Path]:
    """Find a complete file for ``item``, tolerating a rename since last run.

    ``--repair`` renames files in place (e.g. ``.jpg`` -> ``.mp4``). The cache
    still names the old path, so checking only ``cache_filename`` would make
    the next run re-download every repaired file. A single same-stem file in
    the expected directory is therefore adopted.
    """
    expected_size: Optional[int] = None
    rejected: set[Path] = set()
    # Old cache rows recorded the size *before* EXIF rewriting. Only enforce
    # the size once a row has been marked verified; otherwise upgrading would
    # re-download an entire existing archive.
    verify_size = bool(entry and entry.get("size_verified"))

    if entry:
        raw_size = entry.get("file_size")
        if raw_size:
            try:
                expected_size = int(raw_size)
            except (TypeError, ValueError):
                expected_size = None

        recorded = entry.get("cache_filename")
        if recorded:
            path = Path(str(recorded))
            if path.is_file():
                actual = path.stat().st_size
                if verify_size and expected_size and actual != expected_size:
                    logger.warning(
                        "Cached file %s is %d bytes but %d were recorded; re-downloading.",
                        path,
                        actual,
                        expected_size,
                    )
                    # Do not let the same-stem fallback adopt this very file.
                    try:
                        rejected.add(path.resolve())
                    except OSError:
                        rejected.add(path)
                else:
                    return path

    if not directory.exists():
        return None
    stem = Path(item.filename(filename_template)).stem
    ignored = {PART_SUFFIX, ".json", ".tmp"}
    candidates = []
    for path in directory.glob(f"{glob.escape(stem)}.*"):
        if not path.is_file() or path.suffix.lower() in ignored:
            continue
        try:
            if path.resolve() in rejected:
                continue
            # A repaired file keeps its size; a same-stem file of the wrong
            # size is not the download we recorded.
            if (
                verify_size
                and expected_size
                and path.stat().st_size != expected_size
            ):
                continue
        except OSError:
            continue
        candidates.append(path)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logger.warning(
            "Several files match %s in %s; not guessing which one is complete.",
            stem,
            directory,
        )
    return None


async def _stream_to_disk(
    session: Any,
    url: str,
    item: MediaItem,
    directory: Path,
    filename_template: str,
    db_cache: DatabaseCache,
    resume_from: int,
    part_path: Optional[Path],
    quiet: bool,
    timeout: int,
) -> tuple[Path, int, str]:
    """One attempt. Returns ``(final_path, size, sha1_hex)``."""
    headers = {}
    if resume_from > 0 and part_path is not None and part_path.exists():
        headers["Range"] = f"bytes={resume_from}-"

    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with session.get(url, headers=headers, timeout=client_timeout) as response:
        response.raise_for_status()

        # A server may answer a Range request with 200 and the *whole* body.
        # v6 appended regardless, silently corrupting the file; restart instead.
        resuming = bool(headers) and response.status == 206
        if resuming:
            match = _CONTENT_RANGE_START.match(
                response.headers.get("content-range", "")
            )
            if match and int(match.group(1)) != resume_from:
                logger.debug(
                    "Server resumed at %s for %s, not %s; restarting.",
                    match.group(1),
                    item.uuid,
                    resume_from,
                )
                resuming = False
        if headers and not resuming:
            logger.debug(
                "Server ignored Range for %s (status %s); restarting from 0.",
                item.uuid,
                response.status,
            )
            resume_from = 0

        # Now that the response's Content-Type is known, name the file for what
        # it actually is. This is the fix for photos landing as .webp and every
        # video landing as .jpg.
        served_type = response.headers.get("content-type")
        final_path = directory / item.filename(filename_template, served_type)
        target_part = final_path.with_name(final_path.name + PART_SUFFIX)
        if part_path is not None and part_path != target_part and part_path.exists():
            # Extension changed since the previous attempt; carry the bytes over.
            os.replace(part_path, target_part)
        part_path = target_part

        if resuming:
            digest = await _hash_existing(part_path)
            written = resume_from
            mode = "ab"
        else:
            digest = hashlib.sha1()  # noqa: S324
            written = 0
            mode = "wb"

        content_length = response.headers.get("content-length")
        expected_total = (int(content_length) + written) if content_length else None
        total = expected_total

        with tqdm(
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            total=total,
            initial=written,
            desc=final_path.name[:40],
            leave=False,
            ncols=100,
            disable=quiet,
        ) as progress:
            async with aiofiles.open(part_path, mode) as handle:
                pending = 0
                async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                    await handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    pending += len(chunk)
                    progress.update(len(chunk))
                    if pending >= CACHE_FLUSH_BYTES:
                        await db_cache.mark_progress(url, str(final_path), written)
                        pending = 0

    # The only integrity check the server actually supports. `originalHash`
    # looks like a digest but is computed on the uploading device and does not
    # match the bytes served here, so it is recorded, never enforced.
    if expected_total is not None and written != expected_total:
        raise TruncatedDownload(
            f"{item.uuid}: expected {expected_total} bytes, got {written}"
        )

    checksum = digest.hexdigest()

    # Only now does a complete-looking file appear at the final path.
    os.replace(part_path, final_path)
    return final_path, written, checksum


async def download_media(
    session: Any,
    db_cache: DatabaseCache,
    item: MediaItem,
    url: str,
    directory: Path,
    filename_template: str,
    album_url: str = "",
    quiet: bool = False,
    max_retries: int = 4,
    timeout: int = 1200,
    on_auth_error: Optional[Callable[[], Awaitable[None]]] = None,
) -> DownloadResult:
    """Download one media item, resuming and verifying where possible."""
    entry = await db_cache.get_entry(url)
    directory.mkdir(parents=True, exist_ok=True)

    if entry and entry.get("status") == "complete":
        existing = _existing_media(directory, item, filename_template, entry)
        if existing is not None:
            if str(existing) != str(entry.get("cache_filename") or ""):
                logger.debug(
                    "Adopting renamed file %s for %s.", existing.name, item.uuid
                )
                await db_cache.upsert(url, cache_filename=str(existing))
            if not entry.get("size_verified"):
                # Record the post-EXIF size once, so the next run can enforce it.
                try:
                    await db_cache.mark_metadata_applied(
                        url, existing.stat().st_size
                    )
                except OSError as exc:
                    logger.debug("Could not stat %s: %s", existing, exc)
            return DownloadResult(item, Outcome.SKIPPED, existing)
        logger.debug("Cache says %s is complete but the file is gone; re-fetching.", item.uuid)

    resume_from, part_path = _current_partial(directory, item, filename_template)
    if part_path is None and entry and entry.get("cache_filename"):
        candidate = Path(str(entry["cache_filename"]))
        candidate = candidate.with_name(candidate.name + PART_SUFFIX)
        if candidate.is_file():
            part_path = candidate
            # Trust the bytes on disk, not the counter: the cache flushes about
            # once per MiB, so the row is usually behind the file.
            resume_from = candidate.stat().st_size

    last_error: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            path, size, checksum = await _stream_to_disk(
                session,
                url,
                item,
                directory,
                filename_template,
                db_cache,
                resume_from,
                part_path,
                quiet,
                timeout,
            )
            # Any older partial left over from a differently-named attempt is
            # now stale.
            _discard_partials(directory, item, filename_template)
            await db_cache.mark_complete(
                url,
                str(path),
                size,
                checksum,
                media_type=item.media_type,
                content_type=item.content_type,
                album_url=album_url,
                uuid=item.uuid,
                took_at=item.took_at.isoformat() if item.took_at else None,
                expected_checksum=item.original_hash,
            )
            return DownloadResult(item, Outcome.DOWNLOADED, path, size, checksum)

        except TruncatedDownload as exc:
            # Partial state is untrustworthy: throw it away and start clean.
            last_error = exc
            logger.warning("Truncated download for %s; discarding and retrying.", item.uuid)
            _discard_partials(directory, item, filename_template)
            await db_cache.reset(url)
            resume_from, part_path = 0, None

        except asyncio.CancelledError:
            raise

        except Exception as exc:  # noqa: BLE001 - retried below
            last_error = exc
            logger.warning("Error downloading %s: %s", item.uuid, exc)
            status = getattr(exc, "status", None)
            # 416 means the partial is already longer than the file the server
            # will serve (an oversized or corrupt .part). Resuming can never
            # succeed, so throw it away instead of retrying the same Range.
            if status == 416:
                _discard_partials(directory, item, filename_template)
                await db_cache.reset(url)
                resume_from, part_path = 0, None
            elif status in (401, 403) and on_auth_error is not None:
                # The media endpoint answers with a login redirect/denial once
                # the album session expires. Ask the client to restore it, then
                # retry the same Range against the partial on disk.
                logger.warning(
                    "Session rejected %s (HTTP %s); re-authenticating.",
                    item.uuid,
                    status,
                )
                try:
                    await on_auth_error()
                except MiteneError:
                    raise
                except Exception as auth_exc:  # noqa: BLE001 - retried below
                    last_error = auth_exc
                    logger.warning("Re-authentication failed: %s", auth_exc)
            elif _is_fatal(exc):
                logger.error(
                    "Not retrying %s: %s (out of disk space, permission, or "
                    "the file is gone)",
                    item.uuid,
                    exc,
                )
                break
            else:
                # Re-read what actually landed rather than trusting the counter.
                resume_from, part_path = _current_partial(
                    directory, item, filename_template
                )

        if attempt < max_retries - 1:
            wait = backoff_delay(attempt, last_error)
            logger.info(
                "Retrying %s in %.1fs (attempt %d/%d)",
                item.uuid,
                wait,
                attempt + 2,
                max_retries,
            )
            await asyncio.sleep(wait)

    message = str(last_error) if last_error else "unknown error"
    await db_cache.mark_failed(url, message, max_retries)
    logger.error("Giving up on %s: %s", item.uuid, message)
    return DownloadResult(item, Outcome.FAILED, error=message)
