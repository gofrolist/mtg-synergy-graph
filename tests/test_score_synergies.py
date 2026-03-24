from score_synergies import parse_response


def test_parse_valid_json_array():
    raw = '[{"name": "Sol Ring", "score": 4.5, "reason": "Generic ramp"}]'
    result = parse_response(raw, 1)
    assert len(result) == 1
    assert result[0]["name"] == "Sol Ring"
    assert result[0]["score"] == 4.5
    assert result[0]["reason"] == "Generic ramp"


def test_parse_float_scores():
    raw = '[{"name": "A", "score": 7.3, "reason": "x"}, {"name": "B", "score": 2.8, "reason": "y"}]'
    result = parse_response(raw, 2)
    assert result[0]["score"] == 7.3
    assert result[1]["score"] == 2.8


def test_parse_integer_scores_converted_to_float():
    raw = '[{"name": "A", "score": 7, "reason": "x"}]'
    result = parse_response(raw, 1)
    assert isinstance(result[0]["score"], float)
    assert result[0]["score"] == 7.0


def test_parse_with_markdown_fences():
    raw = '```json\n[{"name": "A", "score": 5.5, "reason": "x"}]\n```'
    result = parse_response(raw, 1)
    assert len(result) == 1
    assert result[0]["score"] == 5.5


def test_parse_with_thinking_tags():
    raw = '<think>Let me analyze...</think>[{"name": "A", "score": 6.0, "reason": "x"}]'
    result = parse_response(raw, 1)
    assert len(result) == 1


def test_parse_empty_response():
    result = parse_response("", 5)
    assert result is None


def test_parse_none_response():
    result = parse_response(None, 5)
    assert result is None


def test_parse_truncated_json():
    """Truncated JSON (finish_reason=length) should still parse what it can."""
    raw = '[{"name": "A", "score": 5.0, "reason": "x"}, {"name": "B", "score": 6.0, "rea'
    result = parse_response(raw, 2)
    # Regex search for [...] fails on incomplete JSON, so returns None
    assert result is None


def test_parse_score_out_of_range():
    raw = '[{"name": "A", "score": 15, "reason": "x"}, {"name": "B", "score": 5.0, "reason": "y"}]'
    result = parse_response(raw, 2)
    assert len(result) == 1
    assert result[0]["name"] == "B"


def test_parse_count_mismatch_still_returns():
    """If LLM returns fewer cards than expected, still parse what we got."""
    raw = '[{"name": "A", "score": 5.0, "reason": "x"}]'
    result = parse_response(raw, 10)  # Expected 10, got 1
    assert len(result) == 1


def test_parse_dict_response():
    """Single dict (not array) should be wrapped."""
    raw = '{"name": "A", "score": 5.0, "reason": "x"}'
    result = parse_response(raw, 1)
    assert len(result) == 1
