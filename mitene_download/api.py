"""Talking to mitene.us: authentication, pagination and page parsing."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from typing import Any, AsyncIterator, Dict, Optional

import aiohttp  # type: ignore
from bs4 import BeautifulSoup  # type: ignore

from .errors import (
    AlbumNotFoundError,
    AuthError,
    InvitationLinkError,
    ParseError,
    PasswordRequiredError,
)
from .models import Album
from .retry import backoff_delay

logger = logging.getLogger(__name__)

# Matches `gon.media=` / `gon.familyUserIdToColorMap=` etc. The value itself is
# then decoded with json.raw_decode rather than a regex: v6 used
# `gon\.media=({.*?});gon\.` which required that exact minified serialization
# and another `gon.` assignment to follow it.
_GON_ASSIGNMENT = re.compile(r"gon\.([A-Za-z_$][\w$]*)\s*=\s*")

MAX_PAGES = 10_000  # defensive cap; a runaway hasNext must not loop forever

# One-time browser invitations live under /2d/<code>. They are not album URLs.
_INVITATION_PATH = re.compile(r"^/2d(?:/|$)")
_UPLOAD_LOGIN_PATH = "/web/login"

_INVITATION_MARKERS = (
    "invalid invitation code",
    "invitation code has already been used",
    "招待コードが無効",
    "すでに使用されています",
)
_NOT_FOUND_MARKERS = (
    "page not found",
    "browser-version accounts are disabled",
    "ask an album admin",
    "ページが見つかりません",
    "ブラウザ版のアカウント",
)

INVITATION_HELP = (
    "This is a one-time FamilyAlbum invitation link (/2d/...). It has already "
    "been used or is invalid. Ask an album admin for a new browser-version "
    "invitation, then use the album's /f/<token> link instead of the "
    "invitation URL."
)
NOT_FOUND_HELP = (
    "The share link is unknown, expired, or its browser-version account has "
    "been disabled. Ask an album admin to send a new invitation and use the "
    "resulting /f/<token> URL."
)


def normalize_album_url(url: str) -> str:
    """Strip query/fragment, a trailing slash, and a pasted ``/login`` suffix.

    The site's password form lives at ``/f/<token>/login``; copying the address
    bar therefore yields a URL that would turn ``<url>/login`` into
    ``/f/<token>/login/login`` at authentication time.
    """
    parsed = urllib.parse.urlsplit(url.strip())
    if not parsed.scheme:
        parsed = urllib.parse.urlsplit("https://" + url.strip())
    path = parsed.path.rstrip("/")
    # Only album share paths carry the password-form suffix. Stripping it from
    # /web/login (the computer-upload login) produced a bogus /web URL.
    if path.endswith("/login") and (
        path.startswith("/f/") or path.startswith("/2d/")
    ):
        path = path[: -len("/login")].rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def is_invitation_url(url: str) -> bool:
    """True for ``/2d/<code>`` one-time browser-invitation links."""
    return bool(_INVITATION_PATH.search(urllib.parse.urlsplit(url).path))


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).lower().split())


def _raise_for_known_error_page(html: str, url: str = "") -> None:
    """Turn FamilyAlbum's error pages into messages that explain the cause.

    These pages are small HTML documents with no ``gon.media``. Reporting them
    as "payload not found" hid the real reason (used invitation, expired
    account, or the wrong login page).
    """
    text = _visible_text(html)
    if any(marker in text for marker in _INVITATION_MARKERS):
        raise InvitationLinkError(INVITATION_HELP)
    if any(marker in text for marker in _NOT_FOUND_MARKERS):
        raise AlbumNotFoundError(NOT_FOUND_HELP)
    if _UPLOAD_LOGIN_PATH in urllib.parse.urlsplit(url).path:
        raise ParseError(
            "This is the FamilyAlbum computer-upload login, not an album share "
            "link. Use https://mitene.us/f/<token> or "
            "https://family-album.com/f/<token>."
        )


def extract_gon(html: str, url: str = "") -> Dict[str, Any]:
    """Pull the ``window.gon`` object out of an album page's inline scripts.

    Returns the assembled ``gon`` dict. Raises :class:`ParseError` when the
    payload is absent or unparseable, which is how a changed page format
    surfaces instead of quietly looking like an empty album.
    """
    soup = BeautifulSoup(html, "html.parser")
    decoder = json.JSONDecoder()
    saw_gon = False

    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        if "gon." not in text:
            continue
        gon: Dict[str, Any] = {}
        for match in _GON_ASSIGNMENT.finditer(text):
            try:
                value, _ = decoder.raw_decode(text, match.end())
            except ValueError:
                # Non-JSON right-hand side (a bare string, `{}`, a function).
                continue
            gon[match.group(1)] = value
        if "media" in gon:
            return gon
        if gon:
            saw_gon = True

    _raise_for_known_error_page(html, url)

    if "session_password" in html:
        raise AuthError("Page still shows the password form; not authenticated.")
    if saw_gon:
        raise ParseError("Found gon assignments but no gon.media payload.")
    raise ParseError(
        "Could not find the gon.media payload on the page. The URL may not be "
        "an album share link (for example an invitation, login or home page), "
        "or the album session may have expired. Open the link in a browser to "
        "check what it shows."
    )


class AlbumClient:
    """Fetches album pages, authenticating once if the album is protected."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        album_url: str,
        password: Optional[str] = None,
        max_retries: int = 4,
        request_timeout: int = 120,
    ) -> None:
        self.session = session
        self.album_url = normalize_album_url(album_url)
        self.password = password
        self.max_retries = max(1, max_retries)
        self.request_timeout = request_timeout
        self._authenticated = False
        self._last_url = self.album_url

    # -- low level --------------------------------------------------------

    async def _get_text(self, url: str) -> str:
        """GET with the same exponential backoff the downloader uses.

        v6 never called ``raise_for_status`` here, so a 500 or a captive-portal
        page just produced "could not find media data script" and stopped.
        """
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                timeout = aiohttp.ClientTimeout(total=self.request_timeout)
                async with self.session.get(url, timeout=timeout) as response:
                    status = response.status
                    if status in (404, 410):
                        if is_invitation_url(url):
                            raise InvitationLinkError(INVITATION_HELP)
                        raise AlbumNotFoundError(NOT_FOUND_HELP)
                    if status in (401, 403):
                        raise AuthError(
                            f"{url} returned HTTP {status}; the album password "
                            "or session was rejected."
                        )
                    if 400 <= status < 500 and status not in (408, 429):
                        raise ParseError(
                            f"{url} returned HTTP {status}; this is not an "
                            "album share link."
                        )
                    response.raise_for_status()
                    self._last_url = str(response.url)
                    return await response.text()
            except (AlbumNotFoundError, InvitationLinkError, AuthError, ParseError):
                # Permanent, explainable outcomes: do not hammer the server.
                raise
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = exc
                if attempt < self.max_retries - 1:
                    wait = backoff_delay(attempt, exc)
                    logger.warning(
                        "Failed to fetch %s (%s); retrying in %.1fs", url, exc, wait
                    )
                    await asyncio.sleep(wait)
        raise ParseError(f"Could not fetch {url}: {last_error}")

    @staticmethod
    def _needs_password(html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        return soup.find("input", {"id": "session_password"}) is not None

    async def authenticate(self, html: str) -> str:
        """Submit the album password. Returns the re-fetched page HTML."""
        if not self.password:
            raise PasswordRequiredError(
                "This album is password protected; pass --password or --password-stdin."
            )
        soup = BeautifulSoup(html, "html.parser")
        token_input = soup.find("input", {"name": "authenticity_token"})
        if not token_input or not token_input.get("value"):
            raise AuthError(
                "Password form found but no authenticity_token; the login page "
                "format may have changed."
            )

        logger.info("Authenticating with album password...")
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                async with self.session.post(
                    f"{self.album_url}/login",
                    data={
                        "session[password]": self.password,
                        "authenticity_token": str(token_input["value"]),
                    },
                    timeout=timeout,
                ) as response:
                    response.raise_for_status()
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = exc
                if attempt < self.max_retries - 1:
                    wait = backoff_delay(attempt, exc)
                    logger.warning(
                        "Password submission failed (%s); retrying in %.1fs",
                        exc,
                        wait,
                    )
                    await asyncio.sleep(wait)
        if last_error is not None:
            raise AuthError(f"Could not submit the album password: {last_error}")

        refreshed = await self._get_text(self.album_url)
        if self._needs_password(refreshed):
            raise AuthError("Invalid album password.")
        self._authenticated = True
        return refreshed

    async def reauthenticate(self) -> None:
        """Restore an expired session, without re-submitting a password needlessly.

        Media downloads do not render a login page, so they cannot call
        :meth:`authenticate` themselves. They report a 401/403 instead, and the
        downloader asks for this.
        """
        html = await self._get_text(self.album_url)
        if self._needs_password(html):
            await self.authenticate(html)
        else:
            self._authenticated = True

    async def fetch_page(self, page: int) -> Album:
        """Fetch and parse one page, authenticating or re-authenticating as needed."""
        url = self.album_url if page == 1 else f"{self.album_url}?page={page}"
        html = await self._get_text(url)

        if is_invitation_url(url) and is_invitation_url(self._last_url):
            # The server accepted the request but this is still the one-time
            # invitation page (typically the "already used" form).
            raise InvitationLinkError(INVITATION_HELP)

        if self._needs_password(html):
            # Also covers a session that expired midway through a long album.
            refreshed = await self.authenticate(html)
            # authenticate() already re-fetched the album root, so page 1 can
            # reuse that response instead of issuing a third GET.
            html = refreshed if page == 1 else await self._get_text(url)
        elif page == 1 and self.password and not self._authenticated:
            logger.warning(
                "A password was provided but this album does not require one."
            )

        return Album.from_gon(extract_gon(html, url))

    # -- iteration --------------------------------------------------------

    async def pages(self) -> AsyncIterator[Album]:
        """Yield every page, stopping on ``hasNext == False``."""
        page = 1
        while page <= MAX_PAGES:
            album = await self.fetch_page(page)
            logger.debug("Page %d: %d items (hasNext=%s)", page, len(album.items), album.has_next)
            yield album
            # `hasNext` is authoritative; the empty check is a backstop in case
            # a future page format drops the flag.
            if not album.has_next or not album.items:
                return
            page += 1
        # Returning quietly here would make a truncated archive look complete.
        raise ParseError(
            f"Pagination did not terminate after {MAX_PAGES} pages; "
            "the album was only partially archived."
        )

    async def items(self) -> AsyncIterator[Any]:
        """Yield every :class:`~.models.MediaItem` across all pages."""
        async for album in self.pages():
            for item in album.items:
                yield item

    def download_url(self, uuid: str) -> str:
        """Stable endpoint that serves the *original* upload, not a rendition."""
        return f"{self.album_url}/media_files/{uuid}/download"
