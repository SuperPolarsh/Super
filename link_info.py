"""Reads the title and thumbnail of a public video link.

Kept separate from ``app`` so it can be unit-tested without building the Tk
window or importing the video pipeline.
"""
from __future__ import annotations

import re

TITLE_LIMIT = 110


class _QuietLogger:
    """Swallows yt-dlp output; a windowed exe has no usable stdout/stderr."""

    def debug(self, message):
        pass

    def info(self, message):
        pass

    def warning(self, message):
        pass

    def error(self, message):
        pass


def looks_like_link(text: str) -> bool:
    """True for a public http(s) link, ignoring file paths and placeholders."""
    value = (text or "").strip()
    if not value.lower().startswith(("https://", "http://")):
        return False
    # "https://" on its own is still being typed.
    return bool(value.split("://", 1)[1].strip("/"))


def format_link_title(title: str, limit: int = TITLE_LIMIT) -> str:
    """Builds the banner line shown once a link has been read."""
    text = " ".join(str(title or "").split())
    if not text:
        return "Video link ready"
    if limit > 1 and len(text) > limit:
        text = text[: limit - 1].rstrip(" ,.;:-–—") + "…"
    return f"Video ready: {text}"


def short_error(message, limit: int = 70) -> str:
    """Turns a yt-dlp exception into one readable sentence for the banner."""
    text = " ".join(str(message or "").split())
    text = re.sub(r"^(?:ERROR:\s*)+", "", text, flags=re.IGNORECASE)
    # "ERROR: [youtube] dQw4w9WgXcQ: Video unavailable" -> "Video unavailable"
    text = re.sub(r"^\[[^\]]{1,40}\]\s*(?:[^:\s]{1,40}:\s*)?", "", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip(" ,.;:-") + "…"
    return text


def extract_link_info(url: str, timeout: float = 15) -> dict:
    """Returns title/thumbnail/duration/uploader for a public video link.

    Only metadata is fetched (``download=False``), so this is cheap even for
    long videos. Raises on links that are private, removed or not videos.
    """
    import yt_dlp

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": timeout,
        "cachedir": False,
        "logger": _QuietLogger(),
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False) or {}

    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        info = entries[0] if entries else {}

    title = str(info.get("title") or "").strip()
    if not title:
        raise ValueError("This link doesn't have a readable title.")

    return {
        "title": title,
        "thumbnail": str(info.get("thumbnail") or "").strip(),
        "duration": info.get("duration") or 0,
        "uploader": str(info.get("uploader") or info.get("channel") or "").strip(),
    }
