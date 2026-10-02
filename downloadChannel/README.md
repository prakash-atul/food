# downloadChannel — portable ingredient-card audio downloader

Download the **audio** of YouTube cooking videos whose **description contains an
ingredient card**, and skip everything else. Works with whole **channels** and
**playlists**. Fully resumable. Self-contained: only needs Python + `yt-dlp`,
and a Qwen LLM endpoint reachable over an SSH tunnel.

## What it does per video
1. Fetch the description.
2. Ask the Qwen LLM: *is it a cooking video* **and** *does the description have a
   real ingredients card?* (with an anti-hallucination grounding check). If the
   description is ambiguous about cooking, it double-checks via the ASR transcript.
3. If (card AND cooking): download best audio (original container, no re-encode),
   save the description, and save the parsed ingredients (english + scientific name).

## Output layout
```
output/<channelName>/<video_id>/
    description        # the YouTube description (text)
    ingredients.json   # [{ "english": "...", "scientific": "..." }, ...]  (no quantities)
    audio.<ext>        # best audio (usually audio.webm / audio.m4a)
output/<channelName>/_channel.json          # channel metadata
output/<channelName>/_playlist_<id>.json    # playlist metadata (one per playlist)
```
Resume markers (hidden) tell re-runs to skip a video instantly:
`.no-card`, `.not-cooking`, `.unavailable`. Delete a marker to re-evaluate that video.

## Setup (on the other PC)
```bash
pip install -U yt-dlp
# (ffmpeg only needed if you use --audio-format mp3/wav/etc; default 'best' doesn't need it)
```

## 1) Open the SSH tunnel to the LLM — keep it running
The LLM runs on the HPC. On THIS pc, open the tunnel in its own terminal:
```bash
# bash
bash tunnel.sh                 # 27B on :8005 (default)
PORT=8010 bash tunnel.sh       # if a 4B is served on :8010
```
```powershell
# Windows PowerShell
powershell -ExecutionPolicy Bypass -File tunnel.ps1
powershell -ExecutionPolicy Bypass -File tunnel.ps1 -Port 8010
```
Or do it manually (no auto-reconnect):
```bash
ssh -L 8005:localhost:8005 atul_prakash@10.1.7.58
```
The tunnel scripts auto-reconnect within ~3s if the network blips (it sometimes does).

## 2) Put your links in list.txt
One channel or playlist URL per line (see the samples in `list.txt`):
```
https://www.youtube.com/@kashmirfoodfusion/videos
https://www.youtube.com/playlist?list=PLxxxxxxxxxxxx
```

## 3) Run
```bash
python app.py                      # reads ./list.txt + ./cookies.txt -> ./output/
# or the convenience runners (set a sensible endpoint/env for you):
bash run.sh
powershell -ExecutionPolicy Bypass -File run.ps1
```
Handy flags:
```
python app.py --limit 5                         # quick test: 5 new videos per source
python app.py "https://youtube.com/@someChannel/videos"   # ad-hoc source, ignore list.txt
python app.py --folder-name kashmir             # funnel ALL sources into output/kashmir/
python app.py --per-source 100 --target-total 200   # caps + cross-source compensation
python app.py --audio-format mp3                # re-encode (needs ffmpeg)
```

## Endpoint config (env vars)
| Var | Default | Meaning |
|-----|---------|---------|
| `VLLM_BASE_URL` | `http://localhost:8005/v1` | OpenAI-compatible Qwen endpoint (via tunnel) |
| `MODEL_NAME` | `qwen` | served model name |
| `REQUEST_TIMEOUT` | `120` | seconds per LLM call (keeps a dead tunnel from hanging) |
| `RETRIES` | `4` | LLM retries per call |

```bash
# example: point at a 4B served on 8010 instead
VLLM_BASE_URL=http://localhost:8010/v1 python app.py
```

## Notes / resilience
- **Network drops:** failed videos are left *unmarked*, so just re-run the same
  command and it retries only what's left (already-downloaded videos skip instantly).
- **YouTube rate-limit:** the run aborts cleanly without marking good videos
  unavailable. Wait ~1 hour (or refresh `cookies.txt` / change IP) and re-run.
- **cookies.txt** (Netscape format) greatly reduces rate-limiting. Export it from a
  logged-in browser (e.g. the "Get cookies.txt" extension) if the bundled one expires.
- **Parallel PCs:** safe — each PC writes to its own `output/`. Use `--folder-name`
  if you want a fixed top folder regardless of source.
