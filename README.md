# 📸 Mitene Family Album Downloader

> An asynchronous Python archiver for **Mitene / FamilyAlbum** share links — original files, correct extensions, real EXIF, verified sizes, and resumable downloads.

[![Tests](https://github.com/xeloria/mitene-familyalbum-downloader/actions/workflows/tests.yml/badge.svg)](https://github.com/xeloria/mitene-familyalbum-downloader/actions/workflows/tests.yml)
[![Lint](https://github.com/xeloria/mitene-familyalbum-downloader/actions/workflows/lint.yml/badge.svg)](https://github.com/xeloria/mitene-familyalbum-downloader/actions/workflows/lint.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## ✨ Why this tool?

FamilyAlbum's web viewer is great for browsing, but a poor archive. This tool keeps a **local, restorable copy** of the album:

- 🖼️ **Original uploads** — downloads `/media_files/<uuid>/download`, not the `.webp` preview or `.jpg` video poster.
- 🧩 **Correct extensions** — derived from the real `contentType` and response headers, so photos are `.jpg` and videos are `.mp4`.
- 📅 **Real metadata** — `DateTimeOriginal`, timezone offset, camera model, orientation, and GPS written into JPEGs; file mtimes set for every format.
- 💬 **Comments preserved** — Markdown and/or JSON, nested per year/month.
- 🗂️ **Sidecars & gallery** — optional per-file JSON plus a self-contained `index.html` and `index.csv`.
- 🔁 **Safe resume** — streams to `.part`, validates `Content-Length`, handles servers that ignore `Range`, and only renames complete files.
- ⚡ **Incremental sync** — after one full pass, stop after the first page that is already archived.
- 🧰 **Repair mode** — fixes v6's mislabelled files in place and updates the cache and gallery links.
- 🛡️ **Honest errors** — expired links, used invitations, disabled browser accounts, and wrong login pages are explained instead of "payload not found".
- 🧪 **Offline test suite** — 115 tests, no network needed.
- 🖥️ **Cross-platform** — Windows, macOS and Linux; safe filenames and atomic writes.

---

## 📦 Installation

Requires **Python 3.11+**.

```bash
git clone https://github.com/xeloria/mitene-familyalbum-downloader.git
cd mitene-familyalbum-downloader
python -m pip install .
```

Development install (adds pytest + ruff):

```bash
python -m pip install -e ".[dev]"
```

Build a wheel and sdist locally:

```bash
python -m pip install build
python -m build
```

---

## 🚀 Quick start

Download an album:

```bash
mitene_download --url "https://mitene.us/f/XXXXXXXXXX" --dest ./archive
```

Password-protected album, without leaking the password into the process list:

```bash
printf '%s' "$ALBUM_PASSWORD" | mitene_download \
  --url "https://mitene.us/f/XXXXXXXXXX" --password-stdin
```

PowerShell:

```powershell
$env:ALBUM_PASSWORD | mitene_download --url "https://mitene.us/f/XXXXXXXXXX" --password-stdin
```

Preview without writing anything:

```bash
mitene_download --url "https://mitene.us/f/XXXXXXXXXX" --dry-run --limit 20
```

Keep an archive current:

```bash
mitene_download --url "https://mitene.us/f/XXXXXXXXXX" --watch 600 --dest ./archive --index
```

One pass over every saved album (cron / Task Scheduler):

```bash
mitene_download --sync --incremental --json-log
```

Interactive menu for saved albums:

```bash
mitene_download
```

---

## 🔗 Supported links & troubleshooting

Use the **album share URL** from the browser version:

| ✅ Supported | Notes |
| :--- | :--- |
| `https://mitene.us/f/<token>` | Standard share link |
| `https://family-album.com/f/<token>` | Alternate host, same album |
| Either URL with `/login` appended | Password page; the suffix is stripped automatically |

These are **not album links**:

| ❌ Not an album | Why |
| :--- | :--- |
| `https://family-album.com/2d/<code>` | One-time browser **invitation**; it can only be accepted once |
| `https://family-album.com/web/login` | Computer-upload site login; it cannot view albums |
| Bare `https://family-album.com/` or `https://mitene.us/` | Marketing / home page |

Common errors:

| Message | Meaning | Fix |
| :--- | :--- | :--- |
| 🎟️ `one-time FamilyAlbum invitation link` | The `/2d/...` invitation was already used or is invalid | Ask an album admin for a new browser invitation, accept it, then use the `/f/<token>` URL |
| 🚫 `share link is unknown, expired, or its browser-version account has been disabled` | The `/f/...` URL 404s; inactive browser accounts are disabled by FamilyAlbum | Ask an album admin to send a new invitation |
| 🔍 `Could not find the gon.media payload` | The URL is a home/login/error page, not an album page | Copy the album URL from the browser address bar |
| 🔒 `Invalid album password` | Wrong password, or the form format changed | Re-check the password; prefer `--password-stdin` |

> 💡 Invitation links are single-use and tied to the browser account creation flow. This tool intentionally consumes the durable `/f/<token>` album link, not the `/2d/...` invitation.

---

## 📂 Output layout

```text
archive/
├── photos/2026/07/2026-07-31_035944-d9976283.jpg
├── videos/2026/07/2026-07-24_235041-07203a17.mp4
├── comments/2026/07/2026-07-24_235041-07203a17.md
├── photos/2026/07/2026-07-31_035944-d9976283.jpg.json   # with --sidecar
├── index.html                                          # with --index
└── index.csv
```

In `--sync` / `--watch`, one combined index covers every album in the pass.

---

## 🧭 CLI reference

| Argument | Description | Default |
| :--- | :--- | :--- |
| `--config` | JSON file providing defaults | `config.json` |
| `--url` | Album share URL | None |
| `--password` | Album password (visible in the process list) | None |
| `--password-stdin` | Read the password from stdin instead | `False` |
| `--dest` | Destination directory | `files` |
| `--db` | SQLite cache path | `cache.db` |
| `--verbose` | Debug logging | `False` |
| `--json-log` | JSON logs on stderr and a JSON summary on stdout | `False` |
| `--start-date` | Only media taken on/after `YYYY-MM-DD` | None |
| `--end-date` | Only media taken on/before `YYYY-MM-DD` | None |
| `--media-type` | `photos`, `videos`, or `all` | `all` |
| `--uploader` | Only this user id (repeatable) | None |
| `--limit` | Stop after N matching items (global across albums in sync) | None |
| `--concurrency` | Simultaneous downloads per album | `4` |
| `--album-concurrency` | Albums processed at once in sync mode | `1` |
| `--max-retries` | Attempts per file and page request | `4` |
| `--timeout` | Per-download timeout in seconds | `1200` |
| `--dry-run` | Report the plan, write nothing | `False` |
| `--incremental` | Stop paging once a page is fully archived | `False` |
| `--sync` | Headless single pass over saved albums | `False` |
| `--watch [SECONDS]` | Poll continuously | `300` when bare |
| `--repair` | Fix extensions left by older versions, in place | `False` |
| `--filename-template` | `{date} {time} {datetime} {iso} {uuid} {uuid8} {year} {month} {day} {type} {user} {device}` | `{date}_{time}-{uuid8}` |
| `--comment-format` | `md`, `json`, `both`, `none` | `md` |
| `--sidecar` | Write `<media>.json` beside each file | `False` |
| `--no-exif` | Skip EXIF (file times still set) | `False` |
| `--exiftool` | Use the `exiftool` binary (HEIC/PNG/MP4 too) | `False` |
| `--index` | Write `index.html` and `index.csv` | `False` |

Exit codes: `0` success · `1` some files failed · `2` album/URL error · `130` interrupted.

---

## ⚙️ Configuration

Copy `config.example.json` to `config.json` (ignored by Git), or point `--config` at another file:

```json
{
  "dest": "archive",
  "db": "cache.db",
  "media_type": "all",
  "comment_format": "both",
  "concurrency": 4,
  "timeout": 1800,
  "filename_template": "{date}_{time}-{uuid8}",
  "sidecar": true,
  "index": true,
  "incremental": false,
  "use_exiftool": false
}
```

CLI arguments always override the file. Prefer `--password-stdin` over storing a password in `config.json`.

---

## 🧪 Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
```

The suite is fully offline: HTTP is replaced with fake sessions, and every test writes only into a temporary directory.

---

## 🔐 Privacy & security

- 🔑 **Password handling** — `--password` is visible in the process list; prefer `--password-stdin`.
- 🙈 **No telemetry** — the tool talks only to the album host you give it.
- 🗃️ **Local metadata** — EXIF/GPS and sidecars are written locally; nothing is uploaded.
- 🚫 **No tokens in Git** — `config.json`, `cache.db`, archives, and `.part` files are ignored.
- 🛡️ **Secret-safe logs** — URLs are logged, passwords are not.
- 📜 See [`SECURITY.md`](SECURITY.md) for reporting vulnerabilities.

---

## 🧾 Version

**7.1.0** — live-verified authentication, full pagination, original downloads, JPEG EXIF, gallery links, and repeat-run skips. See [`CHANGELOG.md`](CHANGELOG.md) for the full history.

---

## ⚖️ License

MIT — see [`LICENSE`](LICENSE).
