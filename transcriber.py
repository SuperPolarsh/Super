"""Speech-to-text for the clip pipeline.

Transcription is the slowest stage of clip generation, so this module prefers
Groq's hosted Whisper (usually a few seconds for a whole video, with word-level
timestamps) and only falls back to a local model when the cloud is unavailable.
"""
import glob
import os
import shutil
import subprocess
import tempfile

import whisper
from dotenv import load_dotenv

load_dotenv()

# Local fallback model. Override with WHISPER_LOCAL_MODEL (tiny/base/small/...).
LOCAL_MODEL_NAME = os.environ.get("WHISPER_LOCAL_MODEL", "base")
# Hosted model. whisper-large-v3-turbo is both the fastest and more accurate
# than the local "base" model it replaces.
GROQ_MODEL = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")

# Groq rejects uploads above 25 MB, so stay under it with a margin.
GROQ_MAX_BYTES = 24 * 1024 * 1024
# Mono 16 kHz speech at 32 kbps is ~4 KB/s, i.e. about 100 minutes per 24 MB.
GROQ_AUDIO_BITRATE = "32k"
GROQ_BYTES_PER_SECOND = 32000 / 8

# Loading a local Whisper model is expensive, so keep it alive between runs.
_local_models = {}


def _log(status_callback, message):
    print(message)
    if status_callback:
        status_callback(message)


def _ffmpeg_exe():
    """Locate an ffmpeg binary without requiring it on the PATH."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _field(obj, name, default=None):
    """Read a value from a dict or a pydantic-style object."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize(verbose, offset=0.0):
    """Convert a verbose Groq response into Whisper's result shape.

    The rest of the pipeline expects `{"segments": [{"start", "end", "text",
    "words": [...]}]}`, so each word is attached to the segment that contains it.
    """
    raw_segments = _field(verbose, "segments") or []
    raw_words = _field(verbose, "words") or []
    duration = _to_float(_field(verbose, "duration"), 0.0)

    segments = []
    for raw in raw_segments:
        segments.append({
            "start": _to_float(_field(raw, "start")) + offset,
            "end": _to_float(_field(raw, "end")) + offset,
            "text": str(_field(raw, "text", "") or ""),
            "words": [],
        })

    for raw in raw_words:
        word_start = _to_float(_field(raw, "start")) + offset
        word_end = _to_float(_field(raw, "end"), word_start - offset) + offset
        item = {"word": str(_field(raw, "word", "") or ""), "start": word_start, "end": word_end}

        target = next((seg for seg in segments if word_start < seg["end"] and word_end > seg["start"]), None)
        if target is None and segments:
            target = min(segments, key=lambda seg: abs(seg["start"] - word_start))
        if target is not None:
            target["words"].append(item)

    text = str(_field(verbose, "text", "") or "").strip()
    if not segments and text:
        # Safety net: keep a single segment so downstream logic still works.
        segments.append({"start": offset, "end": offset + duration, "text": text, "words": []})
    if not text:
        text = " ".join(seg["text"].strip() for seg in segments)

    return {"text": text, "segments": segments}


def _compress_for_groq(audio_path):
    """Re-encode any input to a small mono 16 kHz mp3 so uploads stay tiny."""
    fd, compressed = tempfile.mkstemp(prefix="viral_clipper_", suffix=".mp3")
    os.close(fd)
    subprocess.run(
        [_ffmpeg_exe(), "-y", "-i", audio_path, "-vn", "-ac", "1", "-ar", "16000",
         "-b:a", GROQ_AUDIO_BITRATE, compressed],
        check=True, capture_output=True,
    )
    return compressed


def _split_audio(audio_path, seconds_per_chunk):
    """Split a long mp3 into time-based chunks under the upload limit."""
    folder = tempfile.mkdtemp(prefix="groq_chunks_")
    pattern = os.path.join(folder, "chunk_%04d.mp3")
    subprocess.run(
        [_ffmpeg_exe(), "-y", "-i", audio_path, "-vn", "-ac", "1", "-ar", "16000",
         "-b:a", GROQ_AUDIO_BITRATE, "-f", "segment",
         "-segment_time", str(seconds_per_chunk), pattern],
        check=True, capture_output=True,
    )
    return folder, sorted(glob.glob(os.path.join(folder, "chunk_*.mp3")))


def _transcribe_groq(audio_path, status_callback, timeout=60):
    """Transcribe via Groq, chunking only if the audio is unusually long."""
    from groq import Groq

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None

    client = Groq(api_key=api_key)
    compressed = _compress_for_groq(audio_path)
    chunk_folder = None
    try:
        if os.path.getsize(compressed) <= GROQ_MAX_BYTES:
            chunks = [(compressed, 0.0)]
        else:
            seconds = max(60, int(GROQ_MAX_BYTES / GROQ_BYTES_PER_SECOND * 0.9))
            chunk_folder, chunk_paths = _split_audio(compressed, seconds)
            chunks = [(path, index * seconds) for index, path in enumerate(chunk_paths)]

        _log(status_callback, f"Transcribing with Groq ({GROQ_MODEL})...")
        merged_segments = []
        merged_text = []
        for path, offset in chunks:
            with open(path, "rb") as audio_file:
                response = client.audio.transcriptions.create(
                    model=GROQ_MODEL,
                    file=audio_file,
                    response_format="verbose_json",
                    timestamp_granularities=["word", "segment"],
                    timeout=timeout,
                )
            normalized = _normalize(response, offset=offset)
            merged_segments.extend(normalized["segments"])
            if normalized["text"]:
                merged_text.append(normalized["text"])

        return {"text": " ".join(merged_text).strip(), "segments": merged_segments}
    finally:
        if os.path.exists(compressed):
            os.remove(compressed)
        if chunk_folder:
            shutil.rmtree(chunk_folder, ignore_errors=True)


def _load_local_model():
    if LOCAL_MODEL_NAME not in _local_models:
        _local_models[LOCAL_MODEL_NAME] = whisper.load_model(LOCAL_MODEL_NAME)
    return _local_models[LOCAL_MODEL_NAME]


def _transcribe_local(audio_path, status_callback):
    _log(status_callback, f"Transcribing locally with Whisper ({LOCAL_MODEL_NAME})...")
    model = _load_local_model()
    # Word timestamps are required by the caption renderer.
    return model.transcribe(audio_path, word_timestamps=True)


def transcribe(audio_path, status_callback=None, timeout=60):
    """Transcribe `audio_path` into Whisper's result format.

    Tries Groq first, then falls back to a locally cached Whisper model.
    Returns a dict with a "segments" list carrying word-level timings.
    """
    try:
        result = _transcribe_groq(audio_path, status_callback, timeout=timeout)
        if result is not None:
            return result
    except Exception as exc:
        _log(status_callback, f"Cloud transcription unavailable ({exc}). Using the local model instead.")

    return _transcribe_local(audio_path, status_callback)
