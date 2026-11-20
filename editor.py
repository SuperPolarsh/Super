import os
import argparse
import shutil
import tempfile
import threading
import cv2
import numpy as np
from PIL import Image, ImageFont
import subprocess
from moviepy.editor import VideoFileClip, concatenate_videoclips, TextClip, CompositeVideoClip
from moviepy.config import change_settings, get_setting
from subject_tracker import compute_crop_dimensions, crop_expression, plan_subject_tracking


def _configure_imagemagick():
    """Points MoviePy at an installed ImageMagick, if there is one.

    Only the optional text-overlay helper needs it, so a missing copy must
    not stop the app (or caption rendering, which uses ffmpeg drawtext).
    """
    candidates = [
        os.environ.get("IMAGEMAGICK_BINARY"),
        r"C:\Program Files\ImageMagick-7.1.2-Q16-HDRI\magick.exe",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            change_settings({"IMAGEMAGICK_BINARY": candidate})
            return


_configure_imagemagick()


def _ffmpeg_binary():
    """Returns a usable ffmpeg path, even when MoviePy has none configured."""
    try:
        configured = get_setting("FFMPEG_BINARY")
    except Exception:
        configured = None
    if configured and (os.path.isfile(configured) or shutil.which(configured)):
        return configured
    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        bundled = ""
    if bundled and os.path.isfile(bundled):
        try:
            change_settings({"FFMPEG_BINARY": bundled})
        except Exception:
            pass
        return bundled
    return configured or "ffmpeg"


# Messages ffmpeg prints when NVENC/CUDA is missing or broken. Matching these
# lets the renderer retry on the CPU instead of failing the whole job.
_GPU_FAILURE_MARKERS = (
    "nvenc", "cuda", "no capable devices", "cannot load nvcuda",
    "openencodesessionex failed", "device not found", "unknown encoder",
    "encoder not found", "initialization failed", "driver does not support",
)


def _is_gpu_failure(error):
    lowered = str(error).lower()
    return any(marker in lowered for marker in _GPU_FAILURE_MARKERS)


def _status(callback, message):
    print(message)
    if callback:
        callback(message)


def _run_ffmpeg(command, clip_duration, status_callback=None, prefix="Rendering reframed clip"):
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # Drain stderr on a thread: reading it only after stdout finishes can
    # deadlock when ffmpeg emits a large error dump mid-render (pipe buffers).
    stderr_chunks = []
    stderr_reader = threading.Thread(
        target=lambda: stderr_chunks.append(process.stderr.read()), daemon=True)
    stderr_reader.start()
    for line in process.stdout:
        if line.startswith("out_time_us="):
            try:
                seconds = int(line.split("=", 1)[1]) / 1_000_000
                percent = min(
                    99, int(seconds / max(clip_duration, 0.01) * 100))
                _status(status_callback,
                        f"{prefix}: {percent}%")
            except ValueError:
                pass
    stderr_reader.join()
    stderr = stderr_chunks[0] if stderr_chunks else ""
    if process.wait() != 0:
        detail = stderr.strip()[-1200:] or "Unknown FFmpeg failure"
        raise RuntimeError(f"FFmpeg could not render this clip: {detail}")


def _wide_filter(source_width, source_height):
    if abs(source_width / source_height - 16 / 9) < 0.01:
        return "[0:v]scale=1920:1080:flags=lanczos,setsar=1[reframed]"
    return (
        "[0:v]split=2[background][foreground];"
        "[background]scale=1920:1080:force_original_aspect_ratio=increase,"
        "crop=1920:1080,boxblur=20:10[blurred];"
        "[foreground]scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos[fit];"
        "[blurred][fit]overlay=(W-w)/2:(H-h),setsar=1[reframed]"
    )


def clip_video(input_path, start_time, end_time, output_path, vertical=False, aspect_ratio=None, max_duration=30, status_callback=None):
    ratio = aspect_ratio or ("9:16" if vertical else "Original")
    if ratio not in {"Original", "9:16", "16:9"}:
        raise ValueError("Aspect ratio must be Original, 9:16, or 16:9.")
    if end_time <= start_time:
        raise ValueError("Clip end time must be after its start time.")
    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input video was not found: {input_path}")

    # Enforce maximum duration from the UI slider
    duration = min(end_time - start_time, max_duration)

    with VideoFileClip(input_path) as video:
        source_width, source_height = video.size

    filters = None
    video_map = "0:v:0"
    if ratio == "9:16":
        if abs(source_width / source_height - 9 / 16) < 0.02:
            _status(status_callback, "Source is already 9:16; normalizing to 1080x1920.")
            filters = "[0:v]scale=1080:1920:flags=lanczos,setsar=1[reframed]"
            video_map = "[reframed]"
        else:
            _status(status_callback, "Tracking the main subject locally...")
            plan = plan_subject_tracking(
                input_path, start_time, end_time, progress_callback=status_callback)
            if plan.detected_subjects:
                _status(
                    status_callback, f"Subject tracking found {plan.detected_subjects} face/body samples.")
            else:
                _status(status_callback,
                        "No face detected; using a centered vertical crop.")
            x_expression = crop_expression(plan.points, "x")
            y_expression = crop_expression(plan.points, "y")
            filters = (
                f"[0:v]crop=w={plan.crop_width}:h={plan.crop_height}:x='{x_expression}':y='{y_expression}',"
                "scale=1080:1920:flags=lanczos,setsar=1[reframed]"
            )
            video_map = "[reframed]"
    elif ratio == "16:9":
        if abs(source_width / source_height - 16 / 9) < 0.02:
            _status(status_callback, "Source is already 16:9; normalizing to 1920x1080.")
            filters = "[0:v]scale=1920:1080:flags=lanczos,setsar=1[reframed]"
            video_map = "[reframed]"
        else:
            filters = _wide_filter(source_width, source_height)
            video_map = "[reframed]"

    ffmpeg = _ffmpeg_binary()
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", input_path,
        "-ss", f"{start_time:.3f}", "-t", f"{duration:.3f}", "-map", video_map, "-map", "0:a?",
    ]
    if filters:
        command.extend(["-filter_complex", filters])
    
    # HYBRID OPTIMIZATION:
    # Try NVENC (Nvidia GPU) first for maximum speed. 
    # We use -preset p1 (fastest) and a high quality profile.
    command.extend([
        "-c:v", "h264_nvenc", "-preset", "p1", "-rc", "vbr", "-cq", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", output_path,
    ])
    
    _status(status_callback, f"Rendering {ratio} clip (GPU Accelerated)...")
    try:
        _run_ffmpeg(command, duration, status_callback=status_callback)
    except RuntimeError as e:
        if _is_gpu_failure(e):
            _status(status_callback, "GPU encoder unavailable, using the CPU instead...")
            fallback_command = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", input_path,
                "-ss", f"{start_time:.3f}", "-t", f"{duration:.3f}", "-map", video_map, "-map", "0:a?",
            ]
            if filters: fallback_command.append("-filter_complex")
            if filters: fallback_command.append(filters)
            fallback_command.extend([
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", output_path,
            ])
            _run_ffmpeg(fallback_command, duration, status_callback=status_callback)
        else:
            raise e

    _status(status_callback, "Reframed clip export complete.")
    return output_path


def _crop_to_ratio(frame, ratio):
    """Center-crops a BGR frame to the requested aspect ratio."""
    h, w = frame.shape[:2]
    if ratio == "9:16":
        target_w = h * 9 / 16
        if target_w > w:
            target_w = w
            target_h = w * 16 / 9
            y = max(0, (h - target_h) // 2)
            return frame[int(y):int(y + target_h), :]
        x = (w - target_w) // 2
        return frame[:, int(x):int(x + target_w)]
    if ratio == "16:9":
        target_h = w * 9 / 16
        if target_h > h:
            target_h = h
            target_w = h * 16 / 9
            x = max(0, (w - target_w) // 2)
            return frame[:, int(x):int(x + target_w)]
        y = (h - target_h) // 2
        return frame[int(y):int(y + target_h), :]
    return frame


def crop_image_to_ratio(img, ratio):
    """Center-crops a PIL image to the requested aspect ratio."""
    arr = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    res = _crop_to_ratio(arr, ratio)
    if res.size == 0:
        return None
    return Image.fromarray(cv2.cvtColor(res, cv2.COLOR_BGR2RGB))


def _best_preview_frame(capture):
    """Grabs a representative frame, skipping black intros and fades.

    Sampling the first second often lands on a logo/fade/black frame, which
    made the preview look broken. We sample three points across the video and
    keep the brightest, most detailed one.
    """
    frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    fps = capture.get(cv2.CAP_PROP_FPS) or 0
    duration = frame_count / fps if frame_count > 0 and fps > 0 else 0

    if duration <= 0:
        success, frame = capture.read()
        return frame if success else None

    best_score, best_frame = -1.0, None
    for fraction in (0.15, 0.4, 0.65):
        capture.set(cv2.CAP_PROP_POS_MSEC, duration * fraction * 1000)
        success, frame = capture.read()
        if not success:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Brightness plus detail: black fades score 0, flat colour walls low.
        score = float(gray.mean()) + float(gray.std())
        if score > best_score:
            best_score, best_frame = score, frame

    if best_frame is None:
        capture.set(cv2.CAP_PROP_POS_MSEC, 0)
        success, frame = capture.read()
        best_frame = frame if success else None
    return best_frame


def get_preview_frame(video_path, ratio="Original", with_size=False, at_seconds=None, max_size=300):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return (None, None) if with_size else None
    if at_seconds is None:
        frame = _best_preview_frame(cap)
        success = frame is not None
    else:
        if at_seconds:
            cap.set(cv2.CAP_PROP_POS_MSEC, at_seconds * 1000)
        success, frame = cap.read()
        if not success and at_seconds:
            cap.set(cv2.CAP_PROP_POS_MSEC, 0)
            success, frame = cap.read()
    cap.release()
    if not success:
        return (None, None) if with_size else None

    source_h = frame.shape[0]
    res = _crop_to_ratio(frame, ratio)
    if res.size == 0:
        return (None, None) if with_size else None

    img = Image.fromarray(cv2.cvtColor(res, cv2.COLOR_BGR2RGB))
    img.thumbnail((max_size, max_size))
    return (img, source_h) if with_size else img


def join_clips(clip_paths, output_path):
    print(f"Joining {len(clip_paths)} clips...")
    clips = [VideoFileClip(p) for p in clip_paths]
    final_clip = concatenate_videoclips(clips)
    final_clip.write_videofile(output_path, codec="libx264")
    for c in clips:
        c.close()
    return output_path


def _quote_filter_value(value):
    """Escapes a value for use inside a single-quoted ffmpeg filter option.

    Colons must be escaped even inside quotes: the filtergraph option parser
    splits on ':' before honouring the quotes (matters for Windows paths).
    """
    return "'" + (value.replace("\\", "\\\\")
                       .replace(":", "\\:")
                       .replace("'", "\u2019")) + "'"


def _caption_fontfile():
    """Finds a bold system font for drawtext captions."""
    for candidate in (
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\Arial Bold.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


def _video_dimensions(video_path):
    """Returns (width, height) of the first frame, with a safe fallback."""
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        return 1920, 1080
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
    capture.release()
    return width, height


def get_video_size(video_path):
    """Public helper: returns the (width, height) of a video file."""
    return _video_dimensions(video_path)


def _drawtext_filters(words_data, fontsize, frame_width=1920, frame_height=1080):
    """Builds one chained drawtext filtergraph: a timed caption per word."""
    POWER_WORDS = {
        "money": "#00ff00", "cash": "#00ff00", "wealth": "#00ff00", "rich": "#00ff00", "profit": "#00ff00",
        "insane": "#ff00ff", "crazy": "#ff00ff", "shocking": "#ff00ff", "unbelievable": "#ff00ff", "wow": "#ff00ff",
        "secret": "#00ffff", "hidden": "#00ffff", "hack": "#00ffff", "truth": "#00ffff",
        "danger": "#ff0000", "stop": "#ff0000", "warning": "#ff0000",
    }
    # Vibrant colors for the automatic rotation
    COLOR_CYCLE = ["#FFFF00", "#00FFFF", "#FF00FF", "#00FF00", "#FFA500", "#FFD700"]
    
    filters = []
    fontfile = _caption_fontfile()
    if fontfile is None:
        raise RuntimeError("No system bold font found for captions (expected arialbd.ttf).")

    max_width = max(1, int(frame_width * 0.92))
    max_height = max(1, int(frame_height * 0.92))

    # Sanitise timings: transcription word timings often overlap, which used
    # to draw two words on top of each other ("stacked" captions).
    words = []
    for item in words_data:
        word_text = str(item.get("word", "")).strip().upper()
        try:
            start = float(item.get("start", 0))
            end = float(item.get("end", 0))
        except (TypeError, ValueError):
            continue
        if word_text and end > start:
            words.append([max(0.0, start), end, word_text])
    words.sort(key=lambda entry: entry[0])

    # Each caption ends before the next one starts, so only one is visible at
    # any moment. Overlapping transcript timings are trimmed; when two words
    # start almost simultaneously the later one is nudged forward instead of
    # being drawn on top of the previous word.
    min_duration = 0.05
    gap = 0.002
    for index, entry in enumerate(words):
        start = entry[0]
        end = max(entry[1], start + min_duration)
        if index + 1 < len(words):
            next_start = max(words[index + 1][0], start + min_duration + gap)
            words[index + 1][0] = next_start
            end = min(end, next_start - gap)
        else:
            end = max(end, start + 0.3)
        entry[0], entry[1] = start, end

    for idx, (start, end, word_text) in enumerate(words):
        clean_word = word_text.lower()

        # 1. Check for Power Words first
        color = None
        for power_word, power_color in POWER_WORDS.items():
            if power_word in clean_word:
                color = power_color
                break

        # 2. If not a power word, cycle color every 3 words
        if color is None:
            color_idx = (idx // 3) % len(COLOR_CYCLE)
            color = COLOR_CYCLE[color_idx]

        # Shrink the word until it fits inside the frame (prevents overflow).
        size = int(fontsize)
        while size > 8:
            try:
                probe = ImageFont.truetype(fontfile, size)
            except Exception:
                break
            box = probe.getbbox(word_text)
            if (box[2] - box[0]) <= max_width and (box[3] - box[1]) <= max_height:
                break
            size -= 2

        filters.append(
            "drawtext="
            f"fontfile={_quote_filter_value(fontfile)}"
            f":text={_quote_filter_value(word_text)}"
            f":fontsize={size}:fontcolor={color}"
            ":borderw=2:bordercolor=black"
            ":x=(w-text_w)/2:y=(h-text_h)/2"
            ":expansion=none"
            f":enable={_quote_filter_value(f'between(t,{start:.3f},{end:.3f})')}"
        )
    return filters


def add_dynamic_captions(input_path, words_data, output_path, fontsize=70, status_callback=None):
    """Burns word-by-word captions in a single fast ffmpeg pass.

    Each word becomes one timed drawtext filter, so the whole clip is
    re-encoded once (~real time) instead of frame-by-frame in Python.
    """
    _status(status_callback, "Adding professional dynamic captions...")
    frame_width, frame_height = _video_dimensions(input_path)
    filters = _drawtext_filters(words_data, fontsize, frame_width, frame_height)
    if not filters:
        raise ValueError("No caption words were provided.")

    duration = max(float(item["end"]) for item in words_data if item.get("end") is not None)
    graph = "[0:v]" + ",".join(filters) + "[captioned]"

    ffmpeg = _ffmpeg_binary()
    # A 90s clip can produce hundreds of drawtext filters, and Windows caps a
    # process command line at ~32k characters ("WinError 206"). Passing the
    # graph through a file avoids that limit entirely.
    script_fd, script_path = tempfile.mkstemp(prefix="viral_caption_graph_", suffix=".txt")
    try:
        with os.fdopen(script_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(graph)

        # HYBRID OPTIMIZATION:
        # We prioritize NVENC (Nvidia GPU) for the massive speed boost.
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", input_path,
            "-filter_complex_script", script_path, "-map", "[captioned]", "-map", "0:a?",
            "-c:v", "h264_nvenc", "-preset", "p1", "-rc", "vbr", "-cq", "23", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", output_path,
        ]

        try:
            _run_ffmpeg(command, duration, status_callback=status_callback, prefix="Adding captions")
        except RuntimeError as e:
            if _is_gpu_failure(e):
                _status(status_callback, "GPU encoder unavailable, using the CPU for captions...")
                # Fallback to the max-speed CPU settings
                fallback_command = [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", input_path,
                    "-filter_complex_script", script_path, "-map", "[captioned]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", output_path,
                ]
                _run_ffmpeg(fallback_command, duration, status_callback=status_callback, prefix="Adding captions")
            else:
                raise e
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass

    return output_path


def add_text_overlay(input_path, text, output_path, duration=3.0):
    with VideoFileClip(input_path) as video:
        title = (TextClip(text, fontsize=70, color="white", font="Arial-Bold")
                 .set_start(0).set_duration(min(duration, video.duration))
                 .set_position(("center", "center")))
        result = CompositeVideoClip([video, title])
        result.write_videofile(output_path, codec="libx264", audio_codec="aac")
        result.close()
        title.close()
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Simple Video Editor CLI")
    subparsers = parser.add_subparsers(
        dest="command", help="Available commands")

    clip_parser = subparsers.add_parser("clip", help="Clip a video")
    clip_parser.add_argument("input", help="Input video file")
    clip_parser.add_argument("start", type=float, help="Start time in seconds")
    clip_parser.add_argument("end", type=float, help="End time in seconds")
    clip_parser.add_argument("output", help="Output video file")
    clip_parser.add_argument("--ratio", choices=["Original", "9:16", "16:9"], default="Original",
                             help="Output aspect ratio")

    join_parser = subparsers.add_parser("join", help="Join clips")
    join_parser.add_argument("inputs", nargs="+", help="Input video files")
    join_parser.add_argument("output", help="Output video file")

    text_parser = subparsers.add_parser("text", help="Add a text overlay")
    text_parser.add_argument("input", help="Input video file")
    text_parser.add_argument("text", help="Overlay text")
    text_parser.add_argument("output", help="Output video file")
    text_parser.add_argument("--duration", type=float,
                             default=3.0, help="Overlay duration in seconds")

    args = parser.parse_args()
    if args.command == "clip":
        clip_video(args.input, args.start, args.end,
                   args.output, aspect_ratio=args.ratio)
    elif args.command == "join":
        join_clips(args.inputs, args.output)
    elif args.command == "text":
        add_text_overlay(args.input, args.text, args.output,
                         duration=args.duration)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
