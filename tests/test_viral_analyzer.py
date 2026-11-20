import pytest

from viral_analyzer import (
    build_candidate_windows,
    collect_words,
    dedupe_candidates,
    heuristic_window_score,
    snap_to_pause,
)


def make_segment(start, end, text):
    return {"start": start, "end": end, "text": text, "words": []}


def test_keyword_rich_text_outscores_plain_talk():
    volumes = [0.2] * 60
    plain = heuristic_window_score("and then we went to the store again", 0, 30, volumes, 0.2)
    spicy = heuristic_window_score("this is insane, no way, the results were massive", 0, 30, volumes, 0.2)
    assert spicy > plain


def test_volume_spike_raises_the_score():
    quiet_volumes = [0.2] * 60
    loud_volumes = [0.2] * 5 + [0.9] + [0.2] * 54
    text = "a perfectly normal sentence about the weather today"
    quiet = heuristic_window_score(text, 0, 30, quiet_volumes, 0.2)
    loud = heuristic_window_score(text, 0, 30, loud_volumes, 0.2)
    assert loud > quiet


def test_score_is_capped_at_100():
    volumes = [0.9] * 60
    score = heuristic_window_score("insane crazy wow epic massive" * 10, 0, 45, volumes, 0.2)
    assert score == 100


def test_build_candidate_windows_only_keeps_clip_sized_spans():
    segments = [make_segment(i * 4.0, (i + 1) * 4.0, f"segment {i}") for i in range(6)]
    candidates = build_candidate_windows(segments, volumes=[], avg_volume=0)
    assert candidates
    assert all(c["duration"] >= 15.0 for c in candidates)
    assert len({c["id"] for c in candidates}) == len(candidates)


def test_dedupe_candidates_keeps_higher_scoring_window():
    first = {"id": 0, "start": 0.0, "end": 30.0, "duration": 30.0, "heuristic": 80, "text": ""}
    second = {"id": 1, "start": 5.0, "end": 35.0, "duration": 30.0, "heuristic": 50, "text": ""}
    third = {"id": 2, "start": 60.0, "end": 80.0, "duration": 20.0, "heuristic": 40, "text": ""}
    kept = dedupe_candidates([second, first, third], limit=10)
    assert [c["id"] for c in kept] == [0, 2]


def test_snap_to_pause_moves_cut_into_the_gap():
    words = [
        {"word": "one", "start": 0.0, "end": 1.0},
        {"word": "two", "start": 1.5, "end": 2.5},
        {"word": "three", "start": 3.0, "end": 4.0},
    ]
    assert snap_to_pause(words, 1.4, video_duration=20.0) == pytest.approx(1.25)


def test_snap_to_pause_leaves_far_targets_alone():
    words = [
        {"word": "one", "start": 0.0, "end": 1.0},
        {"word": "two", "start": 1.5, "end": 2.5},
    ]
    assert snap_to_pause(words, 15.0, video_duration=20.0) == 15.0


def test_collect_words_flattens_and_sorts():
    segments = [
        make_segment(0, 2, "b a"),
        make_segment(2, 4, "c"),
    ]
    segments[0]["words"] = [
        {"word": "b", "start": 1.0, "end": 1.5},
        {"word": "a", "start": 0.0, "end": 0.8},
    ]
    segments[1]["words"] = [{"word": "c", "start": 2.2, "end": 3.0}]
    words = collect_words(segments)
    assert [w["word"] for w in words] == ["a", "b", "c"]
