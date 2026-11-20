from app import (PROGRESS_RENDER_END, PROGRESS_RENDER_START, job_progress)


def test_first_clip_starts_at_the_render_marker():
    percent, base, span = job_progress("Rendering 1/4 - score 82, trust HIGH, 3.0s-33.0s (30s)")
    assert percent == PROGRESS_RENDER_START
    assert base == PROGRESS_RENDER_START
    assert span == (PROGRESS_RENDER_END - PROGRESS_RENDER_START) / 4


def test_each_clip_owns_one_quarter_of_the_render_range():
    percent, base, span = job_progress("Rendering 2/4 - score 82")
    assert percent == base == PROGRESS_RENDER_START + span
    _, base3, _ = job_progress("Rendering 3/4 - score 82")
    assert base > base3 - 100


def test_reframing_fills_the_first_half_of_a_clip():
    percent, base, span = job_progress("Rendering reframed clip: 40%", 58.5, 13.5)
    assert percent == 58.5 + 13.5 * 0.2
    assert (base, span) == (58.5, 13.5)


def test_captions_fill_the_second_half_of_a_clip():
    percent, _, _ = job_progress("Adding captions: 40%", 58.5, 13.5)
    assert percent == 58.5 + 13.5 * 0.7


def test_caption_and_reframe_percentages_stay_inside_the_clip():
    low, _, _ = job_progress("Rendering reframed clip: 0%", 58.5, 13.5)
    high, _, _ = job_progress("Adding captions: 100%", 58.5, 13.5)
    assert low == 58.5
    assert high == 72.0


def test_saved_clip_completes_its_slice():
    percent, _, _ = job_progress("Saved clip_01_score_88.mp4 (1080x1920)", 58.5, 13.5)
    assert percent == 72.0


def test_download_percentage_drives_the_first_tenth():
    low, _, _ = job_progress("Downloading source video:   0.0%")
    high, _, _ = job_progress("Downloading source video: 100.0%")
    assert low == 4.0
    assert high == 10.0


def test_pipeline_checkpoints_advance_in_order():
    stages = [
        "Fetching the public video link...",
        "Starting analysis: input.mp4",
        "Transcribing...",
        "Heuristics shortlisted 6 candidate moment(s) from 40 window(s).",
        "AI is auditing the candidates (score breakdown, trust, ideal length)...",
        "AI audit returned 6 evaluation(s).",
        "Found 3 candidate clip(s). Rendering 9:16 clips now...",
        "Trust report saved to out/report.json",
        "Done - created 2 clip(s) in out.",
    ]
    values = [job_progress(stage)[0] for stage in stages]
    assert None not in values
    assert values == sorted(values)
    assert values[-1] == 100.0


def test_unrelated_lines_leave_progress_alone():
    percent, base, span = job_progress("Preview ready.", 58.5, 13.5)
    assert percent is None
    assert (base, span) == (58.5, 13.5)


def test_unknown_lines_never_move_the_bar():
    assert job_progress("Interface error: boom")[0] is None
