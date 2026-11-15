# Changelog

## 7.1.0

### Added

- `--incremental`: stop paging as soon as a page is fully archived. A page is
  only trusted once a previous pass reached the end of the album (`album_state`
  in the cache), so an earlier `--limit` run cannot hide older items. Files that
  were deleted are still re-fetched.
- `--exiftool`: use an installed `exiftool` binary for metadata, covering HEIC,
  PNG, WebP, MOV and MP4 as well as JPEG; also used as a fallback when piexif
  fails. Commands are argument lists, never shell strings, and use absolute
  paths so a filename cannot be read as an option.
- Re-authentication during downloads: a 401/403 on the media endpoint asks
  `AlbumClient` to restore the session and retries the same Range. A bad
  password still aborts the album.
- `--repair` now rewrites media paths in an existing `index.html` and
  `index.csv`, so repaired files remain linked.
- Actionable diagnostics instead of "Could not find the gon.media payload":
  - `/2d/<code>` links are recognised as one-time browser invitations and
    explain that they cannot be reused.
  - 404/410 responses and the "Page Not Found / browser-version accounts are
    disabled" page raise `AlbumNotFoundError` immediately (no retry storm).
  - `/web/login` is identified as the computer-upload login, not an album.
  - 200-status error pages are inspected for the same messages.
- `https://family-album.com/f/<token>` is explicitly supported and tested
  alongside `https://mitene.us/f/<token>`.
- Lazy package exports: `from mitene_download import Options, process_album,
  sync_albums, repair_directory, Summary, DatabaseCache` without slowing
  `--help`/`--version`.
- Ruff configuration and a `lint` workflow; the unconfigured Pyre template was
  removed.

### Changed

- One combined index per `--sync`/`--watch` pass. Previously each concurrent
  album wrote its own `index.html` and the last writer won.
- `--limit` is a global cap in sync mode (`ItemBudget`) rather than N items per
  album, including with `--album-concurrency > 1`.
- Retry backoff honours `Retry-After` (capped at five minutes) for page,
  login and download requests.
- The test suite grew to 115 tests; version 7.1.0; source URL updated.

### Verified

Validated against a live 3,054-item album: password login, full pagination,
original-file downloads, JPEG magic bytes, EXIF date/offset/model, file mtimes,
index links, and repeat runs skipping without size warnings.

### Fixed

- **EXIF rewriting invalidated the recorded size.** piexif changes a JPEG's
  byte count after `mark_complete()` runs, so the next run saw a size mismatch
  and would re-download every photo. The cache now records the final
  post-metadata size and checksum, and legacy rows (which recorded the
  pre-EXIF size) migrate on first adoption instead of triggering a full
  re-download.
- A pasted `https://mitene.us/f/<token>/login` URL produced `<url>/login/login`
  during authentication; the `/login` suffix is now stripped by
  `normalize_album_url()`. Stripping is limited to `/f/` and `/2d/` paths so
  `/web/login` is not mangled into `/web`.
- Size verification of an existing file could be defeated by the same-stem
  adoption fallback: a truncated file was reported as "complete" and, with
  `--incremental`, could be trusted forever. Mismatched files are now excluded
  from adoption, and the incremental page check uses the same size rules.
- A filtered pass (`--media-type`, date range, `--uploader`) is no longer
  recorded as a completed album, so a later `--incremental` run cannot skip a
  category the filtered pass never fetched.
- Pagination hitting the 10,000-page safety cap now raises instead of exiting
  successfully with a truncated archive.
- Non-retryable errors (400/401/404, ENOSPC, EACCES, EROFS) fail immediately.

## 7.0.1

### Fixed

- **Orphaned `.part` files.** Partial downloads were reconstructed from the
  item's payload, but the extension is only known from the response's
  `Content-Type`. When the two disagreed, truncation cleanup and resume both
  looked for the wrong file. Partials are now discovered by a glob on the
  (escaped) template stem.
- **`--repair` broke the cache.** Renaming files on disk left `cache_filename`
  pointing at the old path, so the next run considered every repaired file
  missing and downloaded the album again. Repair now updates the cache.
- **`--repair` could rename in-flight downloads and sidecars.** `.part`, `.json`
  and `.tmp` files are skipped.
- **AVI/WAV renamed to `.webp` by `--repair`.** The magic table had a bare
  `RIFF` entry; only `RIFF....WEBP` is claimed now.
- **Wrong links in `index.html`/`index.csv`** when the extension came from the
  response rather than `contentType`. The index now records the path each item
  actually landed on.
- **`--dry-run` wrote files.** It created the destination directory and index,
  and `--repair --dry-run` created the cache database.
- **`--json-log` printed a human summary** after JSON logs. The summary is now
  a single JSON document on stdout, and tracebacks are included in log records.
- **`--max-retries` was ignored for page and login requests.**
- **Database lock errors** under `--concurrency`/`--album-concurrency > 1`.
  SQLite now uses WAL and a 30-second busy timeout.
- **A crashed consumer could hang the producer** on a full queue; the pipeline
  now waits on completion or first failure.
- **Downloads could adopt the wrong same-stem file** (or a sidecar); candidates
  are restricted to media, and a recorded file size is verified before a file
  is skipped.
- `.part` cleanup no longer leaks when the server changes the extension between
  attempts.
- Non-retryable failures (HTTP 400/401/404, out of disk space, permission denied,
  read-only filesystem) fail immediately instead of retrying with backoff.
- `--password-stdin` now strips `\r\n`, so Windows pipelines work.
- CLI: `--watch` clamps its interval, survives unexpected errors, and refuses
  to combine with `--dry-run`; saved URLs are normalized.

### Added

- `--config PATH` to load defaults from anywhere.
- `--timeout SECONDS` for downloads; `request_timeout` is configurable.
- `--repair` reports a `Renamed` count instead of counting renames as downloads.
- `_JsonFormatter` includes exception detail.
- Offline test suite (`tests/`, 76 tests) and a `[dev]` extra, so the existing
  CI workflow installs and runs successfully.
- README development section; real security policy.

### Changed

- `Summary` gains `planned`, `renamed`, and `to_dict()`; index writes are atomic.
- Cache album URLs are listed in stable order.
- EXIF: GPS coordinates outside valid ranges are treated as absent.
- Filenames avoid Windows reserved device names (`CON`, `LPT1`, ...).
- Magic-byte sniffing recognises AVIF, 3GP and more MP4 brands.
- Version bumped to 7.0.1 and the wheel/sdist rebuilt.

## 7.0.0

Rewrite driven by checking the tool's assumptions against a live album. Several
were wrong, and had been silently damaging archives.

### Fixed

- **Wrong file extensions.** Names were derived from `expiringUrl`, which is a
  re-encoded `.webp` preview for photos and a `.jpg` poster frame for movies.
  Every video was saved as `.jpg`. Extensions now come from the real
  `contentType` and the download response's `Content-Type`.
- **EXIF injection never ran.** The regex expected `YYYY-MM-DD HH:MM:SS`;
  mitene sends ISO-8601 with an offset (`2026-07-31T03:59:44+09:00`).
- **`--end-date` dropped the final day**, comparing a full ISO timestamp
  against a `YYYY-MM-DD` string. Dates are now parsed and compared as dates.
- **Corrupt resume.** A `Range` request answered with `200` (full body) was
  appended to the existing partial. The status is now checked; a `200`
  restarts cleanly.
- **Silent failures.** A wrong password looked like an empty album and exited
  `0`; failed files still printed "All downloads completed successfully".
  Errors are typed and the exit code reflects the outcome.
- **Comment numbering** ran continuously across the whole album instead of
  restarting per file.
- **Cache corruption.** `REPLACE INTO` wrote 4 of 14 columns, nulling the rest
  on every progress update. Replaced with a real upsert.
- Pagination fetched one extra page to discover an empty result; it now stops
  on the `hasNext` flag the payload already provides.
- The `gon.media` parser required one exact minified serialization. It now
  brace-matches the JSON and tolerates reformatting.
- The cache could not create its own parent directory, so `--db` pointing into
  a not-yet-existing `--dest` crashed.

### Added

- `--watch [SECONDS]` — the real polling loop the README always advertised.
  `--sync` remains a single pass for cron.
- `--dry-run`, `--limit`, `--concurrency`, `--album-concurrency`, `--uploader`
- `--sidecar` (per-file JSON record), `--index` (HTML gallery + CSV)
- `--filename-template`, `--comment-format none`, `--no-exif`
- `--password-stdin`, so the password stays out of the process list
- `--json-log` for machine-readable output
- `--repair` — fixes extensions left by v6 in place, without re-downloading
- GPS, camera model and orientation written to EXIF; file mtime set for every
  file, including videos and non-JPEG formats
- Downloads stream to `<name>.part` and are renamed on success, so an
  interrupted run never leaves a plausible-looking complete file
- Downloads start while pagination is still running, rather than after it
- Test suite (70 tests, fully offline) and a CI workflow that runs it

### Changed

- Split the single 531-line module into a package.
- `requires-python` lowered from `>=3.14` to `>=3.11`.
- Declared the missing `aiofiles` dependency and dropped the spurious
  `argparse` one — a clean install from `pyproject.toml` previously failed.
- Default filename format is now `{date}_{time}-{uuid8}`, e.g.
  `2026-07-31_035944-d9976283.jpg`. Use `--filename-template` to change it.

### Notes

`originalHash` in the album payload looks like an integrity digest but does not
match the bytes the download endpoint serves — it is computed on the uploading
device. It is stored for identity and de-duplication; downloads are validated
against `Content-Length`, which the server does honour.
