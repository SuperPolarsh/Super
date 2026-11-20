import re

from editor import _drawtext_filters


def _caption_windows(words, fontsize=60):
    filters = _drawtext_filters(words, fontsize, 1920, 1080)
    windows = []
    for item in filters:
        match = re.search(r"between\(t,([\d.]+),([\d.]+)\)", item)
        assert match, item
        windows.append((float(match.group(1)), float(match.group(2))))
    return filters, windows


def test_overlapping_words_never_share_a_caption_window():
    words = [
        {"word": "one", "start": 0.0, "end": 0.6},
        {"word": "two", "start": 0.1, "end": 0.55},
        {"word": "three", "start": 0.1, "end": 0.12},
        {"word": "four", "start": 2.0, "end": 2.4},
    ]
    _, windows = _caption_windows(words)
    assert len(windows) == 4
    for (start, end), (next_start, _) in zip(windows, windows[1:]):
        assert start < end
        assert end <= next_start - 0.0019


def test_invalid_and_zero_length_words_are_dropped():
    words = [
        {"word": "ok", "start": 1.0, "end": 1.4},
        {"word": "zero", "start": 2.0, "end": 2.0},
        {"word": "backwards", "start": 3.0, "end": 2.9},
        {"word": "", "start": 4.0, "end": 4.4},
    ]
    _, windows = _caption_windows(words)
    assert len(windows) == 1


def test_long_transcripts_build_a_graph_bigger_than_windows_command_line():
    words = [{"word": f"w{index}", "start": index * 0.22, "end": index * 0.22 + 0.18}
             for index in range(409)]
    filters, _ = _caption_windows(words)
    graph = "[0:v]" + ",".join(filters) + "[captioned]"
    # Windows rejects command lines over ~32k chars with WinError 206; the
    # renderer must pass this graph through a script file instead.
    assert len(graph) > 32767
