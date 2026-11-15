"""Unit tests for payload normalization and filename handling."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from mitene_download.models import (
    Album,
    MediaItem,
    parse_took_at,
    sanitize_filename,
)


def make_item(**overrides):
    data = {
        "uuid": "d9976283-1111-2222-3333-444455556666",
        "mediaType": "photo",
        "contentType": "image/jpeg",
        "tookAt": "2026-07-31T03:59:44+09:00",
        "originalHash": "ABC123",
        "latitude": 35.6812,
        "longitude": 139.7671,
        "mediaWidth": 3024,
        "mediaHeight": 4032,
        "mediaOrientation": 1,
        "mediaDeviceModel": "Xiaomi 22081212G",
        "videoDuration": 0,
        "userId": "910425853615",
        "comments": [],
    }
    data.update(overrides)
    return MediaItem.from_gon(data)


def test_parse_took_at_iso_with_offset():
    parsed = parse_took_at("2026-07-31T03:59:44+09:00")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=9)
    assert parsed.year == 2026 and parsed.hour == 3


def test_parse_took_at_zulu():
    parsed = parse_took_at("2026-07-31T03:59:44Z")
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc


def test_parse_took_at_legacy_formats():
    assert parse_took_at("2026-07-31 03:59:44") == datetime(2026, 7, 31, 3, 59, 44)
    assert parse_took_at("2026-07-31") == datetime(2026, 7, 31)


@pytest.mark.parametrize("value", [None, "", "not a date", 12345])
def test_parse_took_at_rejects_junk(value):
    assert parse_took_at(value) is None


def test_extension_prefers_response_content_type():
    item = make_item(contentType="image/jpeg")
    assert item.extension("video/mp4") == ".mp4"
    assert item.extension("image/jpeg; charset=binary") == ".jpg"


def test_extension_uses_item_content_type_not_preview_url():
    item = make_item(
        mediaType="movie",
        contentType="video/mp4",
        expiringUrl="https://example.test/preview.webp",
    )
    assert item.is_video
    assert item.extension() == ".mp4"


def test_extension_falls_back_per_media_type():
    photo = make_item(contentType=None)
    video = make_item(mediaType="movie", contentType=None)
    assert photo.extension() == ".jpg"
    assert video.extension() == ".mp4"


def test_filename_template_and_extension():
    item = make_item()
    assert item.filename() == "2026-07-31_035944-d9976283.jpg"
    assert item.filename("{year}/{month}/{type}-{uuid8}") == (
        "2026_07_photo-d9976283.jpg"
    )
    assert item.filename("{bogus}") == "2026-07-31_035944-d9976283.jpg"


def test_sanitize_filename_windows_reserved_names():
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("lpt1.jpg") == "_lpt1.jpg"
    assert sanitize_filename("a<b>:c") == "a_b_c"
    assert sanitize_filename("   ") == "unnamed"


def test_has_location_ignores_sentinel_and_out_of_range():
    assert make_item().has_location
    assert not make_item(latitude=0, longitude=0).has_location
    assert not make_item(latitude=91, longitude=10).has_location
    assert not make_item(latitude=10, longitude=-181).has_location
    assert not make_item(latitude=None, longitude=10).has_location


def test_year_month_and_kind():
    item = make_item()
    assert item.year_month == "2026/07"
    assert item.kind == "photos"
    assert make_item(mediaType="movie").kind == "videos"
    undated = make_item(tookAt=None)
    assert undated.year_month == "unknown/unknown"


def test_comments_are_normalized():
    item = make_item(
        comments=[
            {
                "body": "nice",
                "createdAt": "2026-08-01T10:00:00+09:00",
                "user": {"id": 123, "nickname": "Ana"},
                "isDeleted": False,
            },
            {"body": "gone", "isDeleted": True, "user": {"nickname": "Bob"}},
        ]
    )
    assert [c.nickname for c in item.comments] == ["Ana", "Bob"]
    assert item.comments[0].user_id == "123"
    assert item.comments[1].is_deleted


def test_album_from_gon_and_roster():
    gon = {
        "media": {
            "mediaFiles": [{"uuid": "a", "mediaType": "photo"}],
            "hasNext": True,
            "hasPrev": False,
            "currentPage": 2,
        },
        "familyUserIdToColorMap": {"1": "#fff", "2": "#000"},
    }
    album = Album.from_gon(gon)
    assert album.has_next and not album.has_prev
    assert album.current_page == 2
    assert album.user_ids == ["1", "2"]


def test_sidecar_is_json_serializable():
    item = make_item()
    payload = item.to_sidecar()
    json.dumps(payload)
    assert payload["uuid"] == item.uuid
    assert payload["latitude"] == pytest.approx(35.6812)
