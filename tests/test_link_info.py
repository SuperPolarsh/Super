import sys
import types

import pytest

import link_info


def test_looks_like_link_accepts_public_urls():
    assert link_info.looks_like_link("https://www.youtube.com/watch?v=abc123")
    assert link_info.looks_like_link("  http://example.com/clip.mp4  ")


@pytest.mark.parametrize("text", ["", "   ", "https://", "https:///", "clip.mp4",
                                  r"C:\Videos\clip.mp4",
                                  "Local file: clip.mp4 (12.3 MB)"])
def test_looks_like_link_rejects_files_and_half_typed_urls(text):
    assert not link_info.looks_like_link(text)


def test_format_link_title_prefixes_and_collapses_whitespace():
    assert link_info.format_link_title("  Hurricane   Neo 3 \n VS Hurricane ") == \
        "Video ready: Hurricane Neo 3 VS Hurricane"


def test_format_link_title_falls_back_without_a_title():
    assert link_info.format_link_title("") == "Video link ready"


def test_format_link_title_truncates_long_titles():
    title = link_info.format_link_title("x" * 400, limit=40)
    assert len(title) <= len("Video ready: ") + 40
    assert title.endswith("…")


def test_short_error_strips_the_ytdlp_prefix():
    assert link_info.short_error(
        "ERROR: [youtube] dQw4w9WgXcQ: Video unavailable") == "Video unavailable"


def test_short_error_truncates_and_collapses_newlines():
    message = "ERROR: " + "y" * 300
    result = link_info.short_error(message, limit=60)
    assert len(result) == 60
    assert result.endswith("…")


def test_extract_link_info_reads_metadata_without_downloading(monkeypatch):
    calls = {}

    class FakeYDL:
        def __init__(self, options):
            calls["options"] = options

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            calls["url"] = url
            calls["download"] = download
            return {"title": "  A clip  ", "thumbnail": "https://img/1.jpg",
                    "duration": 42, "channel": "Someone"}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))

    info = link_info.extract_link_info("https://example.com/v/1")
    assert info == {"title": "A clip", "thumbnail": "https://img/1.jpg",
                    "duration": 42, "uploader": "Someone"}
    assert calls["download"] is False
    assert calls["options"]["noplaylist"] is True
    assert calls["options"]["quiet"] is True


def test_extract_link_info_unwraps_a_playlist_entry(monkeypatch):
    class FakeYDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            return {"_type": "playlist", "entries": [{"title": "First clip"}]}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    assert link_info.extract_link_info("https://example.com/list")["title"] == "First clip"


def test_extract_link_info_rejects_links_without_a_title(monkeypatch):
    class FakeYDL:
        def __init__(self, options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=True):
            return {}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    with pytest.raises(ValueError):
        link_info.extract_link_info("https://example.com/nothing")
