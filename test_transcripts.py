import pytest

from transcripts import (
    Store,
    choose_best_vtt,
    clean_cue_text,
    dedupe_rolling_captions,
    extract_cue_lines,
    is_bot_check_error,
    pack_bundles,
    parse_timestamp,
    parse_vtt,
    slugify,
    to_passages,
    to_prose,
    to_timed,
    walk_playlist_entries,
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


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------

def test_to_prose_reflows_into_paragraphs():
    lines = [(0.0, " ".join(f"w{i}" for i in range(250)))]
    prose = to_prose(lines, words_per_paragraph=110)
    paragraphs = prose.split("\n\n")
    assert len(paragraphs) == 3
    assert len(paragraphs[0].split()) == 110
    assert len(paragraphs[2].split()) == 30


def test_to_timed_formats_timestamps():
    lines = [(65.0, "hello"), (3661.0, "world")]
    timed = to_timed(lines)
    assert "[00:01:05] hello" in timed
    assert "[01:01:01] world" in timed


def test_passages_chunk_to_45_words_with_first_appearance_time():
    lines = [(float(i), f"w{i}") for i in range(100)]
    passages = to_passages(lines, video_id="vid1", run_id="run1", title="T", chunk_words=45)
    assert len(passages) == 3
    assert [len(p["body"].split()) for p in passages] == [45, 45, 10]
    assert passages[0]["at"] == 0.0
    assert passages[1]["at"] == 45.0
    assert passages[2]["at"] == 90.0
    assert all(p["video_id"] == "vid1" and p["run_id"] == "run1" for p in passages)


# ---------------------------------------------------------------------------
# Bundle packing
# ---------------------------------------------------------------------------

def test_bundles_never_split_a_document_and_isolate_oversized_ones():
    budget = 20_000
    documents = [
        {"title": "A", "video_id": "v1", "upload_date": "20200101", "text": "word " * 1000},
        {"title": "B", "video_id": "v2", "upload_date": "20200102", "text": "word " * 1000},
        {"title": "Huge", "video_id": "v3", "upload_date": "20200103", "text": "word " * 100_000},
        {"title": "C", "video_id": "v4", "upload_date": "20200104", "text": "word " * 1000},
    ]
    bundles = pack_bundles(documents, source="Test Channel", budget_chars=budget)

    all_titles = [t for b in bundles for t in b["titles"]]
    assert sorted(all_titles) == sorted(d["title"] for d in documents)  # nothing dropped, nothing duplicated

    huge_bundles = [b for b in bundles if "Huge" in b["titles"]]
    assert len(huge_bundles) == 1
    assert huge_bundles[0]["videos"] == 1  # isolated, not merged with anything

    for b in bundles:
        if "Huge" not in b["titles"]:
            assert b["chars"] <= budget


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------

def test_slugify_handles_special_chars_emoji_and_rtl():
    result = slugify("Why 90% Fail: A Post-Mortem | Ep. 44 🔥 שלום עולם", "abc123XYZ_-")
    assert isinstance(result, str)
    for bad in '\\/*?:"<>|':
        assert bad not in result.replace("[abc123XYZ_-]", "")
    assert "[abc123XYZ_-]" in result


def test_slugify_handles_empty_title():
    result = slugify("", "vid1")
    assert result == "untitled [vid1]"


# ---------------------------------------------------------------------------
# Store: round trip + FTS phrase across a cue boundary
# ---------------------------------------------------------------------------

def test_store_round_trip_search(tmp_path):
    store = Store(tmp_path / "steno.db")
    store.create_run("run1", "https://youtube.com/@x", {})
    store.add_passages([
        {"body": "the quick brown fox jumps over the lazy dog", "video_id": "vid1", "run_id": "run1", "at": 12.0, "title": "Video One"},
    ])
    results = store.search("fox", run_id="run1")
    assert len(results) == 1
    assert results[0]["video_id"] == "vid1"
    assert results[0]["at"] == 12.0
    assert results[0]["url"] == "https://youtu.be/vid1?t=12"


def test_fts_phrase_spanning_original_cue_boundary(tmp_path):
    # Two source cue lines, each 3 words — a naive per-cue index could never
    # find a phrase spanning the boundary between them. Chunking into one
    # 45-word-or-fewer passage keeps the phrase searchable.
    lines = [(0.0, "the quick brown"), (1.0, "fox jumps over")]
    passages = to_passages(lines, video_id="vid1", run_id="run1", title="Video One", chunk_words=45)
    assert len(passages) == 1  # both cues landed in the same passage

    store = Store(tmp_path / "steno.db")
    store.create_run("run1", "https://youtube.com/@x", {})
    store.add_passages(passages)

    results = store.search('"brown fox"', run_id="run1")
    assert len(results) == 1
    assert results[0]["video_id"] == "vid1"


# ---------------------------------------------------------------------------
# choose_best_vtt ranking
# ---------------------------------------------------------------------------

def test_choose_best_vtt_prefers_exact_then_non_orig_then_shortest(tmp_path):
    for name in ["abc.en-orig.vtt", "abc.en-US.vtt", "abc.en.vtt"]:
        (tmp_path / name).write_text("WEBVTT\n")
    chosen = choose_best_vtt(tmp_path, "abc", "en")
    assert chosen.name == "abc.en.vtt"


# ---------------------------------------------------------------------------
# walk_playlist_entries: cross-tab duplicate id
# ---------------------------------------------------------------------------

def test_walk_dedupes_video_appearing_under_two_tabs():
    # A video that was streamed live and is now archived shows up under both
    # the Videos tab and the Live tab with the same id. A wrong walk would
    # count it twice — doubling its weight in every bundle and returning it
    # twice in search. Constructed directly rather than hunting for a real
    # channel where this happens naturally, since it's the one branch a
    # wrong result is invisible in.
    fake_info = {
        "title": "Fake Channel",
        "entries": [
            {
                "title": "Videos",
                "entries": [
                    {"id": "dup123", "title": "Shared Video", "duration": 100, "upload_date": "20200101"},
                    {"id": "vid_only", "title": "Videos Only", "duration": 50, "upload_date": "20200102"},
                ],
            },
            {
                "title": "Live",
                "entries": [
                    {"id": "dup123", "title": "Shared Video (Live VOD)", "duration": 100,
                     "upload_date": "20200101", "live_status": "was_live"},
                    {"id": "live_only", "title": "Live Only", "duration": 200,
                     "upload_date": "20200103", "live_status": "was_live"},
                ],
            },
        ],
    }

    result = walk_playlist_entries(fake_info, include_shorts=True)
    ids = [v["id"] for v in result]

    assert ids.count("dup123") == 1
    assert sorted(ids) == ["dup123", "live_only", "vid_only"]
    # First-seen wins — the Videos-tab copy, not the Live-tab copy that
    # arrived second and got silently discarded by the `seen` check.
    dup_entry = next(v for v in result if v["id"] == "dup123")
    assert dup_entry["title"] == "Shared Video"


def test_walk_drops_live_and_upcoming_but_keeps_was_live():
    fake_info = {
        "title": "Fake Channel",
        "entries": [
            {
                "title": "Live",
                "entries": [
                    {"id": "currently_live", "title": "Live Now", "live_status": "is_live"},
                    {"id": "scheduled", "title": "Starts Soon", "live_status": "is_upcoming"},
                    {"id": "archived", "title": "Past Stream", "live_status": "was_live"},
                ],
            },
        ],
    }
    result = walk_playlist_entries(fake_info, include_shorts=True)
    ids = [v["id"] for v in result]
    assert ids == ["archived"]


def test_walk_excludes_shorts_tab_when_disabled():
    fake_info = {
        "title": "Fake Channel",
        "entries": [
            {"title": "Videos", "entries": [{"id": "v1", "title": "A Video"}]},
            {"title": "Shorts", "entries": [{"id": "s1", "title": "A Short"}]},
        ],
    }
    with_shorts = walk_playlist_entries(fake_info, include_shorts=True)
    without_shorts = walk_playlist_entries(fake_info, include_shorts=False)
    assert sorted(v["id"] for v in with_shorts) == ["s1", "v1"]
    assert sorted(v["id"] for v in without_shorts) == ["v1"]


# ---------------------------------------------------------------------------
# Bot-check detection, against real observed message strings
# ---------------------------------------------------------------------------

def test_bot_check_matches_real_sign_in_message():
    msg = "ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you're not a bot. This helps protect our community. Learn more"
    assert is_bot_check_error(msg)


def test_bot_check_matches_real_429_message():
    # The exact message this project produced when a language wildcard
    # matched too many auto-translated subtitle tracks in one request.
    msg = "Unable to download video subtitles for 'en-de': HTTP Error 429: Too Many Requests"
    assert is_bot_check_error(msg)


def test_bot_check_does_not_match_unrelated_errors():
    for msg in ["Video unavailable", "This video is private", "HTTP Error 404: Not Found"]:
        assert not is_bot_check_error(msg)
