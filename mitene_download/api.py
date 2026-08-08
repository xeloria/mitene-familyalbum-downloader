"""Talking to mitene.us: authentication, pagination and page parsing."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import urllib.parse
from typing import Any, AsyncIterator, Dict, Optional

import aiohttp  # type: ignore
from bs4 import BeautifulSoup  # type: ignore

from .errors import AuthError, ParseError, PasswordRequiredError
from .models import Album

logger = logging.getLogger(__name__)

# Matches `gon.media=` / `gon.familyUserIdToColorMap=` etc. The value itself is
# then decoded with json.raw_decode rather than a regex: v6 used
# `gon\.media=({.*?});gon\.` which required that exact minified serialization
# and another `gon.` assignment to follow it.
_GON_ASSIGNMENT = re.compile(r"gon\.([A-Za-z_$][\w$]*)\s*=\s*")

MAX_PAGES = 10_000  # defensive cap; a runaway hasNext must not loop forever


def normalize_album_url(url: str) -> str:
    """Strip query/fragment and any trailing slash from a share link."""
    parsed = urllib.parse.urlsplit(url.strip())
    if not parsed.scheme:
        parsed = urllib.parse.urlsplit("https://" + url.strip())
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def extract_gon(html: str) -> Dict[str, Any]:
    """Pull the ``window.gon`` object out of an album page's inline scripts.

    Returns the assembled ``gon`` dict. Raises :class:`ParseError` when the
    payload is absent or unparseable, which is how a changed page format
    surfaces instead of quietly looking like an empty album.
    """
    soup = BeautifulSoup(html, "html.parser")
    decoder = json.JSONDecoder()

    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        if "gon.media" not in text:
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
        raise ParseError("Found gon assignments but no gon.media payload.")

    if "session_password" in html:
        raise AuthError("Page still shows the password form; not authenticated.")
    raise ParseError("Could not find the gon.media payload on the page.")


class AlbumClient:
    """Fetches album pages, authenticating once if the album is protected."""

    def __init__(
        self,
        session: Any,
        album_url: str,
        password: Optional[str] = None,
        max_retries: int = 4,
    ) -> None:
        self.session = session
        self.album_url = normalize_album_url(album_url)
        self.password = password
        self.max_retries = max_retries
        self._authenticated = False

    # -- low level --------------------------------------------------------

    async def _get_text(self, url: str) -> str:
        """GET with the same exponential backoff the downloader uses.

        v6 never called ``raise_for_status`` here, so a 500 or a captive-portal
        page just produced "could not find media data script" and stopped.
        """
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                async with self.session.get(url) as response:
                    response.raise_for_status()
                    return await response.text()
            except Exception as exc:  # noqa: BLE001 - retried below
                last_error = exc
                if attempt < self.max_retries - 1:
                    wait = 2**attempt + secrets.SystemRandom().uniform(0, 1)
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
        async with self.session.post(
            f"{self.album_url}/login",
            data={
                "session[password]": self.password,
                "authenticity_token": str(token_input["value"]),
            },
        ) as response:
            response.raise_for_status()

        refreshed = await self._get_text(self.album_url)
        if self._needs_password(refreshed):
            raise AuthError("Invalid album password.")
        self._authenticated = True
        return refreshed

    async def fetch_page(self, page: int) -> Album:
        """Fetch and parse one page, authenticating or re-authenticating as needed."""
        url = self.album_url if page == 1 else f"{self.album_url}?page={page}"
        html = await self._get_text(url)

        if self._needs_password(html):
            # Also covers a session that expired midway through a long album.
            await self.authenticate(html)
            html = await self._get_text(url)
        elif page == 1 and self.password and not self._authenticated:
            logger.warning(
                "A password was provided but this album does not require one."
            )

        return Album.from_gon(extract_gon(html))

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
        logger.warning("Stopped after %d pages; hasNext never became false.", MAX_PAGES)

    async def items(self) -> AsyncIterator[Any]:
        """Yield every :class:`~.models.MediaItem` across all pages."""
        async for album in self.pages():
            for item in album.items:
                yield item

    def download_url(self, uuid: str) -> str:
        """Stable endpoint that serves the *original* upload, not a rendition."""
        return f"{self.album_url}/media_files/{uuid}/download"
