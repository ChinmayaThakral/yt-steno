import pytest

from transcripts import (
    clean_cue_text,
    dedupe_rolling_captions,
    extract_cue_lines,
    parse_timestamp,
    parse_vtt,
)


# ---------------------------------------------------------------------------
# Rolling-caption dedupe
# ---------------------------------------------------------------------------

def test_rolling_dedupe_interleaved_pattern():
    lines = [
        (1.0, "so the first thing you need"),
        (3.5, "so the first thing you need"),
        (3.51, "so the first thing you need"),
        (3.51, "to understand about compounding"),
        (6.2, "to understand about compounding"),
        (6.21, "to understand about compounding"),
        (6.21, "is that it starts slow"),
    ]
    result = dedupe_rolling_captions(lines)
    assert result == [
        (1.0, "so the first thing you need"),
        (3.51, "to understand about compounding"),
        (6.21, "is that it starts slow"),
    ]


def test_distant_repetition_survives():
    lines = [(0.0, "we begin")]
    lines += [(float(i), f"line {chr(ord('a') + i - 1)}") for i in range(1, 7)]
    lines.append((1200.0, "we begin"))
    result = dedupe_rolling_captions(lines)
    assert len(result) == 8
    assert result[0] == (0.0, "we begin")
    assert result[-1] == (1200.0, "we begin")


# ---------------------------------------------------------------------------
# VTT extraction
# ---------------------------------------------------------------------------

def test_style_block_does_not_leak():
    raw = (
        "WEBVTT\n\n"
        "STYLE\n"
        "::cue {\n"
        "  color: yellow;\n"
        "}\n\n"
        "00:00:01.000 --> 00:00:02.000\n"
        "Hello there\n"
    )
    lines = extract_cue_lines(raw)
    assert lines == [(1.0, "Hello there")]
    joined = " ".join(t for _, t in lines)
    assert "color" not in joined and "cue" not in joined.lower()


def test_entities_and_tags_cleaned():
    raw = "<c.colorE5E5E5>Hello</c><00:00:01.500><c> world</c> &amp;amp; friends"
    cleaned = clean_cue_text(raw)
    assert "<" not in cleaned and ">" not in cleaned
    assert "amp;" not in cleaned
    assert "&" in cleaned
    assert "Hello" in cleaned and "world" in cleaned and "friends" in cleaned


def test_voice_tag_stripped():
    cleaned = clean_cue_text("<v Speaker>Line of dialogue</v>")
    assert cleaned == "Line of dialogue"


def test_long_form_hours_timestamp():
    assert parse_timestamp("100:00:00.000") == 360000.0


def test_comma_decimal_timestamp():
    assert parse_timestamp("00:00:01,000") == 1.0


def test_music_and_applause_dropped():
    raw = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:02.000\n"
        "[Music]\n\n"
        "00:00:02.000 --> 00:00:03.000\n"
        "Real spoken line\n\n"
        "00:00:03.000 --> 00:00:04.000\n"
        "[Applause]\n"
    )
    lines = extract_cue_lines(raw)
    assert lines == [(2.0, "Real spoken line")]


def test_cue_index_lines_dropped():
    raw = (
        "WEBVTT\n\n"
        "1\n"
        "00:00:01.000 --> 00:00:02.000\n"
        "First line\n\n"
        "2\n"
        "00:00:02.000 --> 00:00:03.000\n"
        "Second line\n"
    )
    lines = extract_cue_lines(raw)
    assert lines == [(1.0, "First line"), (2.0, "Second line")]


def test_crlf_line_endings():
    raw = (
        "WEBVTT\r\n\r\n"
        "00:00:01.000 --> 00:00:02.000\r\n"
        "First line\r\n\r\n"
        "00:00:02.000 --> 00:00:03.000\r\n"
        "Second line\r\n"
    )
    lines = extract_cue_lines(raw)
    assert lines == [(1.0, "First line"), (2.0, "Second line")]
    # No stray \r should survive into the cleaned text.
    assert all("\r" not in text for _, text in lines)


def test_first_cue_at_zero_with_no_blank_line_before_it():
    raw = "WEBVTT\n00:00:00.000 --> 00:00:02.000\nHello world\n"
    lines = extract_cue_lines(raw)
    assert lines == [(0.0, "Hello world")]


def test_full_parse_vtt_end_to_end():
    raw = (
        "WEBVTT\nKind: captions\nLanguage: en\n\n"
        "00:00:01.000 --> 00:00:03.500 align:start position:0%\n"
        "so the<00:00:01.200><c> first</c> thing you need\n\n"
        "00:00:03.500 --> 00:00:03.510\n"
        "so the first thing you need\n\n"
        "00:00:03.510 --> 00:00:06.200\n"
        "so the first thing you need\n"
        "to understand about compounding\n\n"
        "00:00:06.200 --> 00:00:06.210\n"
        "to understand about compounding\n\n"
        "00:00:06.210 --> 00:00:09.000\n"
        "to understand about compounding\n"
        "is that it starts slow\n"
    )
    result = parse_vtt(raw)
    assert result == [
        (1.0, "so the first thing you need"),
        (3.51, "to understand about compounding"),
        (6.21, "is that it starts slow"),
    ]
