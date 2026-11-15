"""Unit tests for magic-byte sniffing, comments, and sidecars."""

from __future__ import annotations

import json

from mitene_download.metadata import (
    _gps_ifd,
    render_comments_markdown,
    set_file_times,
    sniff_extension,
    write_comments,
    write_sidecar,
)
from mitene_download.models import MediaItem

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 28
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 18
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 18
AVI = b"RIFF\x00\x00\x00\x00AVI LIST" + b"\x00" * 16


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_sniff_common_formats(tmp_path):
    assert sniff_extension(write(tmp_path, "a.bin", JPEG)) == ".jpg"
    assert sniff_extension(write(tmp_path, "b.bin", PNG)) == ".png"
    assert sniff_extension(write(tmp_path, "c.bin", WEBP)) == ".webp"
    assert sniff_extension(write(tmp_path, "d.bin", MP4)) == ".mp4"
    assert sniff_extension(write(tmp_path, "e.bin", MOV)) == ".mov"


def test_other_riff_containers_are_not_webp(tmp_path):
    # v7.0.0's magic table had a bare b"RIFF" entry, which would have renamed
    # AVI and WAV files to .webp.
    assert sniff_extension(write(tmp_path, "video.bin", AVI)) is None
    assert sniff_extension(write(tmp_path, "text.bin", b"hello world")) is None


def test_sniff_empty_and_missing(tmp_path):
    assert sniff_extension(write(tmp_path, "empty.bin", b"")) is None
    assert sniff_extension(tmp_path / "missing.bin") is None


def make_item(**overrides):
    data = {
        "uuid": "abc",
        "mediaType": "photo",
        "contentType": "image/jpeg",
        "tookAt": "2026-07-31T03:59:44+09:00",
        "mediaDeviceModel": "Xiaomi 22081212G",
        "comments": [
            {
                "body": "first",
                "createdAt": "2026-08-01T10:00:00+09:00",
                "user": {"nickname": "Ana"},
                "isDeleted": False,
            },
            {"body": "deleted", "user": {"nickname": "X"}, "isDeleted": True},
        ],
    }
    data.update(overrides)
    return MediaItem.from_gon(data)


def test_comment_numbering_restarts_per_file():
    first = render_comments_markdown(make_item(), make_item().comments)
    assert first.startswith("# abc")
    assert "1. **Ana**" in first
    assert "deleted" not in first

    other = make_item(uuid="xyz")
    second = render_comments_markdown(other, other.comments)
    assert "1. **Ana**" in second


def test_write_comments_markdown_and_json(tmp_path):
    item = make_item()
    written = write_comments(tmp_path / "comments", "stem", item, "both")
    names = {path.name for path in written}
    assert names == {"stem.md", "stem.json"}
    payload = json.loads((tmp_path / "comments" / "stem.json").read_text("utf-8"))
    assert payload[0]["body"] == "first"


def test_write_comments_none_returns_empty(tmp_path):
    assert write_comments(tmp_path, "stem", make_item(), "none") == []


def test_sidecar_written_beside_media(tmp_path):
    media = tmp_path / "photo.jpg"
    media.write_bytes(JPEG)
    sidecar = write_sidecar(media, make_item())
    assert sidecar.name == "photo.jpg.json"
    assert json.loads(sidecar.read_text("utf-8"))["uuid"] == "abc"


def test_file_times_use_took_at(tmp_path):
    media = tmp_path / "photo.jpg"
    media.write_bytes(JPEG)
    item = make_item()
    set_file_times(media, item)
    assert abs(media.stat().st_mtime - item.took_at.timestamp()) < 2


def test_gps_ifd_direction_and_rationals():
    gps = _gps_ifd(-33.8688, 151.2093)
    from piexif import GPSIFD

    assert gps[GPSIFD.GPSLatitudeRef] == b"S"
    assert gps[GPSIFD.GPSLongitudeRef] == b"E"
    degrees, minutes, seconds = gps[GPSIFD.GPSLatitude]
    assert degrees == (33, 1)
    assert minutes[0] >= 0
    assert seconds[1] == 10000


def test_gps_seconds_carry_stays_in_range():
    from piexif import GPSIFD

    _, minutes, seconds = _gps_ifd(33.999999999, 0)[GPSIFD.GPSLatitude]
    assert seconds[0] < 600000
    assert minutes[0] < 60


def test_write_exif_skips_non_jpeg(tmp_path):
    from mitene_download.metadata import write_exif

    png = write(tmp_path, "photo.png", PNG)
    assert write_exif(png, make_item()) is False


def test_exiftool_command_carries_all_metadata(monkeypatch, tmp_path):
    import mitene_download.metadata as metadata

    monkeypatch.setattr(metadata.shutil, "which", lambda name: "/usr/bin/exiftool")
    captured = {}

    class Completed:
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)
    png = write(tmp_path, "photo.png", PNG)
    item = make_item(latitude=35.6812, longitude=139.7671)

    assert metadata.write_exif(png, item, use_exiftool=True)
    command = captured["command"]
    assert any(arg.startswith("-DateTimeOriginal=2026:07:31 03:59:44") for arg in command)
    assert "-OffsetTimeOriginal=+09:00" in command
    assert "-Model=Xiaomi 22081212G" in command
    assert "-GPSLatitudeRef=N" in command
    assert "-GPSLongitudeRef=E" in command
    assert command[-1] == str(png.resolve())
    assert captured["kwargs"]["check"] is False


def test_exiftool_is_used_for_jpeg_when_requested(monkeypatch, tmp_path):
    import mitene_download.metadata as metadata

    jpg = write(tmp_path, "photo.jpg", JPEG)
    monkeypatch.setattr(metadata.shutil, "which", lambda name: "/usr/bin/exiftool")
    called = []

    class Completed:
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        called.append(command)
        return Completed()

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)
    assert metadata.write_exif(jpg, make_item(), use_exiftool=True)
    assert called and called[0][0] == "/usr/bin/exiftool"


def test_exiftool_missing_or_failing_returns_false(monkeypatch, tmp_path):
    import mitene_download.metadata as metadata

    png = write(tmp_path, "photo.png", PNG)
    item = make_item()

    monkeypatch.setattr(metadata.shutil, "which", lambda name: None)
    assert metadata.write_exif(png, item, use_exiftool=True) is False

    monkeypatch.setattr(metadata.shutil, "which", lambda name: "/usr/bin/exiftool")

    class Failed:
        returncode = 1
        stderr = "boom"

    monkeypatch.setattr(metadata.subprocess, "run", lambda *a, **k: Failed())
    assert metadata.write_exif(png, item, use_exiftool=True) is False
