#!/usr/bin/env python3
"""Standalone, portable YouTube ingredient-card audio downloader.

Point it at YouTube CHANNEL or PLAYLIST urls (one per line in list.txt). For each
video it:
  1. fetches the DESCRIPTION,
  2. asks a Qwen LLM (OpenAI-compatible endpoint) two things: is this a COOKING
     video, and does the description contain a genuine INGREDIENTS card,
  3. only if it has a real card AND is a cooking video, downloads the best audio
     (original container, no re-encode) + saves the description + the parsed
     ingredient list (english + scientific name only).
When the description is ambiguous about cooking it corroborates via the ASR
transcript (disable with --no-asr).

OUTPUT (per video):
    output/<channelName>/<video_id>/
        description        the YouTube description (text)
        ingredients.json   parsed card: [{english, scientific}, ...]
        audio.<ext>        best audio (webm/opus or m4a), no re-encode
        .no-card / .not-cooking / .unavailable   resume markers (skipped on re-run)
    output/<channelName>/_channel.json | _playlist_<id>.json   source metadata

This is fully RESUMABLE: re-run the same command and it skips anything already
done (audio present, or a marker) and retries only what's left. A YouTube
rate-limit aborts cleanly without mis-marking good videos.

------------------------------------------------------------------------------
THE LLM lives on the HPC. Open an SSH tunnel on THIS pc first (keep it running):

    ssh -L 8005:localhost:8005 atul_prakash@10.1.7.58          # 27B  (default)
    # or, if a 4B is served on 8010:
    # ssh -L 8010:localhost:8010 atul_prakash@10.1.7.58

Then just run (reads ./list.txt, ./cookies.txt, writes ./output/):

    python app.py
    python app.py --limit 5                 # quick test: 5 videos per source
    python app.py "https://www.youtube.com/@kashmirfoodfusion/videos"

Override the endpoint if needed:
    (bash)        VLLM_BASE_URL=http://localhost:8010/v1 python app.py
    (powershell)  $env:VLLM_BASE_URL="http://localhost:8010/v1"; python app.py

Requirements:  pip install -U yt-dlp   (ffmpeg optional, only for re-encoding)
------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows consoles default to cp1252 and crash on native-script titles/logs.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except ImportError:
    sys.exit("yt-dlp is not installed. Install it with:  pip install -U yt-dlp")

HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- #
# config (env-overridable)
# --------------------------------------------------------------------------- #
VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8005/v1").rstrip("/")
MODEL_NAME = os.environ.get("MODEL_NAME", "qwen")
API_KEY = os.environ.get("VLLM_API_KEY", "EMPTY")
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "120"))
RETRIES = int(os.environ.get("RETRIES", "4"))
DISABLE_THINKING = os.environ.get("DISABLE_THINKING", "1") not in ("0", "false", "False")

FFMPEG = shutil.which("ffmpeg")
AUDIO_SUFFIXES = {".webm", ".m4a", ".opus", ".mp3", ".aac", ".ogg", ".wav", ".flac", ".mp4"}

UNAVAILABLE_PATTERNS = re.compile(
    r"Video unavailable|video is unavailable|removed by the uploader|no longer available"
    r"|account associated with this video has been terminated"
    r"|violating YouTube'?s? (Community Guidelines|Terms)"
    r"|This video is private|Private video|This video has been removed"
    r"|This video is (not|no longer) available|members-only|join this channel"
    r"|is not available in your country|blocked it|not available on this app"
    r"|Incomplete YouTube ID|does not exist", re.IGNORECASE)
# NOTE: YouTube's bot-check uses a CURLY apostrophe ("you’re"), and when a session
# is rate-limited it also returns bogus "Video unavailable" for good videos. Match
# the bot-check robustly (apostrophe-agnostic) so we ABORT instead of mis-marking.
RATE_LIMIT_PATTERNS = re.compile(
    r"rate-limited|isn['’]?t available, try again later"
    r"|Sign in to confirm|not a bot|429|Too Many Requests", re.IGNORECASE)

# --------------------------------------------------------------------------- #
# LLM gate contract
# --------------------------------------------------------------------------- #
SCHEMA = {
    "type": "object",
    "properties": {
        "is_cooking": {"type": "boolean"},
        "has_card": {"type": "boolean"},
        "dish_title": {"type": "string"},
        "ingredients": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"english": {"type": "string"}, "scientific": {"type": "string"}},
                "required": ["english", "scientific"], "additionalProperties": False,
            },
        },
    },
    "required": ["is_cooking", "has_card", "dish_title", "ingredients"],
    "additionalProperties": False,
}
SYSTEM = (
    "You analyse a YouTube video description. Do two things and return JSON only.\n"
    "1) is_cooking: true if this is a COOKING / RECIPE video (preparing a food "
    "dish, with ingredients and/or cooking steps); false for anything else "
    "(vlog, travel, announcement, review, music, unboxing, etc.).\n"
    "2) Extract the ingredients card. Many cooking descriptions contain an "
    "explicit ingredients card (often under a heading like 'Ingredients:', "
    "sometimes with sub-sections like 'For Stuffing:'). Return ONLY the food "
    "ingredients from that card. For EACH ingredient output an object with:\n"
    "   - english: the common English name, lowercase, singular. NOTHING ELSE - "
    "no quantities, no units, no numbers, no brand names, no parenthetical "
    "translations, no preparation words (e.g. 'chopped', 'roasted').\n"
    "   - scientific: its scientific (Latin binomial / botanical) name if one "
    "applies (e.g. 'Allium cepa' for onion, 'Curcuma longa' for turmeric); use an "
    "empty string \"\" for non-biological items like water or salt.\n"
    "A description often contains BOTH an ingredients list AND a numbered "
    "method/steps section - extract ONLY the items from the ingredients card, "
    "never anything mentioned only in the steps. Exclude method steps, promo "
    "text, links, and staff credits. If there is no ingredients card, set "
    "has_card=false and return an empty ingredients list. Output JSON only."
)
ASR_SCHEMA = {
    "type": "object",
    "properties": {"is_cooking": {"type": "boolean"}},
    "required": ["is_cooking"], "additionalProperties": False,
}
ASR_SYSTEM = (
    "You are given the auto-generated transcript (ASR) of a YouTube video, which "
    "may be in any language. Decide whether it is a COOKING / RECIPE video: "
    "someone preparing a food dish, mentioning ingredients and/or cooking steps. "
    "Return JSON {\"is_cooking\": true|false} only."
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?")


def parse_json_loose(text: str):
    if not text:
        return None
    t = _THINK_RE.sub("", text).strip()
    if t.startswith("```"):
        t = _FENCE_RE.sub("", t).rstrip("`").strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(t[s:e + 1])
        except Exception:
            return None
    return None


def llm_json(system: str, user: str, schema: dict, max_tokens: int):
    """Call the OpenAI-compatible endpoint, enforcing a JSON schema. Returns a
    dict, or None on repeated failure. Pure stdlib (urllib) — no SDK needed."""
    body = {
        "model": MODEL_NAME,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.0, "seed": 42, "max_tokens": max_tokens,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "extraction", "schema": schema, "strict": True}},
    }
    if DISABLE_THINKING:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    data = json.dumps(body).encode("utf-8")
    url = VLLM_BASE_URL + "/chat/completions"
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {API_KEY}",
            })
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                payload = json.loads(r.read().decode("utf-8"))
            content = payload["choices"][0]["message"]["content"] or ""
            parsed = parse_json_loose(content)
            if parsed is not None:
                return parsed
        except Exception:  # noqa: BLE001 - HTTP/transport/parse errors
            pass
        if attempt < RETRIES - 1:
            time.sleep(1.5 * (attempt + 1))
    return None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def sanitize(name: str) -> str:
    name = (name or "").strip().strip("@")
    name = re.sub(r'[<>:"/\\|?*]+', "_", name)
    name = re.sub(r"\s+", "_", name).strip("._")
    return name or "channel"


def is_playlist_url(url: str) -> bool:
    return "list=" in url


def normalize_source_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if "list=" in url:                       # playlist / mix -> use as-is
        return url
    if not url.startswith("http"):
        url = f"https://www.youtube.com/@{url.lstrip('@')}"
    if not re.search(r"/(videos|streams|shorts|playlists)$", url):
        url = url + "/videos"
    return url


def handle_from_url(url: str):
    m = re.search(r"youtube\.com/@([^/]+)", url)
    return sanitize(m.group(1)) if m else None


def has_audio(folder: Path) -> bool:
    return any(p.is_file() and p.stem == "audio" and p.suffix.lower() in AUDIO_SUFFIXES
               for p in folder.glob("audio.*"))


def has_card(parsed: dict, description: str):
    """Keep only a REAL card: has_card AND >=3 ingredients AND enough of them
    grounded in the description text (anti-hallucination). Grounding uses the
    english name. Returns (ok, [{english, scientific}, ...])."""
    ings = []
    for x in (parsed.get("ingredients") or []):
        if isinstance(x, dict):
            eng = str(x.get("english", "")).strip().lower()
            sci = str(x.get("scientific", "")).strip()
        else:
            eng, sci = str(x).strip().lower(), ""
        if eng:
            ings.append({"english": eng, "scientific": sci})
    tl = description.lower()
    grounded = sum(1 for g in ings
                   if any(len(tok) > 2 and tok in tl for tok in g["english"].split()))
    ok = bool(parsed.get("has_card")) and len(ings) >= 3 and grounded >= max(3, (len(ings) + 1) // 2)
    return ok, ings


# --------------------------------------------------------------------------- #
# yt-dlp
# --------------------------------------------------------------------------- #
def _base(cookies):
    o = {"retries": 10, "fragment_retries": 10, "sleep_interval_requests": 1,
         "sleep_interval": 3, "max_sleep_interval": 8, "ignoreerrors": False,
         "noprogress": True, "quiet": True, "no_warnings": True, "consoletitle": False}
    if cookies:
        o["cookiefile"] = cookies
    return o


def list_source(url, cookies):
    o = _base(cookies)
    o["extract_flat"] = "in_playlist"
    o["skip_download"] = True
    try:
        with YoutubeDL(o) as ydl:
            return ydl.extract_info(url, download=False), ""
    except DownloadError as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def probe_video(video_id, cookies):
    o = _base(cookies)
    o["skip_download"] = True
    try:
        with YoutubeDL(o) as ydl:
            return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False), ""
    except DownloadError as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _wanted_sub_langs(info):
    manual = list((info.get("subtitles") or {}).keys())
    auto = info.get("automatic_captions") or {}
    orig = [k for k in auto if k.endswith("-orig")]
    if not orig:
        lang = info.get("language")
        if lang and lang in auto:
            orig = [lang]
    seen, out = set(), []
    for k in manual + orig:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def fetch_asr_text(video_id, info, cookies):
    """Download the original ASR transcript to a temp dir and return plain text
    (subtitle files are transient, never kept). Returns (text, err)."""
    langs = _wanted_sub_langs(info)
    if not langs:
        return "", "no ASR track"
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix=f"asr_{video_id}_"))
    try:
        o = _base(cookies)
        o["outtmpl"] = {"default": str(tmp / "s.%(ext)s")}
        o["skip_download"] = True
        o["writesubtitles"] = True
        o["writeautomaticsub"] = True
        o["subtitleslangs"] = langs
        o["sleep_interval_subtitles"] = 2
        err = ""
        try:
            with YoutubeDL(o) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        except Exception as e:  # noqa: BLE001
            err = str(e)
        files = list(tmp.glob("*.vtt")) + list(tmp.glob("*.srt"))
        if not files:
            return "", err or "no subs"
        raw = files[0].read_text(encoding="utf-8", errors="ignore")
        lines, prev = [], None
        for ln in raw.splitlines():
            s = ln.strip()
            if not s or s == "WEBVTT" or "-->" in s or s.isdigit():
                continue
            if s.startswith(("Kind:", "Language:", "NOTE")):
                continue
            s = re.sub(r"<[^>]+>", "", s)
            if s != prev:
                lines.append(s)
            prev = s
        return " ".join(lines), err
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def download_audio(target, video_id, audio_format, cookies):
    o = _base(cookies)
    o["outtmpl"] = {"default": str(target / "audio.%(ext)s")}
    o["format"] = "bestaudio/best"
    o["writesubtitles"] = False
    o["writeautomaticsub"] = False
    if audio_format not in ("best", "native", "source"):
        if not FFMPEG:
            return "ffmpeg not found on PATH; cannot re-encode"
        o["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                "preferredcodec": audio_format, "preferredquality": "0"}]
    try:
        with YoutubeDL(o) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
        return ""
    except DownloadError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return str(e)


# --------------------------------------------------------------------------- #
# crawl
# --------------------------------------------------------------------------- #
def enumerate_source(url, out_root, cookies, force_name=None):
    norm = normalize_source_url(url)
    print(f"\n=== source: {url}\n    -> {norm}", flush=True)
    info, err = list_source(norm, cookies)
    if info is None or not info.get("entries"):
        if err and RATE_LIMIT_PATTERNS.search(err):
            print("  ~ RATE-LIMITED while listing. Aborting.", flush=True)
            return {"abort": True}
        print(f"  ! could not verify/list (skipping): {err[:200] or 'no videos'}", flush=True)
        return None
    playlist = is_playlist_url(norm)
    if force_name:
        ch_name = sanitize(force_name)[:80]
    elif playlist:
        ch_name = sanitize(info.get("title") or info.get("id") or "playlist")[:80]
    else:
        ch_name = handle_from_url(norm) or sanitize(
            info.get("uploader_id") or info.get("channel") or info.get("title") or info.get("id"))
    entries = [e for e in info["entries"] if e and e.get("id")]
    kind = "playlist" if playlist else "channel"
    print(f"  verified {kind}: '{info.get('title') or info.get('channel')}'  "
          f"({len(entries)} videos)  -> folder: {ch_name}", flush=True)
    ch_dir = out_root / ch_name
    ch_dir.mkdir(parents=True, exist_ok=True)
    meta_name = f"_playlist_{info.get('id')}.json" if playlist else "_channel.json"
    (ch_dir / meta_name).write_text(json.dumps({
        "input_url": url, "resolved_url": norm, "folder": ch_name, "kind": kind,
        "title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "playlist_id": info.get("id") if playlist else None,
        "uploader_id": info.get("uploader_id"), "n_videos_listed": len(entries),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"abort": False, "url": url, "name": ch_name, "dir": ch_dir, "entries": entries,
            "cursor": 0, "exhausted": False, "accepted": 0, "cards": 0, "audio": 0,
            "no_card": 0, "non_cooking": 0, "unavailable": 0, "scanned": 0, "failed": 0}


def crawl(st, audio_format, cookies, no_asr, accept_target, limit):
    """Scan from st['cursor'] until accepted>=accept_target or exhausted.
    Returns False if the whole run must abort (rate-limited)."""
    entries, ch_dir, ch_name = st["entries"], st["dir"], st["name"]
    total = len(entries)
    # Circuit breaker: after this many CONSECUTIVE failures (probe/LLM/unavailable)
    # we assume a rate-limit or dead tunnel and abort cleanly, leaving the rest
    # unmarked for a later retry instead of churning through hundreds.
    fail_abort = int(os.environ.get("FAIL_ABORT", "8"))
    consec = 0
    while st["cursor"] < total and st["accepted"] < accept_target:
        if limit and st["scanned"] >= limit:
            break
        e = entries[st["cursor"]]
        st["cursor"] += 1
        vid = e["id"]
        target = ch_dir / vid
        target.mkdir(parents=True, exist_ok=True)
        if (target / ".unavailable").exists():
            st["unavailable"] += 1
            continue
        if (target / ".no-card").exists():
            st["no_card"] += 1
            continue
        if (target / ".not-cooking").exists():
            st["non_cooking"] += 1
            continue
        if has_audio(target):
            st["accepted"] += 1
            consec = 0
            continue

        st["scanned"] += 1
        print(f"  [{ch_name} {st['cursor']}/{total} | ok {st['accepted']}] "
              f"{vid}  {(e.get('title') or '')[:60]}", flush=True)

        meta, perr = probe_video(vid, cookies)
        if meta is None:
            if RATE_LIMIT_PATTERNS.search(perr):
                print("    ~ RATE-LIMITED on probe. Aborting.", flush=True)
                return False
            if UNAVAILABLE_PATTERNS.search(perr):
                (target / ".unavailable").write_text((perr.strip() or "unavailable")[:500], encoding="utf-8")
                st["unavailable"] += 1
                print("    ! unavailable", flush=True)
            else:
                st["failed"] += 1
                print("    ! probe failed (retry next run)", flush=True)
            consec += 1
            if consec >= fail_abort:
                print(f"    ~ {consec} consecutive failures -> suspected rate-limit/"
                      "endpoint down. Aborting (rest left for retry).", flush=True)
                return False
            continue

        description = (meta.get("description") or "").strip()
        if not description:
            (target / ".no-card").write_text("empty description", encoding="utf-8")
            st["no_card"] += 1
            consec = 0
            print("    - no description -> ignored", flush=True)
            continue

        parsed = llm_json(SYSTEM,
                          f'Video description:\n"""\n{description[:8000]}\n"""\n\n'
                          "Classify is_cooking and extract the ingredients card.",
                          SCHEMA, 1024)
        if parsed is None:
            st["failed"] += 1
            consec += 1
            print("    ! LLM gate failed (retry next run)", flush=True)
            if consec >= fail_abort:
                print(f"    ~ {consec} consecutive failures -> suspected endpoint "
                      "down. Aborting (rest left for retry).", flush=True)
                return False
            continue
        ok, ings = has_card(parsed, description)
        if not ok:
            (target / ".no-card").write_text("no ingredient card", encoding="utf-8")
            st["no_card"] += 1
            consec = 0
            print(f"    - no card ({len(ings)} items) -> ignored", flush=True)
            continue

        cooking = bool(parsed.get("is_cooking"))
        cooking_src = "description"
        if not cooking and not no_asr:
            asr_text, aerr = fetch_asr_text(vid, meta, cookies)
            if aerr and RATE_LIMIT_PATTERNS.search(aerr):
                print("    ~ RATE-LIMITED fetching ASR. Aborting.", flush=True)
                return False
            if asr_text:
                cp = llm_json(ASR_SYSTEM,
                              f'Transcript:\n"""\n{asr_text[:8000]}\n"""\n\nIs this a cooking video?',
                              ASR_SCHEMA, 32)
                if cp is not None:
                    cooking, cooking_src = bool(cp.get("is_cooking")), "asr"
                else:
                    cooking, cooking_src = True, "card-fallback(asr-unparsed)"
            else:
                cooking, cooking_src = True, "card-fallback(no-asr)"
        if not cooking:
            (target / ".not-cooking").write_text("not a cooking video", encoding="utf-8")
            st["non_cooking"] += 1
            consec = 0
            print(f"    - has card but NOT cooking ({cooking_src}) -> ignored", flush=True)
            continue

        (target / "description").write_text(description, encoding="utf-8")
        (target / "ingredients.json").write_text(json.dumps({
            "video_id": vid, "channel": ch_name, "title": meta.get("title"),
            "dish_title": parsed.get("dish_title", ""), "is_cooking": True,
            "cooking_source": cooking_src, "n_ingredients": len(ings), "ingredients": ings,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        st["cards"] += 1
        print(f"    + CARD ({len(ings)} ingredients, cooking:{cooking_src}) -> audio", flush=True)

        aerr = download_audio(target, vid, audio_format, cookies)
        if has_audio(target):
            st["audio"] += 1
            st["accepted"] += 1
            consec = 0
        elif RATE_LIMIT_PATTERNS.search(aerr):
            print("    ~ RATE-LIMITED on audio. Aborting (card kept).", flush=True)
            return False
        elif UNAVAILABLE_PATTERNS.search(aerr):
            (target / ".unavailable").write_text((aerr.strip() or "unavailable")[:500], encoding="utf-8")
            st["unavailable"] += 1
            print("    ! became unavailable during audio", flush=True)
        else:
            st["failed"] += 1
            print(f"    ! audio failed (retry next run): {aerr[:140]}", flush=True)

    if st["cursor"] >= total:
        st["exhausted"] = True
    return True


def ping_llm() -> bool:
    try:
        req = urllib.request.Request(VLLM_BASE_URL + "/models",
                                     headers={"Authorization": f"Bearer {API_KEY}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            json.loads(r.read())
        return True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Download audio for YouTube videos whose "
                                             "description has an ingredient card (LLM-gated).")
    ap.add_argument("sources", nargs="*", help="Channel/playlist URLs (default: read list.txt).")
    ap.add_argument("--list", default=str(HERE / "list.txt"),
                    help="File of channel/playlist URLs, one per line (default ./list.txt).")
    ap.add_argument("--out", default=str(HERE / "output"), help="Output root (default ./output).")
    ap.add_argument("--cookies", default=str(HERE / "cookies.txt"),
                    help="Netscape cookies.txt (default ./cookies.txt if present).")
    ap.add_argument("--audio-format", default="best",
                    help="best/native = no re-encode (default), or mp3|m4a|wav|opus...")
    ap.add_argument("--per-source", type=int, default=100000,
                    help="Max audios per source (default effectively unlimited).")
    ap.add_argument("--target-total", type=int, default=100000,
                    help="Overall target across all sources (compensation).")
    ap.add_argument("--folder-name",
                    help="Force ALL sources into one output folder of this name.")
    ap.add_argument("--limit", type=int, default=0, help="Scan at most N new videos per source.")
    ap.add_argument("--no-asr", action="store_true", help="Skip the ASR cooking check.")
    args = ap.parse_args()

    sources = list(args.sources)
    if not sources and os.path.isfile(args.list):
        sources += [ln.strip() for ln in Path(args.list).read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]
    if not sources:
        sys.exit(f"No sources. Put channel/playlist URLs in {args.list} or pass them as arguments.")

    cookies = args.cookies if (args.cookies and os.path.isfile(args.cookies)) else None
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"LLM endpoint: {VLLM_BASE_URL}  (model '{MODEL_NAME}')", flush=True)
    if not ping_llm():
        m = re.search(r":(\d+)", VLLM_BASE_URL)
        port = m.group(1) if m else "8005"
        sys.exit(f"\nCannot reach the LLM at {VLLM_BASE_URL}.\n"
                 f"Open the SSH tunnel first and keep it running:\n"
                 f"    ssh -L {port}:localhost:{port} atul_prakash@10.1.7.58\n"
                 f"(or set VLLM_BASE_URL to the right host/port).")
    print(f"  endpoint OK. cookies: {cookies or 'none'}  out: {out_root}\n", flush=True)

    per_cap, target_total = args.per_source, args.target_total
    states, aborted, bad = [], False, 0
    for url in sources:
        st = enumerate_source(url, out_root, cookies, args.folder_name)
        if st is None:
            bad += 1
            continue
        if st.get("abort"):
            aborted = True
            break
        states.append(st)

    def accepted_total():
        return sum(s["accepted"] for s in states)

    if not aborted:
        for st in states:
            if not crawl(st, args.audio_format, cookies, args.no_asr, per_cap, args.limit):
                aborted = True
                break

    if not aborted:
        while accepted_total() < target_total and any(not s["exhausted"] for s in states):
            deficit = target_total - accepted_total()
            st = max((s for s in states if not s["exhausted"]),
                     key=lambda s: len(s["entries"]) - s["cursor"], default=None)
            if st is None:
                break
            before = accepted_total()
            print(f"\n--- compensation: need {deficit} more; pulling from {st['name']} ---", flush=True)
            if not crawl(st, args.audio_format, cookies, args.no_asr, st["accepted"] + deficit, args.limit):
                aborted = True
                break
            # guard against spinning: if a pass couldn't add anything (e.g. --limit
            # hit, or no more cards), stop rather than loop forever.
            if accepted_total() <= before:
                break

    print("\n" + "=" * 60 + "\nPER-SOURCE RESULT\n" + "=" * 60)
    grand = 0
    for st in states:
        grand += st["accepted"]
        print(f"\n  {st['name']}  ({st['url']})")
        print(f"    listed: {len(st['entries'])}  scanned: {st['cursor']}"
              f"  ({'exhausted' if st['exhausted'] else 'stopped'})")
        print(f"    AUDIOS with card (downloaded): {st['accepted']}  (new: {st['audio']})")
        print(f"    no card: {st['no_card']}  not cooking: {st['non_cooking']}"
              f"  unavailable: {st['unavailable']}  failed(retry): {st['failed']}")
    print("\n" + "-" * 60)
    print(f"  TOTAL audios with card: {grand}")
    print(f"  sources that could not be listed: {bad}")
    if aborted:
        print("\n  *** RUN ABORTED (YouTube rate-limit). Nothing mis-marked. "
              "Wait ~1h or rotate cookies/IP, then re-run to resume. ***")
    print(f"\nOutput: {out_root}\\<channelName>\\<video_id>\\{{description, ingredients.json, audio.<ext>}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
