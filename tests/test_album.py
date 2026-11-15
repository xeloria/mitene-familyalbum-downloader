"""Orchestration tests: filters, index, repair, dry-run, summaries."""

from __future__ import annotations

import asyncio
import csv
from datetime import date

from mitene_download import album as album_module
from mitene_download.album import (
    Summary,
    _handle_item,
    _passes_filters,
    process_album,
    repair_directory,
    sync_albums,
    write_index,
)
from mitene_download.cache import DatabaseCache
from mitene_download.download import DownloadResult, Outcome
from mitene_download.models import Album, MediaItem
from mitene_download.options import Options

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20


def make_item(**overrides):
    data = {
        "uuid": "d9976283-1111-2222-3333-444455556666",
        "mediaType": "photo",
        "contentType": "image/jpeg",
        "tookAt": "2026-07-31T03:59:44+09:00",
        "userId": "u1",
        "comments": [],
    }
    data.update(overrides)
    return MediaItem.from_gon(data)


def run(coro):
    return asyncio.run(coro)


# -- filters ---------------------------------------------------------------


def test_filter_by_media_type():
    options = Options(media_type="videos")
    assert not _passes_filters(make_item(), options)
    assert _passes_filters(make_item(mediaType="movie"), options)


def test_filter_by_uploader_and_dates():
    assert _passes_filters(make_item(), Options(uploader=["u1"]))
    assert not _passes_filters(make_item(), Options(uploader=["u2"]))
    assert _passes_filters(
        make_item(), Options(start_date=date(2026, 7, 31), end_date=date(2026, 7, 31))
    )
    assert not _passes_filters(make_item(), Options(end_date=date(2026, 7, 30)))


def test_undated_items_only_excluded_when_filtering_dates():
    item = make_item(tookAt=None)
    assert _passes_filters(item, Options())
    assert not _passes_filters(item, Options(start_date=date(2026, 1, 1)))


# -- summary ---------------------------------------------------------------


def test_summary_record_merge_and_render():
    summary = Summary()
    item = make_item()
    summary.record(DownloadResult(item, Outcome.DOWNLOADED, bytes_written=2048))
    summary.record(DownloadResult(item, Outcome.SKIPPED))
    summary.record(DownloadResult(item, Outcome.FAILED, error="boom"))
    summary.planned = 2
    summary.renamed = 3

    other = Summary(downloaded=1)
    summary.merge(other)
    assert summary.downloaded == 2 and summary.failed == 1
    assert summary.failures == [f"{item.uuid}: boom"]
    text = summary.render()
    assert "Planned" in text and "Renamed" in text and "boom" in text
    assert summary.to_dict()["planned"] == 2


def test_summary_truncates_long_failure_list():
    summary = Summary(failures=[f"f{i}" for i in range(25)])
    text = summary.render()
    assert "... and 5 more" in text


# -- index -----------------------------------------------------------------


def test_index_uses_actual_downloaded_paths(tmp_path):
    destination = tmp_path / "archive"
    photo = destination / "photos" / "2026" / "07" / "p.jpg"
    video = destination / "videos" / "2026" / "07" / "v.mp4"
    for path in (photo, video):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(JPEG if path.suffix == ".jpg" else MP4)

    write_index(
        destination,
        [(make_item(), photo), (make_item(mediaType="movie"), video)],
        Options(),
    )
    rows = list(csv.DictReader((destination / "index.csv").open(encoding="utf-8")))
    assert {row["file"] for row in rows} == {
        "photos/2026/07/p.jpg",
        "videos/2026/07/v.mp4",
    }
    html = (destination / "index.html").read_text("utf-8")
    assert 'src="videos/2026/07/v.mp4"' in html
    assert "FamilyAlbum archive" in html


def test_index_escapes_html_in_comments(tmp_path):
    destination = tmp_path / "archive"
    media = destination / "photos" / "x.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(JPEG)
    item = make_item(
        comments=[{"body": "<script>x</script>", "user": {"nickname": "<b>"}}]
    )
    write_index(destination, [(item, media)], Options())
    html = (destination / "index.html").read_text("utf-8")
    assert "<script>x" not in html
    assert "&lt;script&gt;" in html


# -- repair ----------------------------------------------------------------


def test_repair_renames_by_magic_bytes_and_updates_cache(tmp_path):
    destination = tmp_path / "archive"
    wrong = destination / "videos" / "2026" / "07" / "clip.jpg"
    wrong.parent.mkdir(parents=True)
    wrong.write_bytes(MP4)  # a video named .jpg, as v6 produced
    partial = destination / "videos" / "2026" / "07" / "other.jpg.part"
    partial.write_bytes(JPEG)
    index = destination / "index.html"
    index.write_text("<html></html>", encoding="utf-8")

    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())
    run(cache.mark_complete("url", str(wrong), len(MP4)))

    summary = run(repair_directory(destination, db_path=str(tmp_path / "cache.db")))
    fixed = wrong.with_suffix(".mp4")
    assert fixed.exists() and not wrong.exists()
    assert summary.renamed == 1
    assert partial.exists(), "an in-flight .part must never be renamed"
    assert index.exists()
    assert run(cache.get_entry("url"))["cache_filename"] == str(fixed)


def test_repair_dry_run_does_not_create_cache(tmp_path):
    destination = tmp_path / "archive"
    destination.mkdir()
    db = tmp_path / "cache.db"
    run(repair_directory(destination, dry_run=True, db_path=str(db)))
    assert not db.exists()


def test_repair_dry_run_changes_nothing(tmp_path):
    destination = tmp_path / "archive"
    wrong = destination / "videos" / "clip.jpg"
    wrong.parent.mkdir(parents=True)
    wrong.write_bytes(MP4)
    summary = run(repair_directory(destination, dry_run=True))
    assert summary.renamed == 1
    assert wrong.exists()
    assert not wrong.with_suffix(".mp4").exists()


def test_repair_updates_an_existing_index(tmp_path):
    destination = tmp_path / "archive"
    wrong = destination / "videos" / "2026" / "07" / "clip.jpg"
    wrong.parent.mkdir(parents=True)
    wrong.write_bytes(MP4)
    item = make_item(mediaType="movie", uuid="clip")

    write_index(destination, [(item, wrong)], Options())
    summary = run(repair_directory(destination))

    assert summary.renamed == 1
    csv_text = (destination / "index.csv").read_text("utf-8")
    assert "videos/2026/07/clip.mp4" in csv_text
    assert "videos/2026/07/clip.jpg" not in csv_text
    html_text = (destination / "index.html").read_text("utf-8")
    assert 'src="videos/2026/07/clip.mp4"' in html_text
    assert 'src="videos/2026/07/clip.jpg"' not in html_text


def test_final_state_records_post_metadata_size(tmp_path):
    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())
    path = tmp_path / "photo.jpg"
    # Simulate piexif changing the size: bigger file than the download count.
    path.write_bytes(JPEG + b"EXIF" * 40)
    run(cache.mark_complete("url", str(path), len(JPEG), "old-checksum"))

    result = DownloadResult(
        make_item(),
        Outcome.DOWNLOADED,
        path,
        bytes_written=len(JPEG),
        checksum="old-checksum",
    )
    run(album_module._record_final_state(cache, "url", result))

    entry = run(cache.get_entry("url"))
    assert entry["file_size"] == path.stat().st_size
    assert entry["size_verified"] in (1, True)
    assert entry["checksum"] != "old-checksum"


# -- orchestration ---------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    destination = tmp_path / "archive"
    options = Options(dest=str(destination), dry_run=True, index=True)
    summary = Summary()

    class Client:
        album_url = "https://mitene.us/f/A"

        def download_url(self, uuid):
            return f"https://mitene.us/f/A/media_files/{uuid}/download"

    item = make_item(comments=[{"body": "hi", "user": {"nickname": "A"}}])
    path = run(_handle_item(None, Client(), None, item, options, summary))  # type: ignore[arg-type]
    assert path is not None and path.name.endswith(".jpg")
    assert summary.planned == 1
    assert not destination.exists()


class FakeClient:
    """Stands in for AlbumClient; pages are supplied per album URL."""

    pages_by_url = {}

    def __init__(self, session, album_url, *args, **kwargs):
        self.album_url = album_url

    async def pages(self):
        for page in self.pages_by_url[self.album_url]:
            yield page

    def download_url(self, uuid):
        return f"{self.album_url}/media/{uuid}"

    async def reauthenticate(self):
        return None


class FakeSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def fake_download(calls):
    async def _download(session, db_cache, item, url, directory, template, **kwargs):
        calls.append(item.uuid)
        return DownloadResult(item, Outcome.SKIPPED, directory / f"{item.uuid}.jpg")

    return _download


def test_incremental_stops_at_an_archived_page(tmp_path, monkeypatch):
    destination = tmp_path / "archive"
    known_dir = destination / "photos" / "2026" / "07"
    known_dir.mkdir(parents=True)
    known = make_item(uuid="known")
    known_path = known_dir / "known.jpg"
    known_path.write_bytes(JPEG)

    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())
    run(cache.mark_complete("https://mitene.us/f/A/media/known", str(known_path), len(JPEG)))
    run(cache.mark_album_complete("https://mitene.us/f/A"))

    FakeClient.pages_by_url = {
        "https://mitene.us/f/A": [
            Album(items=[known], has_next=True, has_prev=False, current_page=1),
            Album(
                items=[make_item(uuid="new")],
                has_next=False,
                has_prev=False,
                current_page=2,
            ),
        ]
    }
    monkeypatch.setattr(album_module, "AlbumClient", FakeClient)
    monkeypatch.setattr(album_module.aiohttp, "ClientSession", FakeSession)
    calls = []
    monkeypatch.setattr(album_module, "download_media", fake_download(calls))

    summary = run(
        process_album(
            "https://mitene.us/f/A",
            Options(dest=str(destination), incremental=True),
            cache,
        )
    )
    assert calls == [], "page 2 must never be fetched once page 1 is archived"
    assert summary.downloaded == 0


def test_incremental_does_not_trust_a_truncated_file(tmp_path, monkeypatch):
    destination = tmp_path / "archive"
    known_dir = destination / "photos" / "2026" / "07"
    known_dir.mkdir(parents=True)
    known = make_item(uuid="known")
    known_path = known_dir / "known.jpg"
    known_path.write_bytes(b"short")  # recorded size says it should be longer

    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())
    run(cache.mark_complete("https://mitene.us/f/A/media/known", str(known_path), len(JPEG)))
    # A modern row: size verified after metadata processing.
    run(cache.mark_metadata_applied("https://mitene.us/f/A/media/known", len(JPEG)))
    run(cache.mark_album_complete("https://mitene.us/f/A"))

    FakeClient.pages_by_url = {
        "https://mitene.us/f/A": [
            Album(items=[known], has_next=True, has_prev=False, current_page=1),
            Album(
                items=[make_item(uuid="new")],
                has_next=False,
                has_prev=False,
                current_page=2,
            ),
        ]
    }
    monkeypatch.setattr(album_module, "AlbumClient", FakeClient)
    monkeypatch.setattr(album_module.aiohttp, "ClientSession", FakeSession)
    calls = []
    monkeypatch.setattr(album_module, "download_media", fake_download(calls))

    run(
        process_album(
            "https://mitene.us/f/A",
            Options(dest=str(destination), incremental=True),
            cache,
        )
    )
    assert calls == ["known", "new"], "a truncated file must force a normal pass"


def test_incremental_without_a_completed_album_does_a_full_pass(tmp_path, monkeypatch):
    destination = tmp_path / "archive"
    known = make_item(uuid="known")

    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())

    FakeClient.pages_by_url = {
        "https://mitene.us/f/A": [
            Album(items=[known], has_next=True, has_prev=False, current_page=1),
            Album(
                items=[make_item(uuid="new")],
                has_next=False,
                has_prev=False,
                current_page=2,
            ),
        ]
    }
    monkeypatch.setattr(album_module, "AlbumClient", FakeClient)
    monkeypatch.setattr(album_module.aiohttp, "ClientSession", FakeSession)
    calls = []
    monkeypatch.setattr(album_module, "download_media", fake_download(calls))

    run(
        process_album(
            "https://mitene.us/f/A",
            Options(dest=str(destination), incremental=True),
            cache,
        )
    )
    assert calls == ["known", "new"]


def test_completed_pass_is_recorded_for_incremental(tmp_path, monkeypatch):
    destination = tmp_path / "archive"
    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())
    FakeClient.pages_by_url = {
        "https://mitene.us/f/A": [
            Album(
                items=[make_item(uuid="a")],
                has_next=False,
                has_prev=False,
                current_page=1,
            )
        ]
    }
    monkeypatch.setattr(album_module, "AlbumClient", FakeClient)
    monkeypatch.setattr(album_module.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(album_module, "download_media", fake_download([]))

    run(process_album("https://mitene.us/f/A", Options(dest=str(destination)), cache))
    assert run(cache.is_album_complete("https://mitene.us/f/A"))


def test_filtered_pass_is_not_recorded_as_complete(tmp_path, monkeypatch):
    destination = tmp_path / "archive"
    cache = DatabaseCache(str(tmp_path / "cache.db"))
    run(cache.init_db())
    FakeClient.pages_by_url = {
        "https://mitene.us/f/A": [
            Album(
                items=[make_item(uuid="a")],
                has_next=False,
                has_prev=False,
                current_page=1,
            )
        ]
    }
    monkeypatch.setattr(album_module, "AlbumClient", FakeClient)
    monkeypatch.setattr(album_module.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(album_module, "download_media", fake_download([]))

    run(
        process_album(
            "https://mitene.us/f/A",
            Options(dest=str(destination), media_type="photos"),
            cache,
        )
    )
    assert not run(cache.is_album_complete("https://mitene.us/f/A"))


def test_sync_limit_is_global_not_per_album(tmp_path, monkeypatch):
    urls = ["https://mitene.us/f/A", "https://mitene.us/f/B"]
    FakeClient.pages_by_url = {
        url: [
            Album(
                items=[make_item(uuid=f"{url[-1]}{index}") for index in range(3)],
                has_next=False,
                has_prev=False,
                current_page=1,
            )
        ]
        for url in urls
    }
    monkeypatch.setattr(album_module, "AlbumClient", FakeClient)
    monkeypatch.setattr(album_module.aiohttp, "ClientSession", FakeSession)
    calls = []
    monkeypatch.setattr(album_module, "download_media", fake_download(calls))

    options = Options(
        dest=str(tmp_path / "archive"),
        db_path=str(tmp_path / "cache.db"),
        limit=4,
        album_concurrency=2,
    )
    summary = run(sync_albums(urls, options))
    assert len(calls) == 4
    assert summary.skipped == 4


def test_sync_writes_one_combined_index(tmp_path, monkeypatch):
    urls = ["https://mitene.us/f/A", "https://mitene.us/f/B"]
    FakeClient.pages_by_url = {
        url: [
            Album(
                items=[make_item(uuid=f"{url[-1]}-{index}") for index in range(2)],
                has_next=False,
                has_prev=False,
                current_page=1,
            )
        ]
        for url in urls
    }
    monkeypatch.setattr(album_module, "AlbumClient", FakeClient)
    monkeypatch.setattr(album_module.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(album_module, "download_media", fake_download([]))

    destination = tmp_path / "archive"
    options = Options(
        dest=str(destination),
        db_path=str(tmp_path / "cache.db"),
        index=True,
        album_concurrency=2,
    )
    run(sync_albums(urls, options))

    csv_text = (destination / "index.csv").read_text("utf-8")
    html_text = (destination / "index.html").read_text("utf-8")
    for uuid in ("A-0", "A-1", "B-0", "B-1"):
        assert uuid in csv_text
        assert f'src="photos/2026/07/{uuid}.jpg"' in html_text


def test_process_album_dry_run_does_not_create_dest(tmp_path, monkeypatch):
    destination = tmp_path / "archive"
    items = [make_item(), make_item(uuid="second", mediaType="movie")]

    class Client:
        def __init__(self, *args, **kwargs):
            self.album_url = "https://mitene.us/f/A"

        async def pages(self):
            yield Album(items=items, has_next=False, has_prev=False, current_page=1)

        def download_url(self, uuid):
            return f"https://mitene.us/f/A/media_files/{uuid}/download"

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(album_module, "AlbumClient", Client)
    monkeypatch.setattr(album_module.aiohttp, "ClientSession", Session)

    options = Options(dest=str(destination), dry_run=True, index=True)
    cache = DatabaseCache(str(tmp_path / "cache.db"))
    summary = run(process_album("https://mitene.us/f/A", options, cache))
    assert summary.planned == 2
    assert summary.downloaded == 0
    assert not destination.exists()
