"""Core logic for yt-steno: caption parsing, chunking, bundling, storage, and
the yt-dlp integration. No Flask here — everything is testable without an
HTTP server or network access (except enumerate_channel / fetch_captions,
which need the network by nature).
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from yt_dlp import YoutubeDL

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
    if raw_text.startswith("﻿"):
        raw_text = raw_text[1:]

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


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------

def to_prose(lines: list[tuple[float, str]], words_per_paragraph: int = 110) -> str:
    words: list[str] = []
    for _, text in lines:
        words.extend(text.split())
    if not words:
        return ""
    paragraphs = [
        " ".join(words[i:i + words_per_paragraph])
        for i in range(0, len(words), words_per_paragraph)
    ]
    return "\n\n".join(paragraphs)


def to_timed(lines: list[tuple[float, str]]) -> str:
    return "\n".join(f"[{format_hhmmss(ts)}] {text}" for ts, text in lines)


def to_passages(
    lines: list[tuple[float, str]],
    video_id: str,
    run_id: str,
    title: str,
    chunk_words: int = 45,
) -> list[dict]:
    """Group cues into ~45-word passages. This is the search index unit —
    indexing individual cue lines (5-7 words) would make any search phrase
    spanning a line break silently unfindable."""
    passages: list[dict] = []
    buf: list[str] = []
    buf_start: Optional[float] = None

    def flush():
        if buf:
            passages.append({
                "body": " ".join(buf),
                "video_id": video_id,
                "run_id": run_id,
                "at": buf_start,
                "title": title,
            })

    for ts, text in lines:
        words = text.split()
        if not words:
            continue
        if buf_start is None:
            buf_start = ts
        buf.extend(words)
        if len(buf) >= chunk_words:
            flush()
            buf = []
            buf_start = None

    flush()
    return passages


# ---------------------------------------------------------------------------
# Bundle packing
# ---------------------------------------------------------------------------

def _doc_header(title: str, video_id: str, upload_date: str) -> str:
    return f"===== {title} · {video_id} · {upload_date} ====="


def _doc_block(doc: dict) -> str:
    return f"{_doc_header(doc['title'], doc['video_id'], doc.get('upload_date') or 'unknown')}\n\n{doc['text']}\n"


def pack_bundles(documents: list[dict], source: str, budget_chars: int = 300_000) -> list[dict]:
    """Greedy-fill documents into bundles under budget_chars. A document is
    never split across two bundles; an oversized single document gets a
    bundle of its own."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0

    for doc in documents:
        block = _doc_block(doc)
        block_len = len(block)
        if current and current_chars + block_len > budget_chars:
            groups.append(current)
            current, current_chars = [], 0
        current.append(doc)
        current_chars += block_len
        if block_len > budget_chars:
            groups.append(current)
            current, current_chars = [], 0

    if current:
        groups.append(current)

    total = len(groups)
    bundles = []
    for i, docs in enumerate(groups, start=1):
        body = "\n".join(_doc_block(d) for d in docs)
        titles = [d["title"] for d in docs]
        manifest = (
            f"STENO BUNDLE {i} of {total}\n"
            f"source: {source}\n"
            f"videos: {len(docs)}\n"
            f"chars: {len(body)}\n"
            f"est. tokens: {len(body) // 4}\n"
            f"titles:\n" + "\n".join(f"  - {t}" for t in titles) + "\n\n"
        )
        text = manifest + body
        bundles.append({
            "index": i,
            "videos": len(docs),
            "chars": len(text),
            "tokens": len(text) // 4,
            "titles": titles,
            "text": text,
        })
    return bundles


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------

_ILLEGAL_FS_CHARS = re.compile(r'[\\/*?:"<>|\r\n\t]')


def slugify(title: str, video_id: str) -> str:
    base = _ILLEGAL_FS_CHARS.sub("_", title or "").strip()
    base = re.sub(r"\s+", " ", base)[:120].strip()
    if not base:
        base = "untitled"
    return f"{base} [{video_id}]"


# ---------------------------------------------------------------------------
# Storage (SQLite + FTS5)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  url TEXT, source TEXT,
  status TEXT,
  created REAL, finished REAL,
  options TEXT,
  stats TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS videos (
  run_id TEXT, video_id TEXT, title TEXT, uploaded TEXT,
  duration INTEGER, status TEXT,
  words INTEGER, chars INTEGER, note TEXT, position INTEGER,
  PRIMARY KEY (run_id, video_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
  body, video_id UNINDEXED, run_id UNINDEXED, at UNINDEXED, title UNINDEXED,
  tokenize='porter unicode61'
);
"""


class Store:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._local = threading.local()
        with self._lock:
            conn = self._conn()
            conn.executescript(SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    # -- runs --------------------------------------------------------------

    def create_run(self, run_id: str, url: str, options: dict):
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO runs (id, url, source, status, created, finished, options, stats, error) "
                "VALUES (?, ?, ?, 'running', ?, NULL, ?, ?, '')",
                (run_id, url, "", time.time(), json.dumps(options), json.dumps({})),
            )
            conn.commit()

    def update_run(self, run_id: str, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values())
        with self._lock:
            conn = self._conn()
            conn.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*values, run_id))
            conn.commit()

    def get_run(self, run_id: str) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self) -> list[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM runs ORDER BY created DESC").fetchall()
        return [dict(r) for r in rows]

    def delete_run(self, run_id: str):
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            conn.execute("DELETE FROM videos WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM passages WHERE run_id = ?", (run_id,))
            conn.commit()

    # -- videos --------------------------------------------------------------

    def upsert_video(self, run_id: str, video_id: str, **fields):
        fields = {"run_id": run_id, "video_id": video_id, **fields}
        cols = list(fields.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("run_id", "video_id"))
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"INSERT INTO videos ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(run_id, video_id) DO UPDATE SET {updates}",
                [fields[c] for c in cols],
            )
            conn.commit()

    def list_videos(self, run_id: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM videos WHERE run_id = ? ORDER BY position ASC", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- passages / search --------------------------------------------------

    def add_passages(self, passages: list[dict]):
        if not passages:
            return
        with self._lock:
            conn = self._conn()
            conn.executemany(
                "INSERT INTO passages (body, video_id, run_id, at, title) VALUES (?, ?, ?, ?, ?)",
                [(p["body"], p["video_id"], p["run_id"], p["at"], p["title"]) for p in passages],
            )
            conn.commit()

    def search(self, query: str, run_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        if not query or not query.strip():
            return []
        conn = self._conn()
        sql = (
            "SELECT video_id, run_id, at, title, "
            "snippet(passages, 0, '<mark>', '</mark>', '…', 40) AS excerpt "
            "FROM passages WHERE passages MATCH ? "
        )
        params: list = [query]
        if run_id:
            sql += "AND run_id = ? "
            params.append(run_id)
        sql += "ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Malformed FTS5 query syntax — fall back to a literal phrase search.
            phrase = '"' + query.replace('"', '""') + '"'
            params[0] = phrase
            rows = conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["url"] = f"https://youtu.be/{d['video_id']}?t={int(d['at'] or 0)}"
            results.append(d)
        return results


# ---------------------------------------------------------------------------
# yt-dlp integration
# ---------------------------------------------------------------------------

BOT_CHECK_RE = re.compile(r"sign in|confirm you.?re not a bot|429|too many requests", re.I)


def is_bot_check_error(message: str) -> bool:
    return bool(BOT_CHECK_RE.search(message or ""))


def walk_playlist_entries(
    node: Optional[dict],
    include_shorts: bool = True,
    depth: int = 0,
    seen: Optional[set] = None,
    out: Optional[list] = None,
) -> list[dict]:
    """Recursively flatten a yt-dlp flat-extraction info dict (channel ->
    nested tab playlists -> video entries) into a deduped video list. Pulled
    out of enumerate_channel so it's directly testable against a constructed
    fake info dict, without any network call — this is the one branch where
    a wrong result (a video counted twice) is invisible downstream: it just
    silently doubles that video's weight in every bundle and in search."""
    if seen is None:
        seen = set()
    if out is None:
        out = []
    if node is None or depth > 3:
        return out

    entries = node.get("entries")
    if entries is None:
        vid = node.get("id")
        if not vid or vid in seen:
            return out
        if node.get("live_status") in ("is_live", "is_upcoming"):
            return out
        seen.add(vid)
        out.append({
            "id": vid,
            "title": node.get("title") or vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "duration": node.get("duration"),
            "upload_date": node.get("upload_date") or node.get("release_timestamp"),
        })
        return out

    tab_title = str(node.get("title") or "").strip().lower()
    if depth == 1 and not include_shorts and tab_title == "shorts":
        return out
    for e in entries:
        walk_playlist_entries(e, include_shorts, depth + 1, seen, out)
    return out


def enumerate_channel(url: str, limit: int = 0, include_shorts: bool = True) -> tuple[list[dict], str]:
    """List a channel's videos without touching each video page. Channel URLs
    resolve to nested tab playlists (Videos / Shorts / Live); we walk them to
    depth 3, dedupe by id, and drop live/upcoming streams.

    NOTE on `limit`: it's passed to yt-dlp as `playlistend`, which caps each
    tab *independently* during extraction — not the combined total. On a
    channel with active Videos/Live/Shorts tabs, the Videos tab alone can
    fill the whole limit before the walk ever reaches Live or Shorts, so a
    low limit can mean zero Shorts even with include_shorts=True. The
    post-walk videos[:limit] slice below keeps the final *count* correct,
    but in practice `limit` means "newest N uploads," not "newest N items
    across the channel." See the README for the user-facing version of this."""
    opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": True,
    }
    if limit:
        opts["playlistend"] = limit

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        return [], ""

    videos = walk_playlist_entries(info, include_shorts)
    if limit:
        videos = videos[:limit]

    source = info.get("channel") or info.get("uploader") or info.get("title") or "channel"
    return videos, source


def choose_best_vtt(workdir: Path, video_id: str, lang: str) -> Optional[Path]:
    candidates = sorted(workdir.glob(f"{video_id}.*.vtt")) + sorted(workdir.glob(f"{video_id}.vtt"))
    if not candidates:
        return None

    def rank(p: Path):
        stem = p.name[:-4]  # strip ".vtt"
        prefix = f"{video_id}."
        tag = stem[len(prefix):] if stem.startswith(prefix) else ""
        exact = 0 if tag == lang else 1
        orig = 1 if tag.endswith("-orig") else 0
        return (exact, orig, len(tag))

    candidates.sort(key=rank)
    return candidates[0]


class _ErrorCapture:
    """Passed to yt-dlp as a logger. With ignoreerrors=True (required so one
    bad video doesn't abort an entire run), extract_info() swallows the real
    failure reason and just returns None or {} — the caller's except block
    never sees it, so a private, deleted, or age-restricted video was
    silently getting misclassified downstream as a working video that just
    happened to have no captions. This captures the actual message yt-dlp
    reported so fetch_captions can re-raise it, and the caller's existing
    bot-check vs unavailable vs generic-failure classification stays a
    single code path instead of a second one guessing at "unavailable" for
    every empty result, including ones that were actually a bot check."""
    def __init__(self):
        self.last_error = None

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.last_error = msg

    def error(self, msg):
        self.last_error = msg


def fetch_captions(
    video_id: str,
    url: str,
    workdir: Path,
    lang: str = "en",
    auto: bool = True,
    browser: Optional[str] = None,
    pause: float = 0.6,
) -> dict:
    """Fetch metadata + write caption files for one video in a single
    request (extract_info(download=True) with skip_download=True writes
    subs and returns metadata; ydl.download() alone throws metadata away).

    Raises with yt-dlp's actual error text if extraction failed (private,
    deleted, age-restricted, bot check, etc.) rather than returning an empty
    dict silently — see _ErrorCapture for why that distinction matters."""
    capture = _ErrorCapture()
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": auto,
        "subtitlesformat": "vtt/best",
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "ignoreerrors": True,
        "retries": 3,
        "extractor_retries": 2,
        "sleep_interval_requests": pause,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": capture,
    }
    opts["subtitleslangs"] = ["all", "-live_chat"] if lang == "all" else [lang, f"{lang}.*"]
    if browser:
        opts["cookiesfrombrowser"] = (browser,)

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    if not info:
        raise RuntimeError(capture.last_error or "yt-dlp returned no data for this video")
    return info
