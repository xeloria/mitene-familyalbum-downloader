# Changelog

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
