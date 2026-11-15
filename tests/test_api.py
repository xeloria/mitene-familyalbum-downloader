"""Unit tests for page parsing and URL handling (no network)."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from mitene_download import api as api_module
from mitene_download.api import (
    AlbumClient,
    extract_gon,
    is_invitation_url,
    normalize_album_url,
)
from mitene_download.errors import (
    AlbumNotFoundError,
    AuthError,
    InvitationLinkError,
    ParseError,
)
from mitene_download.models import Album, MediaItem

ALBUM_HTML = """
<html><body>
<script>
window.gon = {};
gon.media = {"mediaFiles": [{"uuid": "abc", "mediaType": "photo"}],
             "hasNext": false, "hasPrev": false, "currentPage": 1};
gon.familyUserIdToColorMap = {"1": "#ff0000"};
</script>
</body></html>
"""


def test_normalize_album_url():
    assert (
        normalize_album_url("https://mitene.us/f/ABCDEF/?foo=bar#x")
        == "https://mitene.us/f/ABCDEF"
    )
    assert normalize_album_url("mitene.us/f/ABCDEF") == "https://mitene.us/f/ABCDEF"
    assert normalize_album_url("  https://mitene.us/f/A/  ") == "https://mitene.us/f/A"


def test_normalize_strips_pasted_login_suffix():
    # The address bar shows /f/<token>/login while the password form is open.
    assert (
        normalize_album_url("https://mitene.us/f/AbCdEfGhIj/login")
        == "https://mitene.us/f/AbCdEfGhIj"
    )
    assert (
        normalize_album_url("https://mitene.us/f/AbCdEfGhIj/login/?x=1")
        == "https://mitene.us/f/AbCdEfGhIj"
    )


def test_normalize_does_not_strip_other_login_pages():
    # /web/login is the computer-upload site, not an album password form.
    assert (
        normalize_album_url("https://family-album.com/web/login")
        == "https://family-album.com/web/login"
    )
    assert (
        normalize_album_url("https://family-album.com/login")
        == "https://family-album.com/login"
    )


def test_extract_gon_parses_media_and_roster():
    gon = extract_gon(ALBUM_HTML)
    assert gon["media"]["hasNext"] is False
    assert gon["familyUserIdToColorMap"] == {"1": "#ff0000"}


def test_extract_gon_tolerates_reformatting():
    html = (
        "<script>gon.media=\n{ 'mediaFiles' : [] , 'hasNext' : false }</script>"
    )
    # Single quotes are not JSON; the parser must not silently accept them.
    with pytest.raises(ParseError):
        extract_gon(html)


def test_extract_gon_reports_password_page():
    html = '<html><input id="session_password"></html>'
    with pytest.raises(AuthError):
        extract_gon(html)


def test_extract_gon_reports_missing_payload():
    with pytest.raises(ParseError):
        extract_gon("<html><body>nothing here</body></html>")


def test_needs_password_detects_form():
    assert AlbumClient._needs_password(
        '<form><input id="session_password" name="session[password]"></form>'
    )
    assert not AlbumClient._needs_password(ALBUM_HTML)


def test_pagination_cap_raises_instead_of_looking_complete(monkeypatch):
    monkeypatch.setattr(api_module, "MAX_PAGES", 3)
    client = AlbumClient(session=None, album_url="https://mitene.us/f/A")  # type: ignore[arg-type]

    async def endless(page: int) -> Album:
        item = MediaItem.from_gon({"uuid": f"u{page}", "mediaType": "photo"})
        return Album(items=[item], has_next=True, has_prev=False, current_page=page)

    client.fetch_page = endless  # type: ignore[assignment]

    async def collect():
        return [album async for album in client.pages()]

    with pytest.raises(ParseError):
        asyncio.run(collect())


class FakeRequestInfo:
    real_url = "https://mitene.us/f/A"
    url = real_url


class FakeResponse:
    def __init__(self, status, text, url="https://mitene.us/f/A"):
        self.status = status
        self._text = text
        self.url = url

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                FakeRequestInfo(), (), status=self.status, message="error"
            )

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        return self.response


def test_invitation_url_detection():
    assert is_invitation_url("https://family-album.com/2d/abc123")
    assert is_invitation_url("https://family-album.com/2d/abc123/")
    assert not is_invitation_url("https://family-album.com/f/abc123")
    assert not is_invitation_url("https://family-album.com/f/2d/abc123")


def test_invitation_error_page_is_explained():
    html = (
        "<html><body><h1>Invalid invitation code.</h1>"
        "<p>This invitation code has already been used by someone else.</p>"
        "</body></html>"
    )
    with pytest.raises(InvitationLinkError) as caught:
        extract_gon(html)
    assert "one-time" in str(caught.value)


def test_not_found_or_disabled_page_is_explained():
    html = (
        "<html><body><h1>Page Not Found</h1>"
        "<p>Browser-version accounts are disabled if they have not been "
        "accessed for an extended period of time.</p></body></html>"
    )
    with pytest.raises(AlbumNotFoundError) as caught:
        extract_gon(html)
    assert "disabled" in str(caught.value)


def test_upload_login_page_is_rejected():
    html = "<html><body><div id='root'>FamilyAlbum</div></body></html>"
    with pytest.raises(ParseError) as caught:
        extract_gon(html, "https://family-album.com/web/login")
    assert "computer-upload" in str(caught.value)


def test_http_404_is_not_retried():
    session = FakeSession(FakeResponse(404, "not found"))
    client = AlbumClient(session, "https://mitene.us/f/gone")  # type: ignore[arg-type]
    with pytest.raises(AlbumNotFoundError):
        asyncio.run(client._get_text("https://mitene.us/f/gone"))
    assert len(session.calls) == 1


def test_http_404_on_invitation_path_is_specific():
    session = FakeSession(FakeResponse(404, "not found"))
    client = AlbumClient(session, "https://family-album.com/2d/code")  # type: ignore[arg-type]
    with pytest.raises(InvitationLinkError):
        asyncio.run(client._get_text("https://family-album.com/2d/code"))
    assert len(session.calls) == 1


def test_invitation_page_served_with_200_is_rejected():
    html = (
        "<html><body><h1>Invalid invitation code.</h1>"
        "<p>This invitation code has already been used.</p></body></html>"
    )
    session = FakeSession(
        FakeResponse(200, html, url="https://family-album.com/2d/code")
    )
    client = AlbumClient(session, "https://family-album.com/2d/code")  # type: ignore[arg-type]
    with pytest.raises(InvitationLinkError):
        asyncio.run(client.fetch_page(1))


def test_retryable_5xx_still_retries(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(api_module.asyncio, "sleep", no_sleep)
    session = FakeSession(FakeResponse(503, "busy"))
    client = AlbumClient(session, "https://mitene.us/f/A", max_retries=2)  # type: ignore[arg-type]
    with pytest.raises(ParseError):
        asyncio.run(client._get_text("https://mitene.us/f/A"))
    assert len(session.calls) == 2


def test_download_url_is_stable_original_endpoint():
    client = AlbumClient(session=None, album_url="https://mitene.us/f/A/")  # type: ignore[arg-type]
    assert client.download_url("u1") == (
        "https://mitene.us/f/A/media_files/u1/download"
    )
