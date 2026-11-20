import json
import os
import re
import time
from groq import Groq
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()

MODEL = "openai/gpt-oss-120b"
MAX_BATCH = 10          # candidates evaluated per LLM request
MAX_TEXT_CHARS = 1200   # transcript characters sent per candidate
VALID_CONFIDENCE = {"high", "medium", "low"}

_SYSTEM_PROMPT = (
    "You are a brutal, high-standard viral content auditor for TikTok, Reels and Shorts. "
    "Your goal is to kill boring content. Most clips are mediocre; only 1% are truly viral. "
    "Respond ONLY with valid JSON."
)


def _clamp(value, low, high):
    """Coerces a value to an int clamped to [low, high], or None if impossible."""
    try:
        return max(low, min(high, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def _coerce_confidence(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value in VALID_CONFIDENCE else None


def _extract_json(raw):
    """Parses the first JSON object found in the model output."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _parse_evaluation(entry):
    """Normalizes one evaluation entry from the model into a safe dict."""
    if not isinstance(entry, dict):
        return None
    score = _clamp(entry.get("score"), 0, 100)
    if score is None:
        return None

    def subscore(key):
        clamped = _clamp(entry.get(key), 0, 100)
        return score if clamped is None else clamped

    confidence = _coerce_confidence(entry.get("confidence"))
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "No explanation provided."
    reason = " ".join(reason.split())[:280]

    return {
        "score": score,
        "hook": subscore("hook"),
        "retention": subscore("retention"),
        "payoff": subscore("payoff"),
        "confidence": confidence or "low",
        "reason": reason,
        "suggested_length": _clamp(entry.get("suggested_length"), 15, 90) or 30,
    }


def evaluate_clip_candidates(candidates, status_callback=None):
    """
    Batch-evaluates candidate clip transcripts with the LLM.

    candidates: list of dicts with an "id" and "text" (optional "duration").
    Returns {id: evaluation} where each evaluation has:
        score, hook, retention, payoff, confidence, reason, suggested_length
    Candidates the model could not evaluate are simply absent from the result,
    so callers can fall back to their heuristic score.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("Warning: GROQ_API_KEY not found in .env or environment. Returning no evaluations.")
        return {}
    if not candidates:
        return {}

    payload = [
        {
            "id": candidate["id"],
            "duration_seconds": round(float(candidate.get("duration", 30)), 1),
            "transcript": str(candidate.get("text", ""))[:MAX_TEXT_CHARS],
        }
        for candidate in candidates[:MAX_BATCH]
    ]

    prompt = f"""
Audit each numbered candidate below as a potential short-form clip (TikTok/Reels/Shorts).

For EACH candidate judge:
1. HOOK: does the opening stop the scroll within 0.5 seconds?
2. RETENTION: is there a clear reason to keep watching until the end?
3. PAYOFF: is the ending satisfying, shocking or useful?
4. LENGTH: the ideal clip length in whole seconds (15-90). End on the payoff and
   cut dead filler. Use the candidate's current duration only as a hint.

BE HARSH. Someone just talking normally scores below 20 on every axis.
An amazing hook with a strong payoff scores 80+.
"confidence" is how sure YOU are that this clip performs well on social feeds
("high", "medium" or "low"). "reason" explains the verdict in at most 2 short
sentences, quoting or referencing the transcript.

Respond with JSON exactly in this shape:
{{"evaluations": [{{"id": 0, "score": 0, "hook": 0, "retention": 0, "payoff": 0,
"confidence": "medium", "reason": "...", "suggested_length": 30}}]}}

Candidates:
{json.dumps(payload, ensure_ascii=False)}
"""

    # Retry logic for 503 errors (Overloaded)
    max_retries = 3
    retry_delay = 2  # seconds
    request_timeout = 30  # seconds per API call

    try:
        client = Groq(api_key=api_key)
    except Exception as e:
        print(f"Groq AI Evaluation Error: {e}")
        return {}

    for attempt in range(max_retries):
        try:
            completion = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
                timeout=request_timeout,
            )
            data = _extract_json(completion.choices[0].message.content)
            if not data:
                raise ValueError("model did not return valid JSON")

            evaluations = {}
            valid_ids = {c["id"] for c in payload}
            for entry in data.get("evaluations", []):
                parsed = _parse_evaluation(entry)
                try:
                    entry_id = int(entry.get("id"))
                except (TypeError, ValueError):
                    continue
                if parsed is not None and entry_id in valid_ids:
                    evaluations[entry_id] = parsed
            return evaluations

        except Exception as e:
            err_str = str(e).upper()
            if "503" in err_str or "UNAVAILABLE" in err_str or "RESOURCE_EXHAUSTED" in err_str or "RATE" in err_str:
                if attempt < max_retries - 1:
                    if status_callback:
                        status_callback(f"AI server busy, retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                    continue
            print(f"Groq AI Evaluation Error: {e}")
            return {}
    return {}


def get_llm_virality_score(text):
    """
    Legacy single-transcript helper kept for compatibility.
    Returns a 0-100 int, or None when the AI cannot evaluate.
    """
    evaluations = evaluate_clip_candidates([{"id": 0, "text": text}])
    evaluation = evaluations.get(0)
    return evaluation["score"] if evaluation else None
