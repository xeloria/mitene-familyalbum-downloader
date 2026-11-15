"""Command line interface."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .album import Summary, process_album, repair_directory, sync_albums
from .api import normalize_album_url
from .cache import DatabaseCache
from .errors import MiteneError
from .models import DEFAULT_FILENAME_TEMPLATE
from .options import Options

logger = logging.getLogger("mitene_download")

BANNER = r"""
 __  __ _ _                    _____                      _                 _
|  \/  (_) |                  |  __ \                    | |               | |
| \  / |_| |_ ___ _ __   ___  | |  | | _____      ___ __ | | ___   __ _  __| | ___ _ __
| |\/| | | __/ _ \ '_ \ / _ \ | |  | |/ _ \ \ /\ / / '_ \| |/ _ \ / _` |/ _` |/ _ \ '__|
| |  | | | ||  __/ | | |  __/ | |__| | (_) \ V  V /| | | | | (_) | (_| | (_| |  __/ |
|_|  |_|_|\__\___|_| |_|\___| |_____/ \___/ \_/\_/ |_| |_|_|\___/ \__,_|\__,_|\___|_| v%s
""" % __version__


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            # Without this, ``logger.exception`` lost the traceback in JSON mode.
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(verbose: bool, json_log: bool) -> None:
    handler = logging.StreamHandler()
    if json_log:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(levelname)s - %(message)s", "%Y-%m-%d %H:%M:%S"
            )
        )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def _parse_date(value: Optional[str], flag: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit(
            f"{flag} must be in YYYY-MM-DD format, got {value!r}"
        ) from None


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ignoring unreadable %s: %s", path, exc)
        return {}


def build_parser(
    config: Dict[str, Any], config_path: str = "config.json"
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mitene_download",
        description="Download and archive media from a Mitene (FamilyAlbum) share link.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--config", default=config_path,
        help="JSON defaults file",
    )
    parser.add_argument("--url", default=config.get("url"), help="Album share URL")
    parser.add_argument(
        "--password", default=config.get("password"),
        help="Album password (visible in the process list; prefer --password-stdin)",
    )
    parser.add_argument(
        "--password-stdin", action="store_true",
        help="Read the album password from stdin",
    )
    parser.add_argument("--dest", default=config.get("dest", "files"), help="Destination directory")
    parser.add_argument("--db", default=config.get("db", "cache.db"), help="SQLite cache path")
    parser.add_argument("--verbose", action="store_true", default=config.get("verbose", False))
    parser.add_argument("--json-log", action="store_true", default=config.get("json_log", False),
                        help="Emit machine-readable logs (for cron)")

    filters = parser.add_argument_group("filters")
    filters.add_argument("--start-date", default=config.get("start_date"), help="Only media taken on/after YYYY-MM-DD")
    filters.add_argument("--end-date", default=config.get("end_date"), help="Only media taken on/before YYYY-MM-DD")
    filters.add_argument("--media-type", choices=["photos", "videos", "all"],
                         default=config.get("media_type", "all"))
    filters.add_argument("--uploader", action="append", default=config.get("uploader") or [],
                         help="Only media from this user id (repeatable)")
    filters.add_argument("--limit", type=int, default=config.get("limit"),
                         help="Stop after N matching items")

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument("--concurrency", type=int, default=config.get("concurrency", 4),
                           help="Simultaneous downloads per album")
    behaviour.add_argument("--album-concurrency", type=int, default=config.get("album_concurrency", 1),
                           help="Albums processed at once in sync mode")
    behaviour.add_argument("--max-retries", type=int, default=config.get("max_retries", 4))
    behaviour.add_argument("--timeout", type=int, default=config.get("timeout", 1200),
                           help="Per-download timeout in seconds")
    behaviour.add_argument("--dry-run", action="store_true", help="Report what would be downloaded, write nothing")
    behaviour.add_argument(
        "--incremental",
        action="store_true",
        default=config.get("incremental", False),
        help="Stop paging once a page is already fully archived",
    )
    behaviour.add_argument("--sync", action="store_true", default=config.get("sync", False),
                           help="Headless single pass over saved albums")
    watch_default = config.get("watch")
    if isinstance(watch_default, bool):
        # ``"watch": true`` in config.json should mean "poll", not 1 second.
        watch_default = 300 if watch_default else None
    behaviour.add_argument("--watch", nargs="?", type=int, const=300, default=watch_default,
                           metavar="SECONDS", help="Poll continuously every N seconds (default 300)")
    behaviour.add_argument("--repair", action="store_true",
                           help="Fix wrong file extensions left by older versions, in place")

    output = parser.add_argument_group("output")
    output.add_argument("--filename-template", default=config.get("filename_template", DEFAULT_FILENAME_TEMPLATE),
                        help="Fields: {date} {time} {datetime} {iso} {uuid} {uuid8} {year} {month} {day} {type} {user} {device}")
    output.add_argument("--comment-format", choices=["md", "json", "both", "none"],
                        default=config.get("comment_format", "md"))
    output.add_argument("--sidecar", action="store_true", default=config.get("sidecar", False),
                        help="Write a <media>.json record beside each file")
    output.add_argument("--no-exif", action="store_true", default=config.get("no_exif", False),
                        help="Skip EXIF writing (file times are still set)")
    output.add_argument("--exiftool", action="store_true", default=config.get("use_exiftool", False),
                        help="Use the exiftool binary for metadata (HEIC/PNG/MP4 too)")
    output.add_argument("--index", action="store_true", default=config.get("index", False),
                        help="Write index.html and index.csv into the destination")
    return parser


def _as_list(value: Any) -> List[str]:
    """Normalize a repeatable option that config.json may hold as a scalar."""
    if value is None:
        return []
    if isinstance(value, (str, int)):
        return [str(value)]
    return [str(item) for item in value]


def options_from_args(
    args: argparse.Namespace, config: Optional[Dict[str, Any]] = None
) -> Options:
    config = config or {}
    password = args.password
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    elif password:
        logger.warning(
            "--password is visible to other users via the process list; "
            "consider --password-stdin."
        )
    return Options(
        dest=args.dest,
        db_path=args.db,
        password=password,
        start_date=_parse_date(args.start_date, "--start-date"),
        end_date=_parse_date(args.end_date, "--end-date"),
        media_type=args.media_type,
        uploader=_as_list(args.uploader),
        limit=args.limit,
        concurrency=max(1, args.concurrency),
        album_concurrency=max(1, args.album_concurrency),
        max_retries=max(1, args.max_retries),
        timeout=max(1, args.timeout),
        request_timeout=max(1, int(config.get("request_timeout", 120))),
        dry_run=args.dry_run,
        incremental=args.incremental,
        quiet=bool(args.sync or args.watch or args.json_log),
        verbose=args.verbose,
        filename_template=args.filename_template,
        comment_format=args.comment_format,
        sidecar=args.sidecar,
        write_exif=not args.no_exif,
        use_exiftool=args.exiftool,
        index=args.index,
    )


async def _resolve_urls(args: argparse.Namespace, options: Options) -> List[str]:
    if args.url:
        return [str(args.url)]
    cache = DatabaseCache(options.db_path)
    await cache.init_db()
    return await cache.get_all_album_urls()


async def run_watch(urls: List[str], options: Options, interval: int) -> Summary:
    """Poll the album(s) forever, downloading anything new each pass.

    This is the loop the README has always advertised; ``--sync`` remains a
    single pass for cron-driven setups.
    """
    interval = max(1, interval)  # a 0-second interval would spin the CPU
    total = Summary()
    logger.info("Watching %d album(s) every %ds. Ctrl-C to stop.", len(urls), interval)
    while True:
        try:
            summary = await sync_albums(urls, options)
            total.merge(summary)
            logger.info(
                "Pass complete: %d new, %d skipped, %d failed.",
                summary.downloaded, summary.skipped, summary.failed,
            )
        except MiteneError as exc:
            logger.error("Pass failed: %s", exc)
        except Exception as exc:  # noqa: BLE001 - keep the watcher alive
            logger.exception("Unexpected failure during watch pass: %s", exc)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return total


async def interactive_mode(args: argparse.Namespace, options: Options) -> Summary:
    print(BANNER)
    cache = DatabaseCache(options.db_path)
    await cache.init_db()

    while True:
        urls = await cache.get_all_album_urls()
        if urls:
            print("\nSaved albums:")
            for index, url in enumerate(urls, start=1):
                print(f"  {index}: {url}")
        else:
            print("\nNo saved albums yet.")

        choice = input("Choice ('a' add, 'd' delete, 'x' exit): ").strip().lower()

        if choice == "a":
            url = input("Album URL: ").strip()
            if url:
                await cache.save_album_url(normalize_album_url(url))
        elif choice == "d":
            if not urls:
                print("Nothing to delete.")
                continue
            raw = input("Number to delete: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(urls):
                await cache.delete_album_url(urls[int(raw) - 1])
            else:
                print("Invalid selection.")
        elif choice == "x":
            return Summary()
        elif choice.isdigit() and 1 <= int(choice) <= len(urls):
            url = urls[int(choice) - 1]
            if not options.password:
                if input("Does the album need a password? (y/n): ").strip().lower() == "y":
                    import getpass

                    options.password = getpass.getpass("Password: ")
            print("Starting download...")
            summary = await process_album(url, options, cache)
            print(summary.render())
            return summary
        else:
            print("Invalid selection.")


async def _run(args: argparse.Namespace, options: Options) -> Summary:
    if args.watch and args.dry_run:
        raise SystemExit("--watch cannot be combined with --dry-run.")
    if args.incremental and args.dry_run:
        raise SystemExit("--incremental cannot be combined with --dry-run.")

    if args.repair:
        return await repair_directory(
            Path(options.dest), options.dry_run, options.db_path
        )

    urls = await _resolve_urls(args, options)

    if args.watch:
        if not urls:
            raise SystemExit("--watch needs --url or at least one saved album.")
        return await run_watch(urls, options, int(args.watch))

    if args.sync:
        if not urls:
            logger.warning("No album URLs to sync.")
            return Summary()
        return await sync_albums(urls, options)

    if args.url:
        return await process_album(str(args.url), options)

    return await interactive_mode(args, options)


def _config_path_from_argv(argv: List[str]) -> Path:
    """Find ``--config`` before argparse exists, so it can supply defaults."""
    for index, arg in enumerate(argv):
        if arg == "--config" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if arg.startswith("--config="):
            return Path(arg.split("=", 1)[1])
    return Path("config.json")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    config_path = _config_path_from_argv(argv)
    config = load_config(config_path)
    parser = build_parser(config, str(config_path))
    args = parser.parse_args(argv)

    configure_logging(args.verbose, args.json_log)
    options = options_from_args(args, config)

    try:
        summary = asyncio.run(_run(args, options))
    except (KeyboardInterrupt, EOFError):
        logger.info("Interrupted by user.")
        return 130
    except MiteneError as exc:
        logger.error("%s", exc)
        return 2
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fatal error: %s", exc)
        return 1

    if args.json_log:
        # Logs go to stderr, so stdout stays a single parseable document.
        print(json.dumps(summary.to_dict(), ensure_ascii=False))
    else:
        print(summary.render())
    # A partial archive must not look like a success to a cron job.
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
