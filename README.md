# Mitene Family Album Downloader (v6.0)

A powerful, asynchronous Python tool to download and archive media from Mitene (FamilyAlbum) URLs. This tool handles large albums efficiently, organizes media by date, injects EXIF metadata, and supports automated sync modes.

## ✨ Features (v6.0 Updates)

*   **⚡ Asynchronous Downloading:** Downloads multiple files concurrently for maximum speed.
*   **📂 Smart Organization:** Automatically saves files into `YYYY/MM` subfolders based on when they were taken.
*   **📸 EXIF Metadata Injection:** Uses `piexif` to write the original "Date Taken" timestamp into the JPEG headers of downloaded photos.
*   **🧠 Database Caching:** Uses `aiosqlite` to track downloads, allowing you to resume interrupted downloads and skip files that are already up-to-date.
*   **🔄 Sync / Watch Mode:** A headless `--sync` mode designed for automated tasks (Cron/Task Scheduler) that bypasses interactive prompts.
*   **🗓️ Advanced Filtering:** Filter your downloads by `--start-date`, `--end-date`, or `--media-type` (photos, videos, or all).
*   **💬 Rich Comment Exporting:** Save family comments as either Markdown files (`.md`), raw structured data (`.json`), or both.
*   **⚙️ Configuration Support:** Supports a `config.json` file to save your default settings, destination paths, and preferences.
*   **🛡️ Robust Parsing:** Uses `BeautifulSoup4` for reliable HTML parsing and session handling.

## 🚀 Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/suasive93/mitene-familyalbum-downloader-main.git
    cd mitene-familyalbum-downloader-main
    ```

2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

## 📖 Usage

### Interactive Mode
Simply run the script to enter the interactive menu:
```bash
python mitene_download.py
```
From here you can add album URLs, manage your saved albums, and start downloads.

### Command Line Mode
Run the script with arguments to bypass the menu:
```bash
python mitene_download.py --url <URL> --dest ./my_backup --media-type photos
```

### Automation / Sync Mode
To run the script as a background task for all your saved albums:
```bash
python mitene_download.py --sync
```

## 🛠️ Configuration (`config.json`)

You can create a `config.json` in the project root to store your defaults:

```json
{
  "dest": "files",
  "verbose": false,
  "media_type": "all",
  "comment_format": "both",
  "sync": false,
  "start_date": "2020-01-01"
}
```

## 📝 CLI Arguments Reference

| Argument | Description | Default |
| :--- | :--- | :--- |
| `--url` | The album URL to download from | None |
| `--password` | Password for the album (if required) | None |
| `--dest` | Destination directory | `files` |
| `--verbose` | Enable detailed logging | `False` |
| `--sync` | Run in headless sync mode | `False` |
| `--start-date` | Filter media taken AFTER (YYYY-MM-DD) | None |
| `--end-date` | Filter media taken BEFORE (YYYY-MM-DD) | None |
| `--media-type` | `photos`, `videos`, or `all` | `all` |
| `--comment-format` | `md`, `json`, or `both` | `md` |

## ⚖️ License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
