import pytest

from ai_evaluator import _clamp, _coerce_confidence, _extract_json, _parse_evaluation


def test_clamp_bounds_and_rounding():
    assert _clamp(120, 0, 100) == 100
    assert _clamp(-5, 0, 100) == 0
    assert _clamp("42.6", 0, 100) == 43
    assert _clamp(None, 0, 100) is None
    assert _clamp("not a number", 0, 100) is None


def test_confidence_must_be_known_word():
    assert _coerce_confidence("HIGH") == "high"
    assert _coerce_confidence(" medium ") == "medium"
    assert _coerce_confidence("certain") is None
    assert _coerce_confidence(7) is None


def test_extracts_json_from_markdown_fences():
    raw = "```json\n{\"evaluations\": []}\n```"
    assert _extract_json(raw) == {"evaluations": []}


def test_extracts_json_from_surrounding_chatter():
    raw = "Here you go: {\"evaluations\": [{\"id\": 0}]} hope this helps"
    assert _extract_json(raw) == {"evaluations": [{"id": 0}]}


def test_parse_evaluation_normalizes_and_fills_defaults():
    parsed = _parse_evaluation({
        "score": 82,
        "hook": 90,
        "retention": "75",
        "payoff": 1000,           # out of range -> clamped to 100
        "confidence": "HIGH",
        "reason": "  Strong hook,\n weak middle.  ",
        "suggested_length": 500,  # out of range -> clamped
    })
    assert parsed == {
        "score": 82,
        "hook": 90,
        "retention": 75,
        "payoff": 100,
        "confidence": "high",
        "reason": "Strong hook, weak middle.",
        "suggested_length": 90,
    }


def test_parse_evaluation_rejects_entries_without_score():
    assert _parse_evaluation({"hook": 10}) is None
    assert _parse_evaluation("not a dict") is None


def test_parse_evaluation_defaults_reason_and_confidence():
    parsed = _parse_evaluation({"score": 30})
    assert parsed["confidence"] == "low"
    assert parsed["reason"] == "No explanation provided."
    assert parsed["suggested_length"] == 30
