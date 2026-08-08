"""Post-download metadata: EXIF, GPS, file times, sidecars and comments.

v6's EXIF injection never ran: its regex expected ``YYYY-MM-DD HH:MM:SS`` while
mitene sends ``2026-07-31T03:59:44+09:00``, and it keyed off a ``.jpg`` suffix
that photos never got. Both are fixed here, and the offset -- previously thrown
away -- is preserved as ``OffsetTimeOriginal``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import piexif  # type: ignore

from .models import Comment, MediaItem

logger = logging.getLogger(__name__)

JPEG_SUFFIXES = {".jpg", ".jpeg"}


def _deg_to_rational(value: float) -> Tuple[Tuple[int, int], ...]:
    """Convert decimal degrees to EXIF's degrees/minutes/seconds rationals."""
    value = abs(value)
    degrees = int(value)
    minutes_float = (value - degrees) * 60
    minutes = int(minutes_float)
    seconds = round((minutes_float - minutes) * 60 * 10000)
    return ((degrees, 1), (minutes, 1), (seconds, 10000))


def _gps_ifd(latitude: float, longitude: float) -> Dict[int, Any]:
    return {
        piexif.GPSIFD.GPSVersionID: (2, 0, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: b"N" if latitude >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: _deg_to_rational(latitude),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if longitude >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: _deg_to_rational(longitude),
    }


def set_file_times(path: Path, item: MediaItem) -> None:
    """Set mtime/atime from ``tookAt``.

    Applied to every media type, including videos and HEIC, so file managers and
    photo importers order the archive correctly even where EXIF is unavailable.
    """
    if item.took_at is None:
        return
    try:
        timestamp = item.took_at.timestamp()
        os.utime(path, (timestamp, timestamp))
    except OSError as exc:
        logger.debug("Could not set file times on %s: %s", path, exc)


def write_exif(path: Path, item: MediaItem) -> bool:
    """Write date, GPS, orientation and camera model into a JPEG.

    Returns ``True`` when EXIF was written. Non-JPEG files are skipped (piexif
    only handles JPEG/TIFF) and left to :func:`set_file_times` and the sidecar.
    """
    if path.suffix.lower() not in JPEG_SUFFIXES:
        return False

    try:
        try:
            exif_dict: Dict[str, Any] = piexif.load(str(path))
        except Exception:  # noqa: BLE001 - file may have no EXIF block at all
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
        exif_dict.setdefault("0th", {})
        exif_dict.setdefault("Exif", {})
        exif_dict.setdefault("GPS", {})

        if item.took_at is not None:
            stamp = item.took_at.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
            exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = stamp
            exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = stamp
            exif_dict["0th"][piexif.ImageIFD.DateTime] = stamp
            offset = item.took_at.strftime("%z")
            if offset:
                # "+0900" -> "+09:00", the format EXIF 2.31 expects.
                formatted = f"{offset[:3]}:{offset[3:]}".encode("ascii")
                exif_dict["Exif"][piexif.ExifIFD.OffsetTimeOriginal] = formatted
                exif_dict["Exif"][piexif.ExifIFD.OffsetTimeDigitized] = formatted

        if item.device_model:
            exif_dict["0th"][piexif.ImageIFD.Model] = item.device_model.encode("utf-8")
        if item.orientation:
            exif_dict["0th"][piexif.ImageIFD.Orientation] = int(item.orientation)
        if item.has_location:
            assert item.latitude is not None and item.longitude is not None
            exif_dict["GPS"].update(_gps_ifd(item.latitude, item.longitude))

        piexif.insert(piexif.dump(exif_dict), str(path))
        return True
    except Exception as exc:  # noqa: BLE001 - metadata must never fail a download
        logger.debug("Could not write EXIF to %s: %s", path, exc)
        return False


def apply_metadata(path: Path, item: MediaItem, write_exif_data: bool = True) -> None:
    """Everything that happens to a file once its bytes are on disk."""
    if write_exif_data:
        write_exif(path, item)
    # After EXIF, since piexif.insert rewrites the file and resets mtime.
    set_file_times(path, item)


def write_sidecar(path: Path, item: MediaItem) -> Path:
    """Write ``<media>.json`` holding the full normalized record."""
    sidecar = path.with_name(path.name + ".json")
    sidecar.write_text(
        json.dumps(item.to_sidecar(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return sidecar


# -- comments -------------------------------------------------------------


def render_comments_markdown(item: MediaItem, comments: Iterable[Comment]) -> str:
    """Render one media item's comments.

    Numbering restarts per file. v6 shared a single counter across the whole
    album, so the second file's comments started at whatever number the first
    left off at.
    """
    lines: List[str] = []
    header = item.took_at.isoformat() if item.took_at else "unknown date"
    lines.append(f"# {item.uuid} ({header})\n")
    for number, comment in enumerate(
        (c for c in comments if not c.is_deleted), start=1
    ):
        when = f" _({comment.created_at:%Y-%m-%d %H:%M})_" if comment.created_at else ""
        lines.append(f"{number}. **{comment.nickname}**{when}: {comment.body}\n")
    return "\n".join(lines)


def write_comments(
    directory: Path,
    stem: str,
    item: MediaItem,
    comment_format: str,
) -> List[Path]:
    """Write comment files for one item; returns the paths written."""
    visible = [c for c in item.comments if not c.is_deleted]
    if not visible:
        return []

    directory.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    if comment_format in ("md", "both"):
        target = directory / f"{stem}.md"
        target.write_text(render_comments_markdown(item, visible), encoding="utf-8")
        written.append(target)

    if comment_format in ("json", "both"):
        target = directory / f"{stem}.json"
        target.write_text(
            json.dumps([c.raw for c in item.comments], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        written.append(target)

    return written


# -- repair ---------------------------------------------------------------

# Magic-number sniffing, used by --repair to fix files an earlier version named
# from the (wrong) rendition URL.
_MAGIC: List[Tuple[bytes, int, str]] = [
    (b"\xff\xd8\xff", 0, ".jpg"),
    (b"\x89PNG\r\n\x1a\n", 0, ".png"),
    (b"GIF8", 0, ".gif"),
    (b"RIFF", 0, ".webp"),  # refined below by checking bytes 8:12
    (b"ftypmp4", 4, ".mp4"),
    (b"ftypisom", 4, ".mp4"),
    (b"ftypmp42", 4, ".mp4"),
    (b"ftypqt", 4, ".mov"),
    (b"ftypheic", 4, ".heic"),
    (b"ftypmif1", 4, ".heic"),
    (b"\x1aE\xdf\xa3", 0, ".webm"),
]


def sniff_extension(path: Path) -> Optional[str]:
    """Return the extension implied by the file's magic bytes, if recognised."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(32)
    except OSError:
        return None
    if not head:
        return None
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    for magic, offset, extension in _MAGIC:
        if head[offset : offset + len(magic)] == magic:
            return extension
    return None
