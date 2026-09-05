# LocalVidGen

A self-hosted, Higgsfield-style prompt-to-video and prompt-to-image dashboard that runs on your own GPU box
through ComfyUI. Type a prompt, pick a model, and the clip shows up in a gallery and on Telegram.

It was built for a two-machine setup, but the shape is generic:

- **GPU machine** ("White-PC" here: Windows, RTX 3090 24 GB) runs ComfyUI and holds the models.
- **Always-on controller** ("tigerclaw" here: a MacBook Air) runs this Flask app, keeps the job list, stores the
  finished files, serves the web UI and forwards results to Telegram. Nothing renders on it.

```
browser ──> Flask dashboard (:5030) ──> ComfyUI API (:8000) on the GPU box
                 │  poll /queue + /history every 10 s, websocket for step progress
                 │  download finished MP4/PNG, save to data/outputs/
                 └─> Telegram bot (optional)
```

## What it can do

| Task | Pipelines | Notes |
|------|-----------|-------|
| Text → Video | LTX-2.5 22B distilled, LTX-2 19B distilled, Wan 2.2 14B (4-step / 20-step), Wan 2.2 5B | LTX models generate **audio** too |
| Image → Video | LTX-2.5, LTX-2, Wan 2.2 5B | upload a still, it becomes frame 1 |
| Text → Image | Wan 2.2 14B single-frame (4-step / 20-step) | no dedicated image model needed |
| Image → Image | Wan 2.2 14B low-noise img2img | strength slider |
| Extend | last frame of a clip → new image-to-video shot | chain shots into longer videos |
| Stitch | join a chain into one MP4 (ffmpeg, audio fades at seams) | 2 × 10 s → 20 s verified |
| Fix face | ReActor face swap across every frame + GFPGAN | optional custom node |

Everything is submitted as **ComfyUI API-format graphs** built in `workflows.py`. No workflow JSON files are
POSTed at runtime; the graphs are hand-flattened from ComfyUI's bundled templates and validated against the
server's `/object_info`.

## Requirements

GPU box:
- ComfyUI ≥ 0.32 (LTX-2.5 support), reachable on the LAN, started with `--listen 0.0.0.0`.
- Models in the usual ComfyUI folders. Names are listed in `workflows.py` under `MODELS`; change them there if
  your files differ.
- Optional: [ComfyUI-ReActor](https://github.com/Gourieff/ComfyUI-ReActor) for the "fix face" button
  (`inswapper_128.onnx` in `models/insightface`, `GFPGANv1.4.pth` in `models/facerestore_models`).

Controller:
- Python 3.11+ with `flask`, `requests`, `websocket-client`.
- `ffmpeg` on PATH or set in `config.json` (needed for extend and stitch).
- Optional: an [OpenClaw](https://openclaw.ai) install with a Telegram bot; the app reads the bot token and
  your chat id from it. Without it the Telegram toggle just reports "no bot creds".

## Quick start

```bash
git clone https://github.com/sparkso/comfyui-local-vid-gen-spark.git localvidgen
cd localvidgen
# point it at your ComfyUI
$EDITOR config.json        # comfy_url, port, ffmpeg path, openclaw_dir
python3 server.py          # http://localhost:5030
```

`config.json`:

```json
{
  "port": 5030,
  "comfy_url": "http://10.0.0.20:8000",
  "poll_seconds": 10,
  "openclaw_dir": "/Users/you/.openclaw",
  "ffmpeg": "/opt/homebrew/bin/ffmpeg"
}
```

Job state lives in `data/jobs.json`, outputs in `data/outputs/`, uploaded stills in `data/uploads/`. All of
`data/` is gitignored.

## Using the dashboard

1. **Pick a task** (Text → Video, Image → Video, Text → Image, Image → Image), then a model.
2. **Write the prompt.** Describe subject, action, camera and, for LTX, the sound you want. Both video model
   families hallucinate on-screen text, so keep scenes textless and add captions in post.
3. **Size and length.** Presets are the final output size. LTX needs sides divisible by 32 and frames `8n+1`
   (241 = 10 s at 24 fps). Wan 14B needs sides divisible by 16 and frames `4n+1`.
4. **Generate.** The card shows queue state, then step progress once sampling starts. Model loading shows an
   indeterminate bar and can take 5–7 minutes after a model switch; keep batches on one model.
5. **Gallery actions** on a finished card: download, send to Telegram, edit & reuse, rerun with a new seed,
   animate / restyle (images), extend, stitch chain, fix face.

### Longer videos

Generate the first 10 s shot, press **extend**, give the next shot's prompt, repeat, then press **stitch chain**
on the last shot. Two habits keep the character consistent:

- Repeat the character description verbatim at the start of every shot prompt.
- End every shot with the character clearly in frame ("she stops and faces the camera"); the next shot starts
  from that exact frame.

### Face consistency

If the face drifts from your reference photo, render at the larger preset (1088×1920) from a tight crop of the
face, then use **fix face** with a sharp, front-facing photo. Expect roughly 1 frame per second for the swap.

## Deploying as a service

`deploy.sh` copies the app to a Mac over SSH and installs the launchd plist in `launchd/`. It is written for the
author's hosts, so edit `HOST`, `REMOTE_DIR` and the plist paths first, and export `TIGERCLAW_PASS` (or switch it
to key auth). `whitepc.py` sleeps/wakes the GPU box via SSH and Wake-on-LAN; edit its constants for your box.

## HTTP API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/status` | ComfyUI reachable, VRAM, queue depth, disk free |
| GET | `/api/presets` | tasks, pipelines, sizes, frame options |
| GET | `/api/jobs` | all jobs with live progress |
| POST | `/api/jobs` | multipart: `mode, prompt, width, height, frames, fps, seed, count, negative, strength, image` |
| POST | `/api/jobs/<id>/extend` | JSON `{prompt, mode, frames, seed}` → new job from the last frame |
| POST | `/api/jobs/<id>/faceswap` | multipart `face` (optional) → ReActor pass |
| POST | `/api/stitch` | JSON `{ids:[...]}` → one MP4 |
| POST | `/api/jobs/<id>/rerun`, `/cancel`, `/telegram` | |
| DELETE | `/api/jobs/<id>` | remove job and file |
| GET | `/videos/<file>` | serve an output |

## Layout

```
server.py        Flask app, poller, websocket listener, Telegram, ffmpeg helpers
workflows.py     API-graph builders + PRESETS (this is where models and pipelines live)
static/index.html  the whole UI, no build step
whitepc.py       sleep / wake / status for the GPU box
deploy.sh        rsync-style deploy + launchd install
launchd/         service definition
CLAUDE.md        working notes for AI-assisted development
```

## License

MIT
