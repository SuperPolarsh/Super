"""Download a public video URL to a temporary local file for processing."""
import os
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError

try:
    from moviepy.config import get_setting
except ImportError:
    get_setting = None

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_COOKIE_FILE = os.path.join(PROJECT_DIR, "cookies.txt")

# YouTube's "sign in to confirm you're not a bot" wall is satisfied by real
# browser cookies, so these markers trigger the cookie retry path.
BOT_CHECK_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "bot check",
    "cookies",
)


def _is_bot_check(message):
    message = message.lower()
    return any(marker in message for marker in BOT_CHECK_MARKERS)


def _cookie_variants():
    """Cookie strategies to try, in order, when YouTube asks for a sign-in.

    * YTDLP_COOKIE_FILE / cookies.txt in the project folder (Netscape export)
    * YTDLP_COOKIES_BROWSER (edge, chrome, firefox, ...) to force a browser
    * Otherwise try common browsers in turn.
    """
    variants = []
    cookie_file = os.environ.get("YTDLP_COOKIE_FILE", DEFAULT_COOKIE_FILE)
    if os.path.isfile(cookie_file):
        variants.append((f"the cookie file {os.path.basename(cookie_file)}", {"cookiefile": cookie_file}))

    forced = os.environ.get("YTDLP_COOKIES_BROWSER", "").strip().lower()
    browsers = [forced] if forced else ["edge", "chrome", "firefox"]
    for browser in browsers:
        variants.append((f"your {browser.title()} browser", {"cookiesfrombrowser": (browser,)}))
    return variants


def download_public_video(url, destination, status_callback=None):
    """Download one public video and return its local filename."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Paste a complete public video link beginning with https://")

    def status(message):
        if status_callback:
            status_callback(message)

    last_percent = {"value": None}

    def progress_hook(update):
        if update.get("status") != "downloading":
            return
        percent = update.get("_percent_str", "").strip()
        if percent and percent != last_percent["value"]:
            last_percent["value"] = percent
            status(f"Downloading source video: {percent}")

    status("Fetching the public video link...")
    base_options = {
        "outtmpl": os.path.join(destination, "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
        "merge_output_format": "mp4",
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        # SPEED OPTIMIZATIONS
        "concurrent_fragment_downloads": 10, # Download multiple parts at once
        "n_threads": 10,                      # Use more threads for the download
        "buffersize": 1024 * 1024,           # Increase buffer to 1MB
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", # Spoof real browser
    }
    if get_setting:
        base_options["ffmpeg_location"] = get_setting("FFMPEG_BINARY")

    # Ordered fallbacks. The old chain ended with "b" (best *combined* format),
    # which YouTube rarely offers, so a transient failure on the first try was
    # reported as "Requested format is not available". Cap at 1080p: the app
    # only outputs 1080-wide clips, and 4K downloads are needlessly slow.
    format_choices = (
        "bv*[height<=1080][vcodec^=avc1]+ba[acodec^=mp4a]/b[height<=1080][ext=mp4]",
        "bv*[height<=1080]+ba/b[height<=1080]",
        "bv*+ba/b",
        None,  # let yt-dlp choose whatever it can play
    )
    fatal_markers = (
        "private video",
        "sign in to confirm",
        "age-restricted",
        "age restricted",
        "not available in your country",
        "video unavailable",
        "has been removed",
        "members-only",
    )

    def attempt(options):
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.extract_info(url, download=True)
    first_error = None
    bot_blocked = None  # holds the bot-check error once YouTube demands sign-in

    for index, format_choice in enumerate(format_choices):
        options = {**base_options, "format": format_choice}
        try:
            attempt(options)
            break
        except DownloadError as error:
            if first_error is None:
                first_error = error
            message = str(error).lower()
            if _is_bot_check(message):
                # Do not give up: retry below with browser cookies.
                bot_blocked = error
                break
            if any(marker in message for marker in fatal_markers):
                raise
            if index == len(format_choices) - 1:
                # Report the original failure, not the last format probe.
                raise first_error
            status("Trying a compatible video format...")

    # YouTube demanded a sign-in: retry with strategies that usually pass.
    if bot_blocked is not None:
        bot_error = bot_blocked
        resolved = False

        # The android player is served differently and often skips the bot wall
        # without any account cookies at all.
        strategies = [
            ("the android player", {"extractor_args": {"youtube": {"player_client": ["android"]}}}),
        ]
        for label, cookie_options in _cookie_variants():
            strategies.append((f"cookies from {label}", cookie_options))

        for label, extra_options in strategies:
            status(f"YouTube asked for a sign-in; retrying with {label}...")
            try:
                attempt({**base_options, "format": format_choices[0], **extra_options})
                resolved = True
                break
            except DownloadError as error:
                message = str(error).lower()
                if _is_bot_check(message):
                    continue  # this strategy did not satisfy YouTube
                if any(marker in message for marker in fatal_markers):
                    raise
                # A non-bot failure (e.g. format issue): retry once with a
                # forgiving format before moving to the next strategy.
                try:
                    attempt({**base_options, "format": None, **extra_options})
                    resolved = True
                    break
                except DownloadError as inner:
                    if _is_bot_check(str(inner).lower()):
                        continue
                    raise
            except Exception as exc:  # cookie extraction failed (locked/absent browser)
                status(f"Could not read cookies from {label}: {exc}")
                continue
        if not resolved:
            raise RuntimeError(
                "YouTube is blocking this download with a bot check, and no fallback "
                "satisfied it. Fix it in one of these ways:\n"
                "1) Open YouTube in Edge/Chrome/Firefox, sign in, and retry - the app "
                "reads those cookies automatically.\n"
                "2) Export cookies with a browser extension into cookies.txt in the "
                "app folder (Netscape format), then retry.\n"
                "3) Update yt-dlp: pip install -U yt-dlp\n"
                f"Original error: {bot_error}"
            )

    media_extensions = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".m4a", ".mp3"}
    files = [
        os.path.join(destination, name)
        for name in os.listdir(destination)
        if os.path.isfile(os.path.join(destination, name))
        and os.path.splitext(name)[1].lower() in media_extensions
        and not name.endswith(".part")
    ]
    if not files:
        raise RuntimeError("The video downloader did not produce a media file.")
    merged_mp4 = os.path.join(destination, "source.mp4")
    if os.path.isfile(merged_mp4):
        return merged_mp4
    return max(files, key=os.path.getsize)
