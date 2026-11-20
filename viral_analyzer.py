import hashlib
import json
import os
import re
import tempfile
import numpy as np
from moviepy.editor import VideoFileClip
from scipy.io import wavfile
from ai_evaluator import evaluate_clip_candidates
from transcriber import transcribe

MIN_CLIP_LENGTH = 15.0
MAX_CLIP_LENGTH = 90.0
MAX_CANDIDATES = 10     # how many windows the AI audits per pass
MIN_HEURISTIC = 25      # below this a window is not worth an AI audit
OVERLAP_LIMIT = 0.55    # final clips that overlap more than this are duplicates
HOOK_PREROLL = 1.5      # seconds of lead-in kept before the strongest sentence


def extract_audio(video_path):
    """Extract one mono 16 kHz WAV, reused for volume analysis and transcription.

    Returns (wav_path, duration). wav_path is None when the video has no audio.
    """
    print(f"Extracting audio from {video_path}...")
    fd, wav_path = tempfile.mkstemp(prefix="viral_clipper_", suffix=".wav")
    os.close(fd)
    try:
        with VideoFileClip(video_path) as video:
            duration = video.duration
            if video.audio is None:
                os.remove(wav_path)
                return None, duration
            video.audio.write_audiofile(
                wav_path, fps=16000, nbytes=2, codec="pcm_s16le",
                ffmpeg_params=["-ac", "1"], logger=None,
            )
        return wav_path, duration
    except Exception:
        if os.path.exists(wav_path):
            os.remove(wav_path)
        raise


def _volumes_from_wav(wav_path):
    """Return the peak amplitude for each one-second window of a WAV file."""
    samplerate, data = wavfile.read(wav_path)

    if len(data.shape) > 1:
        data = np.mean(data, axis=1)

    if data.dtype == np.int16:
        data = data.astype(np.float32) / 32768.0

    window_size = samplerate
    volumes = []
    for i in range(0, len(data), window_size):
        window = data[i : i + window_size]
        if len(window) > 0:
            volumes.append(float(np.max(np.abs(window))))

    return volumes


VIRAL_KEYWORDS = [
    "omg", "wow", "insane", "unbelievable", "no way",
    "look at", "crazy", "amazing", "epic", "holy",
    "laugh", "hahaha", "what the", "best ever",
    "wild", "nuts", "huge", "massive",
    "secret", "mistake", "never", "always", "truth",
    "shocking", "banned", "everyone", "nobody",
]

STATS_PATTERN = re.compile(r"\b\d+([.,]\d+)?%?\b")


def heuristic_window_score(text, start, end, volumes, avg_volume):
    """Fast local score (0-100) for a candidate window, before the AI audit."""
    score = 0
    lower = text.lower()

    keyword_hits = sum(1 for keyword in VIRAL_KEYWORDS if keyword in lower)
    score += min(keyword_hits * 15, 60)

    if "?" in text:
        score += 8  # questions pull viewers in

    if STATS_PATTERN.search(text):
        score += 10  # numbers and stats read as substance

    if volumes and avg_volume > 0:
        indices = [t for t in range(int(start), min(int(end) + 1, len(volumes)))]
        if indices:
            peak = max(volumes[i] for i in indices)
            if peak > avg_volume * 2:
                score += 30
            elif peak > avg_volume * 1.5:
                score += 15

    duration = end - start
    if 20 <= duration <= 60:
        score += 10  # native short-form length

    return min(score, 100)


def segment_interest(segment, volumes, avg_volume):
    """Per-segment interest used to find the strongest sentence in a window."""
    score = 0
    lower = segment["text"].lower()
    score += min(sum(1 for keyword in VIRAL_KEYWORDS if keyword in lower) * 15, 45)
    if volumes and avg_volume > 0:
        index = int(segment["start"])
        if index < len(volumes) and volumes[index] > avg_volume * 1.5:
            score += 25
    return score


def build_candidate_windows(segments, volumes, avg_volume):
    """Groups consecutive transcript segments into overlapping candidate windows."""
    candidates = []
    count = len(segments)
    for first in range(count):
        texts = []
        for last in range(first, count):
            segment = segments[last]
            texts.append(segment["text"].strip())
            start = float(segments[first]["start"])
            end = float(segment["end"])
            duration = end - start
            if duration < MIN_CLIP_LENGTH:
                continue  # too short to be a clip; keep extending
            text = " ".join(texts)
            candidates.append({
                "id": len(candidates),
                "start": start,
                "end": end,
                "duration": duration,
                "text": text,
                "first_segment": first,
                "last_segment": last,
                "heuristic": heuristic_window_score(text, start, end, volumes, avg_volume),
            })
            if duration >= MAX_CLIP_LENGTH:
                break
    return candidates


def dedupe_candidates(candidates, limit):
    """Keeps the highest-scoring windows that do not heavily overlap each other."""
    kept = []
    for candidate in sorted(candidates, key=lambda item: item["heuristic"], reverse=True):
        overlaps = any(
            max(0, min(candidate["end"], existing["end"]) - max(candidate["start"], existing["start"]))
            / max(1, min(candidate["duration"], existing["duration"])) > OVERLAP_LIMIT
            for existing in kept
        )
        if not overlaps:
            kept.append(candidate)
        if len(kept) >= limit:
            break
    return kept


def collect_words(segments):
    """Flattens word timestamps into one absolute-time list."""
    words = []
    for segment in segments:
        for word in segment.get("words", []):
            try:
                start = float(word.get("start"))
                end = float(word.get("end"))
            except (TypeError, ValueError):
                continue
            if end > start:
                words.append({"word": str(word.get("word", "")).strip(), "start": start, "end": end})
    return sorted(words, key=lambda item: item["start"])


def snap_to_pause(words, target, video_duration, max_shift=4.0):
    """Snaps a cut time to the nearest silence gap between spoken words."""
    cut_points = []
    for previous, following in zip(words, words[1:]):
        gap_start = float(previous["end"])
        gap_end = float(following["start"])
        if gap_end - gap_start >= 0.25:
            cut_points.append((gap_start + gap_end) / 2)
    if not cut_points:
        return max(0.0, min(target, video_duration))
    nearest = min(cut_points, key=lambda point: abs(point - target))
    if abs(nearest - target) <= max_shift:
        return max(0.0, min(nearest, video_duration))
    return max(0.0, min(target, video_duration))


CACHE_DIR = os.path.join(tempfile.gettempdir(), "viral_clipper_cache")


def _transcript_cache_path(video_path):
    """Cache key: same file (path + size + mtime) reuses its transcription."""
    stat = os.stat(video_path)
    raw = f"{os.path.abspath(video_path)}|{stat.st_size}|{int(stat.st_mtime)}"
    return os.path.join(CACHE_DIR, f"{hashlib.sha1(raw.encode('utf-8')).hexdigest()}.json")


def _load_cached_transcript(video_path):
    try:
        cache_path = _transcript_cache_path(video_path)
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and data.get("segments"):
                return data
    except (OSError, ValueError):
        pass
    return None


def _store_cached_transcript(video_path, result):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_transcript_cache_path(video_path), "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False)
    except (OSError, ValueError):
        pass  # caching is best-effort only


def analyze_virality(video_path, fallback_duration=30, status_callback=None):
    """Analyzes a video and returns AI-audited moments with trust breakdowns.

    Each moment carries: start, end, score (0-100), hook/retention/payoff
    subscores, confidence ("high"/"medium"/"low"), a written reason, the
    suggested length the AI chose, transcript text, and relative word timings.
    """
    def status(message):
        print(message)
        if status_callback:
            status_callback(message)

    fallback_duration = max(MIN_CLIP_LENGTH, min(float(fallback_duration), MAX_CLIP_LENGTH))

    # 1. Decode the audio once; the same WAV feeds volume analysis and transcript.
    audio_path, video_duration = extract_audio(video_path)
    if audio_path is None:
        raise ValueError("This video has no audio track to transcribe.")

    try:
        try:
            volumes = _volumes_from_wav(audio_path)
            avg_volume = np.mean(volumes) if volumes else 0
        except Exception as e:
            print(f"Warning: Could not analyze volume levels: {e}")
            volumes = []
            avg_volume = 0

        # A cached transcript makes re-runs on the same file almost instant.
        transcript = _load_cached_transcript(video_path)
        if transcript:
            status("Using cached transcription...")
        else:
            status("Transcribing...")
            transcript = transcribe(audio_path, status_callback=status_callback)
            if transcript and transcript.get("segments"):
                _store_cached_transcript(video_path, transcript)
        segments = transcript["segments"]
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)

    if not segments:
        status("No speech detected in this video.")
        return []

    # 2. Build candidate windows and shortlist them heuristically.
    candidates = build_candidate_windows(segments, volumes, avg_volume)
    strong = [c for c in candidates if c["heuristic"] >= MIN_HEURISTIC]
    shortlist = dedupe_candidates(strong or candidates, MAX_CANDIDATES)
    status(f"Heuristics shortlisted {len(shortlist)} candidate moment(s) from {len(candidates)} window(s).")

    # 3. AI audit: one batched call returns the full trust breakdown.
    status("AI is auditing the candidates (score breakdown, trust, ideal length)...")
    evaluations = evaluate_clip_candidates(shortlist, status_callback=status_callback)
    if evaluations:
        status(f"AI audit returned {len(evaluations)} evaluation(s).")
    else:
        status("AI audit unavailable - falling back to heuristic scores only.")

    words = collect_words(segments)

    # 4. Merge heuristic + AI verdicts, then cut adaptive windows.
    moments = []
    for candidate in sorted(shortlist, key=lambda item: item["heuristic"], reverse=True):
        evaluation = evaluations.get(candidate["id"])
        if evaluation:
            score = int(round(0.3 * candidate["heuristic"] + 0.7 * evaluation["score"]))
            confidence = evaluation["confidence"]
            reason = evaluation["reason"]
        else:
            score = int(round(candidate["heuristic"] * 0.85))
            confidence = "low"
            reason = "Scored by audio/keyword heuristics only; the AI audit was unavailable."

        # The length chosen in the Auto Clipper settings is authoritative.
        # The AI's suggested_length is ignored so clips match the requested seconds.
        length = fallback_duration

        length = max(MIN_CLIP_LENGTH, min(float(length), MAX_CLIP_LENGTH, video_duration))

        # Anchor the clip at the strongest sentence inside the window.
        window_segments = segments[candidate["first_segment"]: candidate["last_segment"] + 1]
        anchor_segment = max(window_segments, key=lambda seg: segment_interest(seg, volumes, avg_volume))

        target = min(length, video_duration)
        if target < 1:
            continue

        # Snap the start to a natural pause, then force the window to the
        # exact requested length so clips match the settings precisely.
        start = max(0.0, float(anchor_segment["start"]) - HOOK_PREROLL)
        start = snap_to_pause(words, start, video_duration)
        start = max(0.0, min(start, video_duration - target))

        end = min(start + target, video_duration)
        # If we hit the end of the video, shift the start back to keep the length.
        if end - start < target:
            start = max(0.0, video_duration - target)
            end = min(start + target, video_duration)

        if end - start < 5:
            continue

        # Avoid generating several nearly identical clips.
        overlaps_existing = any(
            max(0, min(end, existing["end"]) - max(start, existing["start"]))
            / max(1, min(end - start, existing["end"] - existing["start"])) > OVERLAP_LIMIT
            for existing in moments
        )
        if overlaps_existing:
            continue

        matching_segments = [segment for segment in segments if segment["end"] > start and segment["start"] < end]
        clip_words = []
        for segment in matching_segments:
            for word in segment.get("words", []):
                word_start, word_end = word.get("start", start), word.get("end", end)
                if word_end > start and word_start < end:
                    clip_words.append({
                        "word": word.get("word", "").strip(),
                        "start": max(0, word_start - start),
                        "end": max(0, word_end - start),
                    })

        moments.append({
            "start": start,
            "end": end,
            "score": min(max(score, 0), 100),
            "heuristic_score": candidate["heuristic"],
            "hook": evaluation["hook"] if evaluation else min(score, 100),
            "retention": evaluation["retention"] if evaluation else min(score, 100),
            "payoff": evaluation["payoff"] if evaluation else min(score, 100),
            "confidence": confidence,
            "reason": reason,
            "suggested_length": round(end - start),
            "text": " ".join(segment["text"].strip() for segment in matching_segments),
            "words": clip_words,
        })

    status(f"Selected {len(moments)} final moment(s).")
    return sorted(moments, key=lambda item: item["start"])
