"""Normalized representations of the ``window.gon`` payload.

Everything downstream works with :class:`MediaItem` rather than raw dicts, so the
quirks of the site's JSON are handled in exactly one place.

Shape of a real item (captured live, fields we care about)::

    {"uuid": "d9976283-...", "mediaType": "photo", "contentType": "image/jpeg",
     "tookAt": "2026-07-31T03:59:44+09:00", "originalHash": "22960717db...",
     "latitude": 0, "longitude": 0, "mediaWidth": 3024, "mediaHeight": 4032,
     "mediaOrientation": 1, "mediaDeviceModel": "Xiaomi 22081212G",
     "videoDuration": 0, "userId": "910425853615", "comments": [], ...}

Two things the v6 parser got wrong and this module fixes:

* ``tookAt`` is ISO-8601 *with* a UTC offset, not ``"YYYY-MM-DD HH:MM:SS"``.
* ``mediaType`` is ``"photo"`` or ``"movie"``; ``expiringVideoUrl`` points at an
  HLS *playlist* and ``expiringUrl`` at a poster frame, so neither URL is a
  reliable source for the file extension. ``contentType`` is.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

# contentType -> extension. The download endpoint returns the *original* file,
# so this is authoritative; the response's own Content-Type is used as a
# cross-check at download time and the expiring URL only as a last resort.
CONTENT_TYPE_EXTENSIONS: Dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/tiff": ".tiff",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-m4v": ".m4v",
    "video/3gpp": ".3gp",
    "video/webm": ".webm",
}

VIDEO_MEDIA_TYPES = frozenset({"movie", "video"})

# Characters no mainstream filesystem enjoys, plus control characters.
_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')

DEFAULT_FILENAME_TEMPLATE = "{date}_{time}-{uuid8}"


def sanitize_filename(name: str) -> str:
    """Make ``name`` safe on Windows, macOS and Linux."""
    cleaned = _UNSAFE.sub("_", name).replace(" ", "_")
    cleaned = re.sub(r"_+", "_", cleaned).strip("._ ")
    return cleaned or "unnamed"


def _coerce_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_took_at(raw: Any) -> Optional[datetime]:
    """Parse mitene's ``tookAt`` into a timezone-aware :class:`datetime`.

    Accepts the real ISO-8601 form (``2026-07-31T03:59:44+09:00``), the
    ``Z``-suffixed variant, and the space-separated form v6 assumed -- so old
    fixtures and any future serialization change both keep working.
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Comment:
    """A single comment on a media item."""

    body: str
    nickname: str
    user_id: Optional[str]
    created_at: Optional[datetime]
    is_deleted: bool
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_gon(cls, data: Dict[str, Any]) -> "Comment":
        user = data.get("user") or {}
        return cls(
            body=str(data.get("body") or ""),
            nickname=str(user.get("nickname") or "Unknown"),
            user_id=str(user.get("id")) if user.get("id") is not None else None,
            created_at=parse_took_at(data.get("createdAt")),
            is_deleted=bool(data.get("isDeleted")),
            raw=data,
        )


@dataclass
class MediaItem:
    """One photo or movie, normalized."""

    uuid: str
    media_type: str
    content_type: Optional[str]
    took_at: Optional[datetime]
    original_hash: Optional[str]
    expiring_url: Optional[str]
    expiring_video_url: Optional[str]
    expiring_thumb_url: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    width: Optional[int]
    height: Optional[int]
    orientation: Optional[int]
    device_model: Optional[str]
    video_duration_ms: Optional[int]
    user_id: Optional[str]
    audience_type: Optional[str]
    origin: Optional[str]
    comments: List[Comment] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_gon(cls, data: Dict[str, Any]) -> "MediaItem":
        """Build from one entry of ``gon.media.mediaFiles``.

        Missing or malformed fields become ``None`` rather than ``""`` so that
        "absent" is distinguishable from "empty" when filtering.
        """
        comments = [
            Comment.from_gon(c)
            for c in (data.get("comments") or [])
            if isinstance(c, dict)
        ]
        content_type = data.get("contentType")
        return cls(
            uuid=str(data.get("uuid") or ""),
            media_type=str(data.get("mediaType") or "photo").lower(),
            content_type=str(content_type).lower() if content_type else None,
            took_at=parse_took_at(data.get("tookAt")),
            original_hash=(
                str(data["originalHash"]).lower() if data.get("originalHash") else None
            ),
            expiring_url=data.get("expiringUrl") or None,
            expiring_video_url=data.get("expiringVideoUrl") or None,
            expiring_thumb_url=data.get("expiringThumbUrl") or None,
            latitude=_coerce_float(data.get("latitude")),
            longitude=_coerce_float(data.get("longitude")),
            width=_coerce_int(data.get("mediaWidth")),
            height=_coerce_int(data.get("mediaHeight")),
            orientation=_coerce_int(data.get("mediaOrientation")),
            device_model=str(data["mediaDeviceModel"]) if data.get("mediaDeviceModel") else None,
            video_duration_ms=_coerce_int(data.get("videoDuration")),
            user_id=str(data["userId"]) if data.get("userId") is not None else None,
            audience_type=data.get("audienceType") or None,
            origin=data.get("origin") or None,
            comments=comments,
            raw=data,
        )

    # -- derived properties ----------------------------------------------

    @property
    def is_video(self) -> bool:
        return self.media_type in VIDEO_MEDIA_TYPES

    @property
    def kind(self) -> str:
        """Top-level destination folder: ``videos`` or ``photos``."""
        return "videos" if self.is_video else "photos"

    @property
    def has_location(self) -> bool:
        """True when GPS is present and not the ``0, 0`` "unknown" sentinel."""
        if self.latitude is None or self.longitude is None:
            return False
        return not (self.latitude == 0 and self.longitude == 0)

    def extension(self, response_content_type: Optional[str] = None) -> str:
        """Best-known file extension.

        Preference order: the response's own ``Content-Type`` (ground truth for
        the bytes on disk), then the item's ``contentType``, then a sensible
        default per media type. The expiring URLs are deliberately *not*
        consulted -- they point at derived renditions (``.webp`` previews,
        ``.jpg`` posters) and produced v6's mislabelled files.
        """
        for candidate in (response_content_type, self.content_type):
            if not candidate:
                continue
            base = candidate.split(";")[0].strip().lower()
            if base in CONTENT_TYPE_EXTENSIONS:
                return CONTENT_TYPE_EXTENSIONS[base]
        # Last resort: the original upload's suffix, if the URL happens to carry
        # one that looks plausible for this media type.
        url = self.expiring_url or ""
        suffix = PurePosixPath(urllib.parse.urlparse(url).path).suffix.lower()
        if suffix and not self.is_video and suffix in CONTENT_TYPE_EXTENSIONS.values():
            return suffix
        return ".mp4" if self.is_video else ".jpg"

    @property
    def year_month(self) -> str:
        """``YYYY/MM`` sub-path, or ``unknown/unknown`` when the date is absent."""
        if self.took_at is None:
            return "unknown/unknown"
        return f"{self.took_at:%Y/%m}"

    def filename(
        self,
        template: str = DEFAULT_FILENAME_TEMPLATE,
        response_content_type: Optional[str] = None,
    ) -> str:
        """Render the destination filename (including extension)."""
        took = self.took_at
        fields = {
            "uuid": self.uuid,
            "uuid8": self.uuid[:8] if self.uuid else "unknown",
            "date": f"{took:%Y-%m-%d}" if took else "unknown-date",
            "time": f"{took:%H%M%S}" if took else "000000",
            "datetime": f"{took:%Y-%m-%d_%H%M%S}" if took else "unknown-date",
            "iso": took.isoformat() if took else "unknown-date",
            "year": f"{took:%Y}" if took else "unknown",
            "month": f"{took:%m}" if took else "unknown",
            "day": f"{took:%d}" if took else "unknown",
            "type": self.media_type,
            "user": self.user_id or "unknown",
            "device": self.device_model or "unknown",
        }
        try:
            stem = template.format(**fields)
        except (KeyError, IndexError, ValueError):
            stem = DEFAULT_FILENAME_TEMPLATE.format(**fields)
        return sanitize_filename(stem) + self.extension(response_content_type)

    def to_sidecar(self) -> Dict[str, Any]:
        """Lossless-ish record written next to the media when ``--sidecar``."""
        return {
            "uuid": self.uuid,
            "media_type": self.media_type,
            "content_type": self.content_type,
            "took_at": self.took_at.isoformat() if self.took_at else None,
            "original_hash": self.original_hash,
            "width": self.width,
            "height": self.height,
            "orientation": self.orientation,
            "device_model": self.device_model,
            "latitude": self.latitude if self.has_location else None,
            "longitude": self.longitude if self.has_location else None,
            "video_duration_ms": self.video_duration_ms or None,
            "user_id": self.user_id,
            "audience_type": self.audience_type,
            "origin": self.origin,
            "comments": [c.raw for c in self.comments],
        }


@dataclass
class Album:
    """One page of an album, plus the album-level context from ``gon``."""

    items: List[MediaItem]
    has_next: bool
    has_prev: bool
    current_page: int
    user_ids: List[str] = field(default_factory=list)

    @classmethod
    def from_gon(cls, gon: Dict[str, Any]) -> "Album":
        media = gon.get("media") or {}
        raw_items = media.get("mediaFiles") or []
        roster = gon.get("familyUserIdToColorMap") or {}
        return cls(
            items=[MediaItem.from_gon(m) for m in raw_items if isinstance(m, dict)],
            has_next=bool(media.get("hasNext")),
            has_prev=bool(media.get("hasPrev")),
            current_page=_coerce_int(media.get("currentPage")) or 1,
            user_ids=[str(k) for k in roster],
        )
