"""Downloader tests with a fake HTTP session -- no network access."""

from __future__ import annotations

import asyncio
import errno
import hashlib
from pathlib import Path

import pytest

from mitene_download.cache import DatabaseCache
from mitene_download.errors import AuthError
from mitene_download.download import (
    Outcome,
    _existing_media,
    _find_partials,
    _is_fatal,
    download_media,
)
from mitene_download.models import MediaItem

BODY = bytes(range(100)) * 2  # 200 deterministic bytes


def make_item(**overrides):
    data = {
        "uuid": "d9976283-1111-2222-3333-444455556666",
        "mediaType": "movie",
        "contentType": "video/mp4",
        "tookAt": "2026-07-31T03:59:44+09:00",
    }
    data.update(overrides)
    return MediaItem.from_gon(data)


class FakeContent:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def iter_chunked(self, size: int):
        for offset in range(0, len(self.data), size):
            yield self.data[offset : offset + size]


class FakeHTTPError(Exception):
    def __init__(self, status: int, headers=None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.headers = headers or {}


class FakeResponse:
    def __init__(self, data: bytes, status: int = 200, headers=None) -> None:
        self._data = data
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(data)

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise FakeHTTPError(self.status, self.headers)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    """Serves ``data``; honours Range only when ``obey_range`` is true."""

    def __init__(self, data: bytes, obey_range: bool = True, content_type="video/mp4"):
        self.data = data
        self.obey_range = obey_range
        self.content_type = content_type
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        headers = headers or {}
        self.calls.append(headers)
        if "Range" in headers and self.obey_range:
            start = int(headers["Range"].split("=")[1].rstrip("-"))
            body = self.data[start:]
            return FakeResponse(
                body,
                206,
                {
                    "content-length": str(len(body)),
                    "content-range": f"bytes {start}-{len(self.data) - 1}/{len(self.data)}",
                    "content-type": self.content_type,
                },
            )
        return FakeResponse(
            self.data,
            200,
            {"content-length": str(len(self.data)), "content-type": self.content_type},
        )


class ExplodingSession:
    def get(self, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("network should not be touched")


def new_cache(tmp_path: Path) -> DatabaseCache:
    cache = DatabaseCache(str(tmp_path / "cache.db"))
    asyncio.run(cache.init_db())
    return cache


def test_download_writes_original_extension_and_checksum(tmp_path):
    cache = new_cache(tmp_path)
    item = make_item()
    result = asyncio.run(
        download_media(
            FakeSession(BODY, content_type="video/mp4"),
            cache,
            item,
            "https://example.test/download",
            tmp_path / "videos",
            "{date}_{time}-{uuid8}",
            max_retries=1,
        )
    )
    assert result.outcome is Outcome.DOWNLOADED
    assert result.path is not None and result.path.suffix == ".mp4"
    assert result.path.read_bytes() == BODY
    assert result.checksum == hashlib.sha1(BODY).hexdigest()
    assert not list((tmp_path / "videos").glob("*.part"))

    entry = asyncio.run(cache.get_entry("https://example.test/download"))
    assert entry["status"] == "complete"
    assert entry["file_size"] == len(BODY)


def test_resume_appends_only_the_remaining_bytes(tmp_path):
    cache = new_cache(tmp_path)
    item = make_item()
    directory = tmp_path / "videos"
    directory.mkdir()
    part = directory / (item.filename("{date}_{time}-{uuid8}") + ".part")
    part.write_bytes(BODY[:80])

    session = FakeSession(BODY)
    result = asyncio.run(
        download_media(
            session, cache, item, "url", directory, "{date}_{time}-{uuid8}",
            max_retries=1,
        )
    )
    assert result.outcome is Outcome.DOWNLOADED
    assert result.path.read_bytes() == BODY
    assert session.calls and "Range" in session.calls[0]


def test_server_ignoring_range_restarts_cleanly(tmp_path):
    cache = new_cache(tmp_path)
    item = make_item()
    directory = tmp_path / "videos"
    directory.mkdir()
    part = directory / (item.filename("{date}_{time}-{uuid8}") + ".part")
    part.write_bytes(b"corrupt partial")

    result = asyncio.run(
        download_media(
            FakeSession(BODY, obey_range=False),
            cache,
            item,
            "url",
            directory,
            "{date}_{time}-{uuid8}",
            max_retries=1,
        )
    )
    assert result.outcome is Outcome.DOWNLOADED
    assert result.path.read_bytes() == BODY


def test_partial_with_old_extension_is_carried_over(tmp_path):
    cache = new_cache(tmp_path)
    item = make_item()
    directory = tmp_path / "videos"
    directory.mkdir()
    # Simulate v6 naming the part from the poster frame (.jpg).
    old_part = directory / (
        item.filename("{date}_{time}-{uuid8}").replace(".mp4", ".jpg") + ".part"
    )
    old_part.write_bytes(BODY[:80])

    result = asyncio.run(
        download_media(
            FakeSession(BODY), cache, item, "url", directory, "{date}_{time}-{uuid8}",
            max_retries=1,
        )
    )
    assert result.outcome is Outcome.DOWNLOADED
    assert result.path.suffix == ".mp4"
    assert result.path.read_bytes() == BODY
    assert not old_part.exists()


def test_truncated_download_fails_and_leaves_no_partial(tmp_path):
    cache = new_cache(tmp_path)
    item = make_item()
    short = FakeSession(BODY[:10])
    # Claim the full length while sending ten bytes.
    short.get = lambda *a, **k: FakeResponse(
        BODY[:10], 200, {"content-length": str(len(BODY)), "content-type": "video/mp4"}
    )

    result = asyncio.run(
        download_media(
            short, cache, item, "url", tmp_path / "videos",
            "{date}_{time}-{uuid8}", max_retries=1,
        )
    )
    assert result.outcome is Outcome.FAILED
    assert not list((tmp_path / "videos").glob("*.part"))
    assert not list((tmp_path / "videos").glob("*.mp4"))


def test_complete_entry_is_skipped_without_network(tmp_path):
    cache = new_cache(tmp_path)
    item = make_item()
    directory = tmp_path / "videos"
    directory.mkdir()
    existing = directory / "already.mp4"
    existing.write_bytes(BODY)
    asyncio.run(cache.mark_complete("url", str(existing), len(BODY)))

    result = asyncio.run(
        download_media(
            ExplodingSession(), cache, item, "url", directory,
            "{date}_{time}-{uuid8}", max_retries=1,
        )
    )
    assert result.outcome is Outcome.SKIPPED
    assert result.path == existing


def test_renamed_file_is_adopted_and_cache_updated(tmp_path):
    cache = new_cache(tmp_path)
    item = make_item()
    directory = tmp_path / "videos"
    directory.mkdir()
    old = directory / "2026-07-31_035944-d9976283.jpg"
    new = directory / "2026-07-31_035944-d9976283.mp4"
    new.write_bytes(BODY)
    asyncio.run(cache.mark_complete("url", str(old), len(BODY)))

    result = asyncio.run(
        download_media(
            ExplodingSession(), cache, item, "url", directory,
            "{date}_{time}-{uuid8}", max_retries=1,
        )
    )
    assert result.outcome is Outcome.SKIPPED
    assert result.path == new
    assert asyncio.run(cache.get_entry("url"))["cache_filename"] == str(new)


def test_legacy_size_mismatch_is_adopted_once_and_migrated(tmp_path):
    cache = new_cache(tmp_path)
    item = make_item()
    directory = tmp_path / "videos"
    directory.mkdir()
    existing = directory / "done.mp4"
    existing.write_bytes(BODY)
    # A v7.0.x row: size recorded before EXIF rewriting, never verified.
    asyncio.run(cache.mark_complete("url", str(existing), len(BODY) + 999))

    result = asyncio.run(
        download_media(
            ExplodingSession(), cache, item, "url", directory,
            "{date}_{time}-{uuid8}", max_retries=1,
        )
    )
    assert result.outcome is Outcome.SKIPPED

    entry = asyncio.run(cache.get_entry("url"))
    assert entry["size_verified"] in (1, True)
    assert entry["file_size"] == len(BODY)


def test_partial_discovery_handles_glob_characters(tmp_path):
    item = make_item()
    directory = tmp_path / "videos"
    directory.mkdir()
    template = "[{uuid8}]_{date}"
    part = directory / (item.filename(template) + ".part")
    part.write_bytes(b"x" * 7)
    assert _find_partials(directory, item, template) == [part]


def test_partial_discovery_prefers_largest(tmp_path):
    item = make_item()
    directory = tmp_path / "videos"
    directory.mkdir()
    small = directory / "2026-07-31_035944-d9976283.jpg.part"
    large = directory / "2026-07-31_035944-d9976283.mp4.part"
    small.write_bytes(b"x" * 7)
    large.write_bytes(b"x" * 20)
    assert _find_partials(directory, item, "{date}_{time}-{uuid8}")[0] == large


def test_401_triggers_reauthentication_then_retries(tmp_path, monkeypatch):
    import mitene_download.download as download_module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(download_module.asyncio, "sleep", no_sleep)

    cache = new_cache(tmp_path)
    item = make_item()

    class FlakySession(FakeSession):
        def __init__(self):
            super().__init__(BODY)
            self.attempts = 0

        def get(self, url, headers=None, timeout=None):
            self.attempts += 1
            if self.attempts == 1:
                return FakeResponse(b"", 401, {"content-type": "text/html"})
            return super().get(url, headers=headers, timeout=timeout)

    session = FlakySession()
    reauth_calls = []

    async def reauthenticate():
        reauth_calls.append(True)

    result = asyncio.run(
        download_media(
            session,
            cache,
            item,
            "url",
            tmp_path / "videos",
            "{date}_{time}-{uuid8}",
            max_retries=2,
            on_auth_error=reauthenticate,
        )
    )
    assert result.outcome is Outcome.DOWNLOADED
    assert result.path.read_bytes() == BODY
    assert reauth_calls == [True]


def test_failed_reauthentication_aborts_the_album(tmp_path, monkeypatch):
    import mitene_download.download as download_module

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(download_module.asyncio, "sleep", no_sleep)

    cache = new_cache(tmp_path)
    item = make_item()

    class DeniedSession(FakeSession):
        def get(self, url, headers=None, timeout=None):
            return FakeResponse(b"", 403, {"content-type": "text/html"})

    async def reauthenticate():
        raise AuthError("Invalid album password.")

    with pytest.raises(AuthError):
        asyncio.run(
            download_media(
                DeniedSession(BODY),
                cache,
                item,
                "url",
                tmp_path / "videos",
                "{date}_{time}-{uuid8}",
                max_retries=2,
                on_auth_error=reauthenticate,
            )
        )


def test_non_retryable_errors_are_identified():
    class HttpError(Exception):
        def __init__(self, status):
            self.status = status

    assert _is_fatal(HttpError(404))
    assert _is_fatal(HttpError(403))
    assert _is_fatal(OSError(errno.ENOSPC, "disk full"))
    assert _is_fatal(PermissionError(errno.EACCES, "denied"))
    assert not _is_fatal(HttpError(500))
    assert not _is_fatal(HttpError(429))
    assert not _is_fatal(RuntimeError("transient"))


def test_wrong_size_is_not_treated_as_complete(tmp_path):
    item = make_item()
    directory = tmp_path / "videos"
    directory.mkdir()
    path = directory / item.filename("{date}_{time}-{uuid8}")
    path.write_bytes(b"truncated")
    entry = {
        "cache_filename": str(path),
        "file_size": len(BODY),
        "size_verified": True,
    }

    # The recorded file exists but is short: do not skip it, and do not let the
    # same-stem fallback adopt the very same file.
    assert _existing_media(
        directory, item, "{date}_{time}-{uuid8}", entry
    ) is None


def test_wrong_size_same_stem_is_not_adopted(tmp_path):
    item = make_item()
    directory = tmp_path / "videos"
    directory.mkdir()
    stem = Path(item.filename("{date}_{time}-{uuid8}")).stem
    (directory / f"{stem}.mp4").write_bytes(b"wrong")
    entry = {
        "cache_filename": str(directory / "gone.mp4"),
        "file_size": len(BODY),
        "size_verified": True,
    }

    assert _existing_media(
        directory, item, "{date}_{time}-{uuid8}", entry
    ) is None


def test_sidecar_does_not_confuse_adoption(tmp_path):
    item = make_item()
    directory = tmp_path / "videos"
    directory.mkdir()
    stem = Path(item.filename("{date}_{time}-{uuid8}")).stem
    (directory / f"{stem}.jpg.json").write_text("{}", encoding="utf-8")
    entry = {"cache_filename": str(directory / "gone.mp4"), "file_size": 1}
    # Only a sidecar shares the stem: that is not the media file.
    assert _existing_media(directory, item, "{date}_{time}-{uuid8}", entry) is None
