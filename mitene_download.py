"""
This module provides functionality to download and manage media from album URLs.
It supports asynchronous downloads, database caching, and error handling.
"""

import asyncio
import json
import logging
import urllib.parse
import aiohttp
import aiosqlite
import argparse
import random
import re
import secrets
from pathlib import Path
from typing import Awaitable, Optional, Tuple, List, Callable, Dict, Any, Union
from tqdm import tqdm
import aiofiles
import aiofiles.os
from bs4 import BeautifulSoup
import piexif

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class DatabaseCache:
    def __init__(self, db_path: str = "cache.db") -> None:
        self.db_path = db_path

    async def init_db(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """CREATE TABLE IF NOT EXISTS cache
                   (url TEXT PRIMARY KEY,
                    cache_filename TEXT,
                    status TEXT,
                    downloaded_size INTEGER,
                    media_type TEXT,
                    creation_date TEXT,
                    last_modified TEXT,
                    file_size INTEGER,
                    checksum TEXT,
                    download_count INTEGER DEFAULT 0,
                    last_accessed TEXT,
                    error_log TEXT,
                    retry_count INTEGER DEFAULT 0,
                    re_download BOOLEAN DEFAULT FALSE)"""
            )
            await db.execute(
                """CREATE TABLE IF NOT EXISTS album_urls
                   (url TEXT PRIMARY KEY)"""
            )
            await db.commit()

    async def update_cache(self, url: str, cache_filename: str, status: str = "partial", downloaded_size: int = 0) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "REPLACE INTO cache (url, cache_filename, status, downloaded_size) VALUES (?, ?, ?, ?)",
                (url, cache_filename, status, downloaded_size),
            )
            await db.commit()

    async def save_album_url(self, url: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO album_urls (url) VALUES (?)", (url,))
            await db.commit()

    async def get_all_album_urls(self) -> List[str]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT url FROM album_urls") as cursor:
                rows = await cursor.fetchall()
                return [str(row[0]) for row in rows]

    async def delete_album_url(self, url: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM album_urls WHERE url = ?", (url,))
            await db.commit()
            logger.info(f"Deleted URL: {url}")

    async def get_cache_info(self, url: str) -> Tuple[Optional[str], Optional[str], int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT cache_filename, status, downloaded_size FROM cache WHERE url=?", (url,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return (str(row[0]), str(row[1]), int(row[2]))
                return (None, None, 0)


class TqdmUpTo(tqdm):
    def update_to(self, b: int = 1, bsize: int = 1, tsize: Optional[int] = None) -> None:
        if tsize is not None:
            self.total = tsize
        self.update(b * bsize - self.n)


class AsyncPageIterator:
    def __init__(self, session: aiohttp.ClientSession, album_url: str, password: Optional[str]) -> None:
        self.session = session
        self.album_url = album_url
        self.password = password
        self.page = 0
        self.last_page = False

    def __aiter__(self) -> 'AsyncPageIterator':
        return self

    async def __anext__(self) -> Dict[str, Any]:
        if self.last_page:
            raise StopAsyncIteration

        self.page += 1
        page_url = f"{self.album_url}?page={self.page}"
        logger.debug(f"Fetching page {self.page}...")
        response = await self.session.get(page_url)
        response_text = await response.text()
        
        soup = BeautifulSoup(response_text, "html.parser")
        
        if self.page == 1:
            if soup.find("input", {"id": "session_password"}):
                if not self.password:
                    logger.error("Password required but not provided.")
                    raise StopAsyncIteration
                    
                auth_token_input = soup.find("input", {"name": "authenticity_token"})
                if auth_token_input:
                    authenticity_token = str(auth_token_input.get("value", ""))
                    logger.info("Authenticating...")
                    auth_resp = await self.session.post(
                        f"{self.album_url}/login",
                        data={
                            "session[password]": self.password,
                            "authenticity_token": authenticity_token,
                        },
                    )
                    auth_resp.raise_for_status()
                    response = await self.session.get(page_url)
                    response_text = await response.text()
                    soup = BeautifulSoup(response_text, "html.parser")
                    
                    if soup.find("input", {"id": "session_password"}):
                        logger.error("Invalid password provided.")
                        raise StopAsyncIteration
            elif self.password:
                logger.warning("A password was provided, but this album does not require one. Proceeding...")

        script_content = None
        for script in soup.find_all("script"):
            if script.string and "window.gon=" in script.string:
                script_content = script.string
                break
        
        if not script_content:
            logger.error("Could not find media data script on the page.")
            self.last_page = True
            return {"mediaFiles": []}
            
        media_match = re.search(r"gon\.media=({.*?});gon\.", script_content)
        
        if not media_match:
            logger.error("Could not extract gon.media JSON from the page.")
            self.last_page = True
            return {"mediaFiles": []}
            
        try:
            data: Dict[str, Any] = json.loads(media_match.group(1))
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON data: {e}")
            self.last_page = True
            return {"mediaFiles": []}

        if not data.get("mediaFiles"):
            self.last_page = True

        return data


async def gather_with_concurrency(n: int, *tasks: Callable[[], Awaitable[None]]) -> None:
    semaphore = asyncio.Semaphore(n)

    async def sem_task(task_func: Callable[[], Awaitable[None]]) -> None:
        async with semaphore:
            await task_func()

    await asyncio.gather(*(sem_task(task) for task in tasks))


async def download_media(
    session: aiohttp.ClientSession,
    db_cache: DatabaseCache,
    url: str,
    destination_filename: Path,
    media_name: str,
    verbose: bool,
    current_index: int,
    total_media: int,
    took_at_raw: str = "",
    quiet: bool = False,
    max_retries: int = 4,
    buffer_size: int = 8192,
) -> None:
    cache_info = await db_cache.get_cache_info(url)
    downloaded_size = cache_info[2] if cache_info else 0

    if cache_info and cache_info[1] == "complete":
        return

    headers = {"Range": f"bytes={downloaded_size}-"} if downloaded_size > 0 else {}
    for attempt in range(max_retries):
        try:
            async with session.get(url, headers=headers, timeout=1200) as r:
                r.raise_for_status()

                mode = "ab" if downloaded_size > 0 else "wb"
                content_length = r.headers.get("content-length")
                total_size = (int(content_length) if content_length else 0) + downloaded_size
                
                with TqdmUpTo(
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    total=total_size,
                    initial=downloaded_size,
                    desc=f"Downloading {media_name}",
                    leave=False,
                    mininterval=0.1,
                    ncols=100,
                    disable=quiet
                ) as pbar:
                    async with aiofiles.open(destination_filename, mode) as f:
                        bytes_accumulated = 0
                        update_interval = 1024 * 1024
                        while True:
                            chunk = await r.content.read(buffer_size)
                            if not chunk:
                                break
                            await f.write(chunk)
                            bytes_accumulated += len(chunk)
                            if bytes_accumulated >= update_interval:
                                pbar.update(bytes_accumulated)
                                downloaded_size += bytes_accumulated
                                await db_cache.update_cache(
                                    url,
                                    str(destination_filename),
                                    "partial",
                                    downloaded_size,
                                )
                                bytes_accumulated = 0

                        if bytes_accumulated > 0:
                            pbar.update(bytes_accumulated)
                            downloaded_size += bytes_accumulated
                            await db_cache.update_cache(
                                url,
                                str(destination_filename),
                                "partial",
                                downloaded_size,
                            )

            await db_cache.update_cache(url, str(destination_filename), "complete", downloaded_size)
            
            # Inject EXIF metadata
            if destination_filename.suffix.lower() in [".jpg", ".jpeg"] and took_at_raw:
                match = re.match(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})", took_at_raw)
                if match:
                    exif_datetime = f"{match.group(1)}:{match.group(2)}:{match.group(3)} {match.group(4)}:{match.group(5)}:{match.group(6)}"
                    try:
                        try:
                            exif_dict = piexif.load(str(destination_filename))
                        except Exception:
                            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
                        
                        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_datetime.encode("utf-8")
                        exif_bytes = piexif.dump(exif_dict)
                        piexif.insert(exif_bytes, str(destination_filename))
                    except Exception as e:
                        if verbose:
                            logger.debug(f"Failed to inject EXIF for {destination_filename}: {e}")

            break
        except (aiohttp.ClientPayloadError, aiohttp.ClientError) as e:
            logger.warning(f"Error occurred during download of {media_name}: {e}")
            if attempt < max_retries - 1:
                retry_wait = 2**attempt + secrets.SystemRandom().uniform(0, 1)
                logger.info(
                    f"Retrying download of {media_name} in {retry_wait:.2f} seconds (Attempt {attempt + 2}/{max_retries})..."
                )
                await asyncio.sleep(retry_wait)
            else:
                logger.error(f"Max retries reached for {media_name}. Download failed.")
                break


async def process_album(
    album_url: str, password: Optional[str], destination_directory: str, verbose: bool, args: argparse.Namespace
) -> None:
    if verbose:
        logger.setLevel(logging.DEBUG)
        
    db_cache = DatabaseCache()
    await db_cache.init_db()

    destination_directory_path = Path(destination_directory)
    destination_directory_path.mkdir(parents=True, exist_ok=True)

    conn = aiohttp.TCPConnector(limit_per_host=4)
    async with aiohttp.ClientSession(connector=conn) as session:
        page_iterator = AsyncPageIterator(session, album_url, password)
        download_coroutines: List[Callable[[], Awaitable[None]]] = []
        comment_counter = 0
        
        logger.info(f"Starting to process album: {album_url}")
        
        async for data in page_iterator:
            media_files = data.get("mediaFiles", [])
            for index, media in enumerate(media_files):
                took_at_raw = str(media.get("tookAt", ""))
                
                # Filter by date
                date_str = took_at_raw.split(" ")[0] if took_at_raw else ""
                if args.start_date and date_str < args.start_date:
                    continue
                if args.end_date and date_str > args.end_date:
                    continue
                
                # Filter by media type
                is_video = bool(media.get("expiringVideoUrl"))
                if args.media_type == "photos" and is_video:
                    continue
                if args.media_type == "videos" and not is_video:
                    continue

                media_url = str(media.get("expiringVideoUrl") or media.get("expiringUrl", ""))
                parsed_url = urllib.parse.urlparse(media_url)
                filename = parsed_url.path.split("/")[-1]
                
                took_at = took_at_raw.replace(":", "_")
                filename_formatted = f'{took_at}-{filename}'

                # Smart folder organization (YYYY/MM)
                if took_at_raw and len(took_at_raw) >= 7:
                    year_month = f"{took_at_raw[0:4]}/{took_at_raw[5:7]}"
                else:
                    year_month = "unknown"

                if is_video:
                    media_directory = destination_directory_path / "videos" / year_month
                    media_directory.mkdir(parents=True, exist_ok=True)
                    destination_filename = media_directory / filename_formatted
                    if not destination_filename.suffix:
                        destination_filename = destination_filename.with_suffix(".mp4")
                else:
                    media_directory = destination_directory_path / "photos" / year_month
                    media_directory.mkdir(parents=True, exist_ok=True)
                    destination_filename = media_directory / filename_formatted

                uuid = str(media.get("uuid", ""))
                dl_url = f"{album_url}/media_files/{uuid}/download"
                cache_info = await db_cache.get_cache_info(dl_url)
                
                if not (cache_info and cache_info[1] == "complete"):
                    download_coroutines.append(
                        lambda s=session, dc=db_cache, du=dl_url, df=destination_filename, mu=uuid, v=verbose, idx=index + 1, total=len(media_files), tar=took_at_raw, q=bool(args.sync): download_media(
                            s, dc, du, df, mu, v, idx, total, tar, q
                        )
                    )

                if media.get("comments"):
                    comments_directory = destination_directory_path / "comments" / year_month
                    comments_directory.mkdir(parents=True, exist_ok=True)
                    
                    if args.comment_format in ("md", "both"):
                        comment_filename = comments_directory / (Path(filename_formatted).stem + ".md")
                        try:
                            if not await aiofiles.os.path.exists(comment_filename):
                                async with aiofiles.open(comment_filename, "w", encoding="utf-8") as comment_f:
                                    for comment in media["comments"]:
                                        if not comment.get("isDeleted"):
                                            user_nickname = comment.get("user", {}).get("nickname", "Unknown")
                                            body = comment.get("body", "")
                                            await comment_f.write(f'{comment_counter + 1}. **{user_nickname}**: {body}\n\n')
                                            comment_counter += 1
                                            if not args.sync:
                                                print(f"Comments being saved: {comment_counter}", end="\r")
                        except Exception as e:
                            logger.error(f"Error writing comments for {uuid}: {e}")

                    if args.comment_format in ("json", "both"):
                        comment_json_filename = comments_directory / (Path(filename_formatted).stem + ".json")
                        try:
                            if not await aiofiles.os.path.exists(comment_json_filename):
                                async with aiofiles.open(comment_json_filename, "w", encoding="utf-8") as comment_f:
                                    await comment_f.write(json.dumps(media["comments"], ensure_ascii=False, indent=2))
                        except Exception as e:
                            logger.error(f"Error writing JSON comments for {uuid}: {e}")

        if not download_coroutines:
            logger.info("No new downloads found. All files are up to date.")
        else:
            logger.info(f"Found {len(download_coroutines)} new files to download. Starting downloads...")
            await gather_with_concurrency(4, *download_coroutines)
            logger.info("All downloads completed successfully.")


async def interactive_mode(args: argparse.Namespace) -> None:
    ascii_art = r"""
 __  __ _ _                    _____                      _                 _           
|  \/  (_) |                  |  __ \                    | |               | |          
| \  / |_| |_ ___ _ __   ___  | |  | | _____      ___ __ | | ___   __ _  __| | ___ _ __ 
| |\/| | | __/ _ \ '_ \ / _ \ | |  | |/ _ \ \ /\ / / '_ \| |/ _ \ / _` |/ _` |/ _ \ '__|
| |  | | | ||  __/ | | |  __/ | |__| | (_) \ V  V /| | | | | (_) | (_| | (_| |  __/ |   
|_|  |_|_|\__\___|_| |_|\___| |_____/ \___/ \_/\_/ |_| |_|_|\___/ \__,_|\__,_|\___|_| v6.0
"""
    print(ascii_art)
    db_cache = DatabaseCache()
    await db_cache.init_db()

    while True:
        album_urls = await db_cache.get_all_album_urls()
        if album_urls:
            print("\nSelect an option from the list:")
            for index, url in enumerate(album_urls, start=1):
                print(f"{index}: {url}")
        else:
            print("\nNo album URLs found.")

        # Displaying consolidated choices for actions
        choice = input("Your choice (or 'a' to add, 'd' to delete, 'x' to exit): ").strip().lower()

        if choice == "a":
            album_url = input("Enter the new album URL: ").strip()
            if album_url:
                await db_cache.save_album_url(album_url)
        elif choice == "d":
            if album_urls:
                delete_choice = input("Enter the number of the URL to delete: ").strip()
                try:
                    delete_index = int(delete_choice)
                    if 1 <= delete_index <= len(album_urls):
                        await db_cache.delete_album_url(album_urls[delete_index - 1])
                    else:
                        print("Invalid selection. Please try again.")
                except ValueError:
                    print("Invalid number entered. Please enter a valid number.")
            else:
                print("No URLs to delete.")
        elif choice == "x":
            print("You have exited the script.")
            break
        elif choice.isdigit() and 1 <= int(choice) <= len(album_urls):
            album_url = album_urls[int(choice) - 1]
            if input("Does the album have a password? (y/n): ").strip().lower() == "y":
                password = input("Enter the password: ")
            else:
                password = None

            destination_directory = str(args.dest)
            verbose_input = input("Enable verbose logging (y/n): ").strip().lower()
            verbose = True if verbose_input == "y" else bool(args.verbose)

            print("Starting the script. Please wait...")
            await process_album(album_url, password, destination_directory, verbose, args)
        else:
            print("Invalid selection. Please try again.")


async def sync_mode(args: argparse.Namespace) -> None:
    db_cache = DatabaseCache()
    await db_cache.init_db()
    
    urls = [str(args.url)] if args.url else await db_cache.get_all_album_urls()
    if not urls:
        logger.warning("No URLs provided or found in database for sync.")
        return
        
    for url in urls:
        logger.info(f"Syncing album: {url}")
        await process_album(url, args.password, str(args.dest), bool(args.verbose), args)


def main() -> None:
    config_path = Path("config.json")
    config: Dict[str, Any] = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read config.json: {e}")

    parser = argparse.ArgumentParser(description="Mitene Family Album Downloader")
    parser.add_argument("--url", help="The album URL to download from", default=config.get("url"))
    parser.add_argument("--password", help="Password for the album (if required)", default=config.get("password"))
    parser.add_argument("--dest", help="Destination directory", default=config.get("dest", "files"))
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging", default=config.get("verbose", False))
    
    # New arguments
    parser.add_argument("--start-date", help="Start date for filtering (YYYY-MM-DD)", default=config.get("start_date"))
    parser.add_argument("--end-date", help="End date for filtering (YYYY-MM-DD)", default=config.get("end_date"))
    parser.add_argument("--media-type", choices=["photos", "videos", "all"], help="Type of media to download", default=config.get("media_type", "all"))
    parser.add_argument("--sync", action="store_true", help="Run in continuous sync mode (bypass prompts)", default=config.get("sync", False))
    parser.add_argument("--comment-format", choices=["md", "json", "both"], help="Format for saving comments", default=config.get("comment_format", "md"))
    
    args = parser.parse_args()

    try:
        if args.sync:
            asyncio.run(sync_mode(args))
        elif args.url:
            asyncio.run(process_album(str(args.url), args.password, str(args.dest), bool(args.verbose), args))
        else:
            asyncio.run(interactive_mode(args))
    except (KeyboardInterrupt, EOFError):
        logger.info("Script has terminated via user interrupt.")
    except Exception as e:
        logger.exception(f"Fatal error occurred: {e}")


if __name__ == "__main__":
    main()
