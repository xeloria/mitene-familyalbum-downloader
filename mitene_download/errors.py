"""Typed exceptions.

The v6 code raised ``StopAsyncIteration`` for auth failures, which made a wrong
password indistinguishable from an empty album -- and still exited 0.
"""

from __future__ import annotations


class MiteneError(Exception):
    """Base class for every error this package raises deliberately."""


class AuthError(MiteneError):
    """The album is password protected and authentication did not succeed."""


class PasswordRequiredError(AuthError):
    """The album asked for a password and none was supplied."""


class ParseError(MiteneError):
    """The album page did not contain the ``gon.media`` payload we expect."""


class DownloadError(MiteneError):
    """A media file could not be retrieved after all retries."""


class TruncatedDownload(DownloadError):
    """Fewer bytes arrived than ``Content-Length`` promised.

    Note this is the *only* integrity signal available. The album's
    ``originalHash`` field looks like a digest but is computed on the uploading
    device: it does not match the bytes the download endpoint serves (verified
    against a live album), so it is recorded for identity/dedup only and never
    used to accept or reject a file.
    """
