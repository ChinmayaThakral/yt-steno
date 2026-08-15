import argparse
import json
import re
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

import transcripts as T

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
RUNS_DIR = DATA_DIR / "runs"
FIXTURES_DIR = BASE_DIR / "fixtures"
DATA_DIR.mkdir(exist_ok=True)
RUNS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
store = T.Store(DATA_DIR / "steno.db")

# In-memory live state for runs currently in progress: run_id -> dict
RUNS: dict[str, dict] = {}
RUNS_LOCK = threading.Lock()

UNAVAILABLE_RE = re.compile(
    r"private video|video unavailable|has been removed|members-only|been terminated|no longer available",
    re.I,
)


def _mark_stale_runs_failed():
    """A run left 'running' means the process died mid-run. On restart there
    is no thread to resume it, so it's a failed run — but whatever transcripts
    it already fetched are untouched and stay searchable."""
    for run in store.list_runs():
        if run["status"] == "running":
            store.update_run(run["id"], status="failed", finished=time.time(),
                              error="Server restarted while this run was in progress.")


def _log(run_id: str, kind: str, text: str):
    with RUNS_LOCK:
        live = RUNS.get(run_id)
        if not live:
            return
        elapsed = time.time() - live["start"]
        live["log"].append({"at": round(elapsed, 3), "text": text, "kind": kind})
        live["log"] = live["log"][-400:]


def _run_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id


def process_one_video(run_id: str, v: dict, options: dict, cancel_event: threading.Event, docs: dict, docs_lock: threading.Lock):
    vid = v["id"]
    if cancel_event.is_set():
        store.upsert_video(run_id, vid, title=v["title"], uploaded=v.get("upload_date") or "",
                            duration=v.get("duration") or 0, status="cancelled", words=0, chars=0,
                            note="run cancelled before this video started", position=v["position"])
        return

    store.upsert_video(run_id, vid, title=v["title"], uploaded=v.get("upload_date") or "",
                        duration=v.get("duration") or 0, status="fetching", words=0, chars=0,
                        note="", position=v["position"])

    run_dir = _run_dir(run_id)
    workdir = run_dir / "cap"
    workdir.mkdir(parents=True, exist_ok=True)

    def finish(status, note="", words=0, chars=0):
        store.upsert_video(run_id, vid, title=v["title"], uploaded=v.get("upload_date") or "",
                            duration=v.get("duration") or 0, status=status, words=words, chars=chars,
                            note=note, position=v["position"])
        for f in workdir.glob(f"{vid}*"):
            f.unlink(missing_ok=True)

    try:
        # fetch_captions raises with yt-dlp's real error text if extraction
        # failed (private, deleted, age-restricted, bot check, ...) instead
        # of returning empty — see transcripts._ErrorCapture. That keeps
        # every failure reason running through the one classification below,
        # rather than a second branch here that would have to guess.
        T.fetch_captions(vid, v["url"], workdir, lang=options["lang"], auto=options["auto"],
                          browser=options.get("browser"), pause=options["pause"])

        vtt_path = T.choose_best_vtt(workdir, vid, options["lang"])
        if vtt_path is None:
            finish("no-captions", "no captions published in the requested language")
            _log(run_id, "warn", f"no captions — {v['title']}")
            return

        raw = vtt_path.read_text(encoding="utf-8", errors="replace")
        lines = T.parse_vtt(raw)
        if len(lines) < 3:
            finish("empty", "caption track held no speech")
            _log(run_id, "warn", f"caption track empty — {v['title']}")
            return

        prose = T.to_prose(lines)
        timed = T.to_timed(lines)
        passages = T.to_passages(lines, vid, run_id, v["title"])
        store.add_passages(passages)

        slug = T.slugify(v["title"], vid)
        txt_dir = run_dir / "txt"
        txt_dir.mkdir(parents=True, exist_ok=True)
        (txt_dir / f"{slug}.txt").write_text(prose, encoding="utf-8")
        (txt_dir / f"{slug}.timed.txt").write_text(timed, encoding="utf-8")

        with docs_lock:
            docs[vid] = {
                "title": v["title"],
                "video_id": vid,
                "upload_date": v.get("upload_date") or "unknown",
                "text": timed if options["timestamps"] else prose,
            }

        finish("ok", "", words=len(prose.split()), chars=len(prose))
        _log(run_id, "ok", f"captioned — {v['title']}")

    except Exception as e:
        msg = str(e)
        if T.is_bot_check_error(msg):
            finish("failed", "YouTube asked for sign-in. Raise the pause, or pick a browser to read cookies from.")
            _log(run_id, "error", f"blocked by YouTube — raise the pause or add cookies ({v['title']})")
        elif UNAVAILABLE_RE.search(msg):
            finish("unavailable", "video is private, deleted, or members-only")
            _log(run_id, "warn", f"unavailable — {v['title']}")
        else:
            finish("failed", msg[:200])
            _log(run_id, "error", f"failed — {v['title']}: {msg[:120]}")

    with RUNS_LOCK:
        live = RUNS.get(run_id)
        if live:
            live["done"] += 1


def run_worker(run_id: str, url: str, options: dict):
    with RUNS_LOCK:
        RUNS[run_id] = {
            "log": [], "stage": "reading channel", "done": 0, "total": 0,
            "start": time.time(), "cancel_event": threading.Event(),
        }
    cancel_event = RUNS[run_id]["cancel_event"]
    run_dir = _run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    _log(run_id, "info", f"reading {url}")
    try:
        videos, source = T.enumerate_channel(url, limit=options["limit"], include_shorts=options["shorts"])
    except Exception as e:
        store.update_run(run_id, status="failed", finished=time.time(), error=str(e)[:300])
        _log(run_id, "error", f"could not read that URL: {e}")
        return

    if not videos:
        store.update_run(run_id, status="failed", finished=time.time(),
                          error="No videos found at that URL.")
        _log(run_id, "error", "no videos found at that URL")
        return

    for i, v in enumerate(videos):
        v["position"] = i
    store.update_run(run_id, source=source)
    with RUNS_LOCK:
        RUNS[run_id]["total"] = len(videos)
        RUNS[run_id]["stage"] = "fetching captions"
    _log(run_id, "info", f"{len(videos)} videos found on {source}")

    docs: dict[str, dict] = {}
    docs_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=options["workers"]) as ex:
        futures = [ex.submit(process_one_video, run_id, v, options, cancel_event, docs, docs_lock) for v in videos]
        for f in futures:
            f.result()

    with RUNS_LOCK:
        RUNS[run_id]["stage"] = "packing bundles"
    _log(run_id, "info", "packing transcripts into bundles")

    video_rows = store.list_videos(run_id)
    documents = [docs[row["video_id"]] for row in video_rows if row["video_id"] in docs]
    bundles = T.pack_bundles(documents, source=source, budget_chars=options["bundle_chars"])

    bundles_dir = run_dir / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_meta = []
    for b in bundles:
        path = bundles_dir / f"bundle-{b['index']:02d}.txt"
        path.write_text(b["text"], encoding="utf-8")
        bundle_meta.append({"n": b["index"], "videos": b["videos"], "chars": b["chars"], "tokens": b["tokens"]})

    shutil.rmtree(run_dir / "cap", ignore_errors=True)

    captioned = sum(1 for r in video_rows if r["status"] == "ok")
    stats = {
        "videos": len(video_rows),
        "captioned": captioned,
        "words": sum(r["words"] or 0 for r in video_rows),
        "chars": sum(r["chars"] or 0 for r in video_rows),
        "tokens": sum((r["chars"] or 0) for r in video_rows) // 4,
        "bundles": bundle_meta,
        "skipped": len(video_rows) - captioned,
    }
    final_status = "cancelled" if cancel_event.is_set() else "done"
    store.update_run(run_id, status=final_status, finished=time.time(), stats=json.dumps(stats))
    _log(run_id, "ok", f"done — {captioned} of {len(video_rows)} videos captioned, {len(bundle_meta)} bundle(s)")


def build_zip(run_id: str) -> Path:
    run_dir = _run_dir(run_id)
    run = store.get_run(run_id)
    source = (run["source"] or run_id) if run else run_id
    safe_source = T._ILLEGAL_FS_CHARS.sub("_", source)[:60] or run_id
    zip_path = run_dir / f"steno-{safe_source}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for sub in ("txt", "bundles"):
            for f in (run_dir / sub).glob("*.txt"):
                zf.write(f, arcname=f"{sub}/{f.name}")
    return zip_path


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/runs", methods=["POST"])
def create_run():
    body = request.get_json(force=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Paste a channel, playlist, or video URL first."}), 400

    options = {
        "lang": (body.get("lang") or "en").strip(),
        "auto": bool(body.get("auto", True)),
        "shorts": bool(body.get("shorts", True)),
        "limit": max(0, int(body.get("limit") or 0)),
        "workers": min(8, max(1, int(body.get("workers") or 3))),
        "pause": max(0.0, min(5.0, float(body.get("pause") if body.get("pause") is not None else 0.6))),
        "browser": body.get("browser") or None,
        "bundle_chars": max(20_000, int(body.get("bundle_chars") or 300_000)),
        "timestamps": bool(body.get("timestamps", False)),
    }

    run_id = uuid.uuid4().hex[:12]
    store.create_run(run_id, url, options)
    thread = threading.Thread(target=run_worker, args=(run_id, url, options), daemon=True)
    thread.start()
    return jsonify({"run_id": run_id})


@app.route("/api/runs", methods=["GET"])
def list_runs():
    runs = store.list_runs()
    out = []
    for r in runs:
        stats = json.loads(r["stats"] or "{}")
        out.append({
            "run_id": r["id"], "url": r["url"], "source": r["source"], "status": r["status"],
            "created": r["created"], "stats": stats,
        })
    return jsonify(out)


@app.route("/api/runs/<run_id>", methods=["GET"])
def get_run(run_id):
    run = store.get_run(run_id)
    if not run:
        return jsonify({"error": "That run doesn't exist."}), 404

    videos = store.list_videos(run_id)
    with RUNS_LOCK:
        live = RUNS.get(run_id)
        live_snapshot = dict(live) if live else None
    stats = json.loads(run["stats"] or "{}")

    if run["status"] == "running" and live_snapshot:
        done, total = live_snapshot["done"], live_snapshot["total"]
        stage = live_snapshot["stage"]
        elapsed = time.time() - live_snapshot["start"]
        log = live_snapshot["log"]
        bundles = []
    else:
        done = stats.get("captioned", len(videos))
        total = stats.get("videos", len(videos))
        stage = run["status"]
        elapsed = (run["finished"] or time.time()) - run["created"]
        log = live_snapshot["log"] if live_snapshot else []
        bundles = stats.get("bundles", [])

    return jsonify({
        "run_id": run_id, "status": run["status"], "stage": stage,
        "source": run["source"], "url": run["url"],
        "done": done, "total": total, "elapsed": round(elapsed, 1),
        "log": log,
        "videos": [{
            "video_id": v["video_id"], "title": v["title"], "uploaded": v["uploaded"],
            "duration": v["duration"], "status": v["status"], "words": v["words"],
            "chars": v["chars"], "note": v["note"],
        } for v in videos],
        "bundles": bundles,
        "stats": stats,
        "error": run["error"] or "",
    })


@app.route("/api/runs/<run_id>/cancel", methods=["POST"])
def cancel_run(run_id):
    with RUNS_LOCK:
        live = RUNS.get(run_id)
    if not live:
        return jsonify({"error": "That run isn't active."}), 404
    live["cancel_event"].set()
    _log(run_id, "warn", "cancelling — finishing videos already in flight")
    return jsonify({"ok": True})


@app.route("/api/runs/<run_id>", methods=["DELETE"])
def delete_run(run_id):
    store.delete_run(run_id)
    with RUNS_LOCK:
        RUNS.pop(run_id, None)
    shutil.rmtree(_run_dir(run_id), ignore_errors=True)
    return jsonify({"ok": True})


@app.route("/api/runs/<run_id>/video/<video_id>", methods=["GET"])
def get_video(run_id, video_id):
    fmt = request.args.get("format", "prose")
    videos = {v["video_id"]: v for v in store.list_videos(run_id)}
    row = videos.get(video_id)
    if not row:
        return jsonify({"error": "That video isn't part of this run."}), 404
    slug = T.slugify(row["title"], video_id)
    suffix = ".timed.txt" if fmt == "timed" else ".txt"
    path = _run_dir(run_id) / "txt" / f"{slug}{suffix}"
    if not path.exists():
        return jsonify({"error": "No transcript stored for this video."}), 404
    return app.response_class(path.read_text(encoding="utf-8"), mimetype="text/plain; charset=utf-8")


@app.route("/api/runs/<run_id>/bundle/<int:n>", methods=["GET"])
def get_bundle(run_id, n):
    path = _run_dir(run_id) / "bundles" / f"bundle-{n:02d}.txt"
    if not path.exists():
        return jsonify({"error": "Not found."}), 404
    return app.response_class(path.read_text(encoding="utf-8"), mimetype="text/plain; charset=utf-8")


@app.route("/api/runs/<run_id>/zip", methods=["GET"])
def get_zip(run_id):
    run = store.get_run(run_id)
    if not run:
        return jsonify({"error": "That run doesn't exist."}), 404
    zip_path = build_zip(run_id)
    return send_file(zip_path, as_attachment=True, download_name=zip_path.name)


@app.route("/api/search", methods=["GET"])
def search():
    q = request.args.get("q", "")
    run_id = request.args.get("run_id") or None
    results = store.search(q, run_id=run_id, limit=50)
    return jsonify(results)


# ---------------------------------------------------------------------------
# Demo seeding (--demo flag): exercises the full pipeline with no network.
# ---------------------------------------------------------------------------

def seed_demo_run():
    run_id = "demo"
    if store.get_run(run_id):
        return run_id

    store.create_run(run_id, "demo://local-fixtures", {"lang": "en", "auto": True})
    store.update_run(run_id, source="Steno Demo Channel")
    run_dir = _run_dir(run_id)
    (run_dir / "txt").mkdir(parents=True, exist_ok=True)
    (run_dir / "bundles").mkdir(parents=True, exist_ok=True)

    fixtures = sorted(FIXTURES_DIR.glob("demo*.vtt"))
    titles = {
        "demo1.vtt": "Why Compounding Feels Slow At First",
        "demo2.vtt": "A Field Guide to Sourdough Starters",
        "demo3.vtt": "The Fall of the Library at Alexandria, in Five Minutes",
    }
    RUNS[run_id] = {"log": [], "stage": "done", "done": 0, "total": len(fixtures), "start": time.time(),
                     "cancel_event": threading.Event()}
    _log(run_id, "info", f"reading demo://local-fixtures")
    _log(run_id, "info", f"{len(fixtures)} videos found on Steno Demo Channel")

    docs = {}
    for i, fpath in enumerate(fixtures):
        vid = f"demo{i+1:03d}"
        title = titles.get(fpath.name, fpath.stem)
        raw = fpath.read_text(encoding="utf-8")
        lines = T.parse_vtt(raw)
        prose = T.to_prose(lines)
        timed = T.to_timed(lines)
        passages = T.to_passages(lines, vid, run_id, title)
        store.add_passages(passages)

        slug = T.slugify(title, vid)
        (run_dir / "txt" / f"{slug}.txt").write_text(prose, encoding="utf-8")
        (run_dir / "txt" / f"{slug}.timed.txt").write_text(timed, encoding="utf-8")

        store.upsert_video(run_id, vid, title=title, uploaded="20240101", duration=68,
                            status="ok", words=len(prose.split()), chars=len(prose), note="", position=i)
        docs[vid] = {"title": title, "video_id": vid, "upload_date": "20240101", "text": prose}
        _log(run_id, "ok", f"captioned — {title}")
        RUNS[run_id]["done"] += 1

    bundles = T.pack_bundles(list(docs.values()), source="Steno Demo Channel", budget_chars=300_000)
    bundle_meta = []
    for b in bundles:
        (run_dir / "bundles" / f"bundle-{b['index']:02d}.txt").write_text(b["text"], encoding="utf-8")
        bundle_meta.append({"n": b["index"], "videos": b["videos"], "chars": b["chars"], "tokens": b["tokens"]})

    stats = {
        "videos": len(fixtures), "captioned": len(fixtures),
        "words": sum(len(d["text"].split()) for d in docs.values()),
        "chars": sum(len(d["text"]) for d in docs.values()),
        "tokens": sum(len(d["text"]) for d in docs.values()) // 4,
        "bundles": bundle_meta, "skipped": 0,
    }
    store.update_run(run_id, status="done", finished=time.time(), stats=json.dumps(stats))
    _log(run_id, "ok", f"done — {len(fixtures)} of {len(fixtures)} videos captioned, {len(bundle_meta)} bundle(s)")
    return run_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="seed a demo run from local fixtures, no network needed")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    _mark_stale_runs_failed()
    if args.demo:
        demo_id = seed_demo_run()
        print(f"\n  Demo run seeded: {demo_id}\n")

    print(f"\n  yt-steno running at http://127.0.0.1:{args.port}\n")
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)
