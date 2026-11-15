"""Tests for the SQLite download-state cache."""

from __future__ import annotations

import asyncio
import sqlite3

from mitene_download.cache import DatabaseCache

LEGACY_SCHEMA = """
CREATE TABLE cache (
    url TEXT PRIMARY KEY,
    cache_filename TEXT,
    status TEXT,
    downloaded_size INTEGER DEFAULT 0,
    media_type TEXT,
    creation_date TEXT,
    last_modified TEXT,
    file_size INTEGER,
    checksum TEXT,
    download_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    error_log TEXT,
    retry_count INTEGER DEFAULT 0,
    re_download BOOLEAN DEFAULT FALSE
)
"""


def run(coro):
    return asyncio.run(coro)


def test_upsert_preserves_unmentioned_columns(tmp_path):
    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())
    run(cache.mark_progress("u1", "a.jpg", 10))
    run(cache.mark_complete("u1", "a.jpg", 100, "deadbeef"))

    entry = run(cache.get_entry("u1"))
    assert entry["status"] == "complete"
    assert entry["file_size"] == 100
    assert entry["checksum"] == "deadbeef"

    # A second progress update (a resume of another file) must not null them.
    run(cache.mark_progress("u2", "b.jpg", 5))
    entry = run(cache.get_entry("u1"))
    assert entry["checksum"] == "deadbeef"
    assert entry["file_size"] == 100


def test_reset_and_is_complete(tmp_path):
    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())
    run(cache.mark_complete("u1", "a.jpg", 1))
    assert run(cache.is_complete("u1"))
    run(cache.reset("u1"))
    assert run(cache.get_entry("u1")) is None


def test_migrates_legacy_database_without_losing_history(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    connection.execute(
        "INSERT INTO cache (url, status, checksum) VALUES ('u1', 'complete', 'abc')"
    )
    connection.commit()
    connection.close()

    cache = DatabaseCache(str(path))
    run(cache.init_db())
    entry = run(cache.get_entry("u1"))
    assert entry["status"] == "complete"
    assert entry["checksum"] == "abc"
    for column in ("content_type", "album_url", "uuid", "took_at", "expected_checksum"):
        assert column in entry


def test_update_filename_points_cache_at_renamed_file(tmp_path):
    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())
    run(cache.mark_complete("u1", "old.jpg", 10))
    assert run(cache.update_filename("old.jpg", "new.mp4")) == 1
    assert run(cache.get_entry("u1"))["cache_filename"] == "new.mp4"


def test_album_urls_are_sorted_and_deletable(tmp_path):
    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())
    run(cache.save_album_url("https://mitene.us/f/B"))
    run(cache.save_album_url("https://mitene.us/f/A"))
    assert run(cache.get_all_album_urls()) == [
        "https://mitene.us/f/A",
        "https://mitene.us/f/B",
    ]
    run(cache.delete_album_url("https://mitene.us/f/A"))
    assert run(cache.get_all_album_urls()) == ["https://mitene.us/f/B"]


def test_album_completion_state(tmp_path):
    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())
    assert not run(cache.is_album_complete("https://mitene.us/f/A"))
    run(cache.mark_album_complete("https://mitene.us/f/A"))
    assert run(cache.is_album_complete("https://mitene.us/f/A"))
    # Re-marking is an upsert, not a duplicate-key error.
    run(cache.mark_album_complete("https://mitene.us/f/A"))
    assert run(cache.is_album_complete("https://mitene.us/f/A"))


def test_init_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "cache.db"
    cache = DatabaseCache(str(path))
    run(cache.init_db())
    assert path.exists()


def test_concurrent_writes_do_not_lock(tmp_path):
    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())

    async def burst() -> None:
        await asyncio.gather(
            *(cache.mark_progress(f"u{i}", f"f{i}.jpg", i) for i in range(50))
        )

    run(burst())
    assert len(run(cache.get_all_album_urls())) == 0
    entries = [run(cache.get_entry(f"u{i}")) for i in range(50)]
    assert all(entry is not None for entry in entries)
