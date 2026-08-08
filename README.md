# Mitene Family Album Downloader (v7.0)

An asynchronous Python tool that archives media from a Mitene (FamilyAlbum) share link — with the **original files**, correct extensions, real EXIF metadata, and verified integrity.

## ✨ What's new in v7

v7 is a rewrite driven by checking the tool's assumptions against a live album. Several were wrong:

| Was | Now |
| :--- | :--- |
| Photos saved as `.webp`, **every video saved as `.jpg`** — names came from the preview/poster URL | Extensions come from the real `contentType` and the download response, so JPEGs are `.jpg` and MP4s are `.mp4` |
| EXIF injection **never ran** — its regex expected `YYYY-MM-DD HH:MM:SS`, but mitene sends `2026-07-31T03:59:44+09:00` | Full ISO-8601 parsing; `DateTimeOriginal`, `DateTimeDigitized` **and** the timezone offset are written |
| No GPS, camera, or orientation | GPS coordinates, camera model and orientation written to EXIF; file mtime set for every file including videos |
| `--end-date` silently dropped the final day | Real `date` comparison |
| Resume appended blindly, corrupting the file if the server ignored `Range` | Verifies `206` before appending; restarts cleanly on `200` |
| A wrong password looked like an empty album and exited `0` | Typed errors and a non-zero exit code |
| "All downloads completed successfully" even when files failed | Summary of downloaded / skipped / failed, with a non-zero exit |
| Comment numbering ran continuously across the album | Numbering restarts per file |
| Cache wrote 4 of its 14 columns, nulling the rest on every update | Real upserts; every file's SHA-1 and byte count are recorded, and short downloads are rejected |
| `--sync` was a single pass despite advertising "watch mode" | `--sync` is still a single pass; `--watch` is the real loop |

Plus: `--dry-run`, `--limit`, `--concurrency`, `--uploader`, `--sidecar`, `--index`, `--filename-template`, `--password-stdin`, `--json-log`, `--repair`, and a test suite.

## 🚀 Installation

```bash
pip install -e ".[dev]"
```

Requires Python 3.11+. (Plain `pip install -r requirements.txt` also works if you just want to run it.)

## 📖 Usage

```bash
mitene_download --url https://mitene.us/f/XXXXXXXX --dest ./archive
```

Interactive menu (manage saved albums):

```bash
mitene_download
```

Preview without writing anything:

```bash
mitene_download --url https://mitene.us/f/XXXXXXXX --dry-run --limit 20
```

Password-protected album, without leaking the password into the process list:

```bash
printf '%s' "$ALBUM_PASSWORD" | mitene_download --url https://mitene.us/f/XXXXXXXX --password-stdin
```

Keep an archive continuously up to date:

```bash
mitene_download --watch 600 --dest ./archive --index
```

One-shot pass over every saved album, for cron or Task Scheduler:

```bash
mitene_download --sync --json-log
```

Upgrading from v6? Fix the wrongly-named files already on disk — no re-downloading:

```bash
mitene_download --repair --dest ./archive
```

### A note on `originalHash`

Album pages expose an `originalHash` field that looks like a SHA-1 integrity digest. It isn't one for our purposes: checked against a live album, it does **not** match the bytes the download endpoint serves — it's computed on the uploading device. It's stored as `expected_checksum` for identity and de-duplication, but downloads are validated against `Content-Length` instead, which is the signal the server actually honours.

## 📂 Output layout

```
archive/
  photos/2026/07/2026-07-31_035944-d9976283.jpg
  videos/2026/07/2026-07-24_235041-07203a17.mp4
  comments/2026/07/2026-07-24_235041-07203a17.md
  index.html        # with --index
  index.csv
```

## 📝 CLI reference

| Argument | Description | Default |
| :--- | :--- | :--- |
| `--url` | Album share URL | None |
| `--password` | Album password (visible in the process list) | None |
| `--password-stdin` | Read the password from stdin instead | `False` |
| `--dest` | Destination directory | `files` |
| `--db` | SQLite cache path | `cache.db` |
| `--verbose` / `--json-log` | Debug logging / machine-readable logs | `False` |
| `--start-date` / `--end-date` | Filter by date taken (`YYYY-MM-DD`, inclusive) | None |
| `--media-type` | `photos`, `videos`, or `all` | `all` |
| `--uploader` | Only this user id (repeatable) | None |
| `--limit` | Stop after N matching items | None |
| `--concurrency` | Simultaneous downloads | `4` |
| `--album-concurrency` | Albums processed at once in sync mode | `1` |
| `--max-retries` | Attempts per file | `4` |
| `--dry-run` | Report the plan, write nothing | `False` |
| `--sync` | Headless single pass over saved albums | `False` |
| `--watch [SECONDS]` | Poll continuously | `300` when bare |
| `--repair` | Fix extensions left by older versions, in place | `False` |
| `--filename-template` | `{date} {time} {datetime} {uuid} {uuid8} {year} {month} {day} {type} {user} {device}` | `{date}_{time}-{uuid8}` |
| `--comment-format` | `md`, `json`, `both`, `none` | `md` |
| `--sidecar` | Write `<media>.json` beside each file | `False` |
| `--no-exif` | Skip EXIF (file times still set) | `False` |
| `--index` | Write `index.html` + `index.csv` | `False` |

Exit codes: `0` success, `1` some files failed, `2` album error (bad password, unparseable page), `130` interrupted.

## 🗂️ Project layout

```
mitene_download/     the installed package -- runtime code only
  models.py            normalizes the album's JSON payload
  api.py               auth, pagination, page parsing
  download.py          streaming, resume, verification
  metadata.py          EXIF, GPS, file times, sidecars, comments
  cache.py             SQLite download state
  album.py             orchestration, index, repair
  options.py           run configuration
  cli.py               argument parsing and dispatch
tests/               not installed; needed only to modify the tool
config.example.json  copy to config.json to set defaults
```

## 🛠️ Configuration (`config.json`)

Copy `config.example.json` to `config.json`. Any long option can be defaulted there:

```json
{
  "dest": "archive",
  "media_type": "all",
  "comment_format": "both",
  "concurrency": 6,
  "sidecar": true,
  "index": true
}
```

Storing `password` here means keeping it in plaintext; prefer `--password-stdin`.

## 🧪 Development

```bash
pytest
```

The suite runs entirely offline against a local `aiohttp` server and fixtures modelled on real album pages. `mitene_download_v6_legacy.py.bak` is the previous single-file version, kept for reference only.

## ⚖️ License

MIT — see [LICENSE](LICENSE).
