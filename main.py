import json
import os
import argparse
from datetime import datetime
from viral_analyzer import analyze_virality, MAX_CLIP_LENGTH
from editor import clip_video, add_dynamic_captions, get_video_size
from runtime_utils import hide_child_console_windows

hide_child_console_windows()


def auto_generate_viral_clips(video_path, min_score=30, output_folder="viral_clips",
                              captions=True, fallback_duration=30, aspect_ratio="Original",
                               caption_size=70, on_clip=None, status_callback=None):
    """
    Finds viral moments, AI-audits each one (trust breakdown + ideal length),
    and automatically cuts them into separate files.

    Returns a list of moment dicts (each includes "path" plus the score
    breakdown) and writes a report.json summary into the output folder.
    """
    def status(message):
        print(message)
        if status_callback:
            status_callback(message)

    status(f"Starting analysis: {os.path.basename(video_path)}")

    # 1. Analyze the video to find moments (lengths already chosen by the AI)
    moments = analyze_virality(video_path, fallback_duration=fallback_duration, status_callback=status_callback)

    if not moments:
        status("No high-interest moments were detected in this video.")
        return []

    # 2. Filter for only high-score moments
    viral_moments = [m for m in moments if m['score'] >= min_score]

    if not viral_moments:
        status(f"No moments scored {min_score} or higher. Try a lower threshold.")
        return []

    status(f"Found {len(viral_moments)} candidate clip(s). Rendering {aspect_ratio} clips now...")

    # Create an output folder for the clips
    os.makedirs(output_folder, exist_ok=True)
    rendered = []
    failures = []

    # 3. Loop through and cut each one
    for i, moment in enumerate(viral_moments):
        start = moment['start']
        end = moment['end']
        score = moment['score']

        clip_name = f"clip_{i:02d}_score_{score}.mp4"
        temp_clip_path = os.path.join(output_folder, f"temp_{clip_name}")
        final_clip_path = os.path.join(output_folder, clip_name)

        status(f"Rendering {i + 1}/{len(viral_moments)} - score {score}, trust {moment['confidence']}, "
               f"{start:.1f}s-{end:.1f}s ({end - start:.0f}s)")

        try:
            clip_video(video_path, start, end, temp_clip_path, aspect_ratio=aspect_ratio,
                       max_duration=MAX_CLIP_LENGTH, status_callback=status_callback)

            if captions:
                words_data = moment.get('words') or []
                if not words_data:
                    words = moment['text'].split()
                    duration = end - start
                    words_data = [{"word": word, "start": idx * duration / len(words),
                                   "end": (idx + 1) * duration / len(words)}
                                  for idx, word in enumerate(words)]
                add_dynamic_captions(temp_clip_path, words_data, final_clip_path,
                                     fontsize=caption_size,
                                     status_callback=status_callback)
                if os.path.exists(temp_clip_path):
                    os.remove(temp_clip_path)
            else:
                os.replace(temp_clip_path, final_clip_path)

            moment = dict(moment)
            moment["path"] = final_clip_path
            moment["rank"] = i + 1
            out_w, out_h = get_video_size(final_clip_path)
            status(f"Saved {clip_name} ({out_w}x{out_h})")
            rendered.append(moment)
            if on_clip:
                on_clip(dict(moment))

        except Exception as e:
            failures.append(str(e))
            status(f"Could not render clip {i}: {e}")

    if not rendered and failures:
        # Every clip failed: surface the first real reason instead of finishing
        # with a cheerful "created 0 clip(s)" and no explanation.
        raise RuntimeError(f"None of the clips could be rendered. First problem: {failures[0]}")

    # 4. Write the trust report next to the clips so results can be reviewed later
    report = {
        "source": os.path.abspath(video_path),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "min_score": min_score,
        "clips": [
            {
                "rank": moment["rank"],
                "file": os.path.basename(moment["path"]),
                "start": round(moment["start"], 2),
                "end": round(moment["end"], 2),
                "length_seconds": round(moment["end"] - moment["start"]),
                "score": moment["score"],
                "heuristic_score": moment.get("heuristic_score"),
                "hook": moment.get("hook"),
                "retention": moment.get("retention"),
                "payoff": moment.get("payoff"),
                "confidence": moment.get("confidence"),
                "reason": moment.get("reason"),
                "text": moment.get("text"),
            }
            for moment in rendered
        ],
    }
    report_path = os.path.join(output_folder, "report.json")
    try:
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        status(f"Trust report saved to {report_path}")
    except OSError as e:
        status(f"Could not write report.json: {e}")

    status(f"Done - created {len(rendered)} clip(s) in {output_folder}.")
    return rendered


def print_summary(clips):
    """Prints a ranked trust report for terminal users."""
    if not clips:
        return
    print("\n=== TRUST REPORT ===")
    for clip in clips:
        print(f"#{clip['rank']}  score {clip['score']:>3}  trust {clip['confidence'].upper():<6} "
              f"hook {clip['hook']} / retention {clip['retention']} / payoff {clip['payoff']}  "
              f"length {clip['end'] - clip['start']:.0f}s")
        print(f"    {clip['reason']}")
        print(f"    -> {clip['path']}")


def main():
    parser = argparse.ArgumentParser(description="AI Viral Clip Generator")
    parser.add_argument("--input", default="input.mp4", help="Input video file")
    parser.add_argument("--score", type=int, default=30, help="Minimum virality score to clip (default: 30)")
    parser.add_argument("--output", default="viral_clips", help="Folder for generated clips")
    parser.add_argument("--no-captions", action="store_true", help="Skip burned-in captions")
    parser.add_argument("--duration", type=int, default=30,
                        help="Fallback clip length in seconds, used only when the AI cannot pick one (default: 30)")
    parser.add_argument("--ratio", choices=["Original", "9:16", "16:9"], default="Original",
                        help="Output aspect ratio (default: Original)")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"Error: {args.input} not found.")
    else:
        clips = auto_generate_viral_clips(args.input, min_score=args.score, output_folder=args.output,
                                          captions=not args.no_captions, fallback_duration=args.duration,
                                          aspect_ratio=args.ratio)
        print_summary(clips)

if __name__ == "__main__":
    main()
