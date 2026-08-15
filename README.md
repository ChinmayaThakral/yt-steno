# yt-steno

A local tool that takes a YouTube channel (or playlist, or single video) and
pulls every available transcript, cleaned of caption junk, packaged into
chunks sized to paste straight into an AI chat.

Runs entirely on your machine. No accounts, no cloud, nothing installed
system-wide beyond a Python virtualenv.

## Install & run

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py
```

Open `http://127.0.0.1:5000`.

Try it without touching YouTube first:

```bash
./venv/bin/python app.py --demo
```

This seeds one run from three short local fixture transcripts so you can see
the whole UI — cue log, transcripts table, bundles, search — with zero
network calls.

Run the test suite (no network required):

```bash
./venv/bin/pytest
```

## Using it

1. Paste a channel URL (`youtube.com/@name`), a playlist, or a single video
   link into the box at the top.
2. Set a video limit if you don't want the whole channel — leave it at `0`
   for everything.
3. Hit **Fetch transcripts**. Progress streams into the cue log as it goes.
4. When it's done: the **Transcripts** tab lists every video with its status
   (a video with no captions still shows up, with a reason, rather than
   silently vanishing). The **Bundles** tab has the packaged output — hit
   **Copy** and paste directly into a chat with your AI. **Search** finds a
   phrase across everything fetched and links back to the exact second in
   the original video.

## Options, and what they cost

- **Language** — caption language code (`en`, `es`, ...). `all` grabs every
  language track YouTube has, which is a lot more data.
- **Auto-captions** — include YouTube's machine-generated captions, not just
  creator-uploaded ones. Almost every channel needs this on; manually
  authored captions are rare.
- **Include shorts** — Shorts tend to be low-signal for transcript analysis;
  turn this off to skip that tab entirely.
- **Video limit** — on a **channel**, this is in practice "newest N
  uploads," not "newest N items across the channel." `0` fetches
  everything, which for a large channel can mean hours of runtime and
  millions of words. Under the hood, the limit caps each of YouTube's
  Videos/Live/Shorts tabs independently during enumeration, and the Videos
  tab is usually the biggest, so a low limit can fill up on regular uploads
  before the walk ever reaches Live or Shorts — meaning a low limit may
  return zero Shorts even with that toggle on. This is what most people
  want anyway (the N most recent uploads), but it's not literally "the N
  most recent items YouTube has for this channel." On a **playlist**, this
  caveat doesn't apply — a playlist has no tabs, so the limit caps that one
  list directly and `limit=5` means exactly the first 5 items in the
  playlist's own order, no ambiguity.
- **Parallel workers** (default 3) — more workers means faster fetching and
  a higher chance of tripping YouTube's bot check. If you see failures
  mentioning sign-in, drop this back down.
- **Pause between requests** (default 0.6s) — raise this if YouTube starts
  blocking you mid-run.
- **Cookies from browser** — if YouTube asks you to sign in to confirm
  you're not a bot, point this at a browser where you're logged into
  YouTube. yt-dlp reads the session cookie from there; nothing is typed or
  stored by this app.
- **Bundle size** — how many characters (roughly 4 per token) go into each
  packaged file. Bigger bundles mean fewer files to paste but each one eats
  more of your AI's context window.
- **Keep timestamps in bundles** — swaps the bundled text from clean prose
  to `[HH:MM:SS]`-tagged lines, so the model can cite exact moments. Costs
  more characters per word of actual content.

A large channel (hundreds of videos) easily produces several million words —
far more than any single AI conversation can hold. That's what bundling is
for: work through bundles a few at a time, or use Search first to find the
handful of videos actually relevant to what you're asking, and only paste
those.

## Troubleshooting

- **Fetches suddenly failing with a confusing error, or "signature
  extraction" type messages** — YouTube changes its site regularly and
  breaks extractors; yt-dlp ships fixes within days, often faster than a
  pinned version can catch up. This is the first thing to try, before
  anything else:
  ```bash
  ./venv/bin/pip install -U yt-dlp
  ```
- **"Sign in to confirm you're not a bot" partway through a run** — YouTube's
  bot check. Cancel, drop **Parallel workers** to 1, raise **Pause between
  requests** to 2s or more, and/or set **Cookies from browser** to a browser
  where you're logged into YouTube.
- **A video shows `unavailable`** — it's private, deleted, or otherwise
  gone; nothing to fix on your end.
- **A video shows `no-captions`** — it exists and was fetched fine, it just
  has no caption track in the language you asked for. Try `all` for
  **Language** if you want to see what's available.
- **A run shows `failed` after a restart** — the server process died while
  it was running (crash, machine sleep, `Ctrl-C`). Whatever it had already
  fetched is untouched — it's still listed, readable, and searchable. Start
  a new run to pick up where enumeration left off.

## A note on legality

Bulk-downloading from YouTube is against YouTube's Terms of Service,
regardless of the tool used. Transcripts are the creator's copyrighted
expression, not public domain text. Fetching and analyzing them privately —
for your own research, study, or investigation — is a materially different
thing from republishing or redistributing them. This tool does the former;
what you do with the output is on you.
