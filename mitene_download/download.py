"""Streaming downloads with safe resume and integrity verification."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import aiofiles  # type: ignore
import aiofiles.os  # type: ignore
import aiohttp  # type: ignore
from tqdm import tqdm  # type: ignore

from .cache import DatabaseCache
from .errors import DownloadError, TruncatedDownload
from .models import MediaItem

logger = logging.getLogger(__name__)

PART_SUFFIX = ".part"
CHUNK_SIZE = 1 << 16  # 64 KiB
CACHE_FLUSH_BYTES = 1 << 20  # persist progress about once per MiB


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


def _apply_extension(path: Path, extension: str) -> Path:
    return path.with_suffix(extension) if path.suffix != extension else path


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
) -> DownloadResult:
    """Download one media item, resuming and verifying where possible."""
    entry = await db_cache.get_entry(url)
    if entry and entry.get("status") == "complete":
        existing = entry.get("cache_filename")
        if existing and Path(existing).exists():
            return DownloadResult(item, Outcome.SKIPPED, Path(existing))
        logger.debug("Cache says %s is complete but the file is gone; re-fetching.", item.uuid)

    directory.mkdir(parents=True, exist_ok=True)

    resume_from = int(entry.get("downloaded_size") or 0) if entry else 0
    part_path: Optional[Path] = None
    if entry and entry.get("cache_filename"):
        candidate = Path(str(entry["cache_filename"]))
        candidate = candidate.with_name(candidate.name + PART_SUFFIX)
        if candidate.exists():
            part_path = candidate
            # Trust the bytes on disk, not the counter: the cache flushes about
            # once per MiB, so the row is usually behind the file.
            resume_from = candidate.stat().st_size
        else:
            resume_from = 0

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
            await _discard_partials(directory, item, filename_template)
            await db_cache.reset(url)
            resume_from, part_path = 0, None

        except asyncio.CancelledError:
            raise

        except Exception as exc:  # noqa: BLE001 - retried below
            last_error = exc
            logger.warning("Error downloading %s: %s", item.uuid, exc)
            # Re-read what actually landed rather than trusting the counter.
            resume_from, part_path = await _current_partial(
                directory, item, filename_template
            )

        if attempt < max_retries - 1:
            wait = 2**attempt + secrets.SystemRandom().uniform(0, 1)
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


async def _current_partial(
    directory: Path, item: MediaItem, template: str
) -> tuple[int, Optional[Path]]:
    path = directory / (item.filename(template) + PART_SUFFIX)
    if path.exists():
        return path.stat().st_size, path
    return 0, None


async def _discard_partials(directory: Path, item: MediaItem, template: str) -> None:
    _, path = await _current_partial(directory, item, template)
    if path is not None:
        try:
            await aiofiles.os.remove(path)
        except OSError:
            pass


async def gather_with_concurrency(limit: int, *coroutines: Any) -> list[Any]:
    """Run coroutines with at most ``limit`` in flight."""
    semaphore = asyncio.Semaphore(limit)

    async def guarded(coro: Any) -> Any:
        async with semaphore:
            return await coro

    return list(await asyncio.gather(*(guarded(c) for c in coroutines)))


def raise_if_failed(results: list[DownloadResult]) -> None:
    failures = [r for r in results if r.outcome is Outcome.FAILED]
    if failures:
        raise DownloadError(f"{len(failures)} file(s) failed to download.")
