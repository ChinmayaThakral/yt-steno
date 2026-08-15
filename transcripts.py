"""Core logic for yt-steno: caption parsing, chunking, bundling, storage, and
the yt-dlp integration. No Flask here — everything is testable without an
HTTP server or network access (except enumerate_channel / fetch_captions,
which need the network by nature).
"""

from __future__ import annotations

import html
import re
from typing import Optional

# ---------------------------------------------------------------------------
# VTT parsing
# ---------------------------------------------------------------------------

TS_LINE_RE = re.compile(r"^([\d:,.]+)\s*-->\s*[\d:,.]+")
TAG_RE = re.compile(r"<[^>]*>")
SOUND_DESCRIPTOR_RE = re.compile(r"^\[.*\]$")


def parse_timestamp(ts: str) -> float:
    """'HH:MM:SS.mmm' (any number of hour digits) or 'MM:SS.mmm', comma or
    dot decimal, -> seconds. Splitting from the right handles any number of
    leading unit fields generically."""
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + float(p)
    return seconds


def format_hhmmss(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def clean_cue_text(raw: str) -> str:
    # Unescape twice: some feeds double-encode entities (&amp;lt;i&amp;gt;),
    # and a single pass leaves those half-decoded. Idempotent for normal text.
    text = html.unescape(html.unescape(raw))
    text = TAG_RE.sub("", text)
    return text.strip()


def _is_droppable(cleaned: str) -> bool:
    if not cleaned:
        return True
    if cleaned == "-":
        return True
    if SOUND_DESCRIPTOR_RE.match(cleaned):
        return True
    return False


def extract_cue_lines(raw_text: str) -> list[tuple[float, str]]:
    """Line-based VTT state machine. Returns (start_seconds, text) for every
    surviving cue text line, duplicates included (dedupe is a separate step)."""
    lines: list[tuple[float, str]] = []
    skip_block = False
    reading_text = False
    current_start: Optional[float] = None

    for raw_line in raw_text.splitlines():
        stripped = raw_line.strip("\r")

        if stripped.strip() == "":
            skip_block = False
            reading_text = False
            current_start = None
            continue

        if skip_block:
            continue

        head = stripped.strip()
        if head.startswith(("NOTE", "STYLE", "REGION")):
            skip_block = True
            continue
        if head.startswith("WEBVTT") or head.startswith("Kind:") or head.startswith("Language:"):
            continue

        m = TS_LINE_RE.match(head) if "-->" in head else None
        if m:
            current_start = parse_timestamp(m.group(1))
            reading_text = True
            continue

        if not reading_text:
            # Cue index line (or anything else before a timestamp) — drop.
            continue

        cleaned = clean_cue_text(stripped)
        if _is_droppable(cleaned):
            continue
        lines.append((current_start, cleaned))

    return lines


def dedupe_rolling_captions(lines: list[tuple[float, str]], window_size: int = 6) -> list[tuple[float, str]]:
    """YouTube auto-captions scroll: each line is emitted 2-3x as it rolls.
    A sliding window of recently emitted lines survives the interleaved
    near-zero-duration cues while still letting a genuinely repeated line,
    minutes later, through."""
    window: list[str] = []
    out: list[tuple[float, str]] = []
    for ts, text in lines:
        key = text.casefold()
        if key in window:
            continue
        out.append((ts, text))
        window.append(key)
        if len(window) > window_size:
            window.pop(0)
    return out


def parse_vtt(raw_text: str) -> list[tuple[float, str]]:
    return dedupe_rolling_captions(extract_cue_lines(raw_text))
