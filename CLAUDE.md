# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

LocalVidGen is a Higgsfield-style prompt-to-video dashboard. It runs on **tigerclaw** (MacBook Air, Tailscale
`100.114.171.88`, port **5030**) and drives **ComfyUI on White-PC** (`http://10.0.0.20:8000`, Windows, RTX 3090
24 GB) over the LAN. Nothing renders on the Mac; the Mac only builds ComfyUI API prompts, polls, downloads the
MP4s, serves the gallery, and forwards videos to Telegram.

It replaces the old ad-hoc flow (POST a workflow JSON + `/tmp/poll_comfy.py` polling every 10 s + Telegram
delivery, all of which lived in `/tmp` and was lost on reboot).

## Layout

- `server.py` - Flask app + two daemon threads: a 10 s poller (`poll_once`) and a websocket listener for
  ComfyUI progress events. Job state is `data/jobs.json`; finished videos are `data/outputs/<job_id>.mp4`;
  uploaded i2v stills are `data/uploads/`. `data/` is gitignored and lives only on tigerclaw.
- `workflows.py` - ComfyUI **API-format** graph builders plus the UI `PRESETS` (keyed by pipeline id, each
  tagged with a `task`: t2i / t2v / i2i / i2v). Video: `ltx2_t2v`, `ltx2_i2v`, `wan22_t2v`, `wan22_t2v_hq`,
  `wan22_5b_t2v`, `wan22_5b_i2v`. Image: `wan22_t2i`, `wan22_t2i_hq` (Wan 14B at length=1) and `wan22_i2i`
  (VAE-encode + partial denoise with the low-noise expert, `strength` = denoise). No dedicated image model
  (Flux, Qwen-Image, SDXL) is installed on White-PC; their text encoders are, so adding one is a model download
  plus a new builder. Graphs are hand-flattened from the UI-format template workflows on tigerclaw
  (`~/.openclaw/workspace/comfyui/*.json`) and White-PC (`/api/userdata/workflows/...`); UI-format JSON with
  subgraphs cannot be POSTed to `/prompt`. Validate new builders against `/object_info` before deploying.
- `static/index.html` - the whole UI, vanilla JS, polls `/api/jobs` every 3 s and `/api/status` every 8 s.
- `config.json` - port, ComfyUI URL, poll interval, OpenClaw dir (for Telegram creds).
- `launchd/com.tigerclaw.localvidgen.plist` - service definition; `deploy.sh` installs it.

## Deploy / run

```bash
./deploy.sh            # scp files to tigerclaw, install plist, restart service, print status
./deploy.sh --logs     # same, then tail server.out.log / server.err.log
```

The service runs `/opt/homebrew/bin/python3` (3.14) on tigerclaw, which already has flask, requests,
websocket-client and Pillow. There is no venv. Run locally for development with `python3 server.py`
(it will target White-PC over Tailscale only if you change `comfy_url`; from this Mac 10.0.0.20 is not routable
unless you are on the same LAN).

SSH to tigerclaw needs password auth forced, otherwise macOS keychain keys trigger "too many auth failures":
`sshpass -e ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password tigerclaw@100.114.171.88`.

## Key mechanics

- **Submit**: `submit_job()` uploads the still (i2v only) via `/upload/image`, builds the graph with
  `filename_prefix=video/localvidgen/<job_id>`, POSTs `/prompt` with the server's `client_id` so websocket
  progress messages are addressed to us.
- **Poll**: a job is `queued` while its prompt_id is in `/queue` pending, `running` while in running, then the
  poller reads `/history/<prompt_id>`; a missing history entry after leaving the queue means ComfyUI restarted.
  The MP4 is pulled with `/view` and, if `auto_telegram` is on (`data/settings.json`), sent via the Telegram bot.
- **Telegram creds are never in this repo**: bot token comes from `~/.openclaw/openclaw.json`
  (`channels.telegram.botToken`) and the chat id from `~/.openclaw/credentials/telegram-allowFrom.json`.
- **Model constraints** (enforced in `workflows.py`): LTX-2 sides divisible by 32 and frames `8n+1` (max 241);
  Wan 14B sides divisible by 16, Wan 5B by 32, frames `4n+1`. LTX-2 renders stage 1 at half the requested size,
  then a 2x latent upsample and a second sampler pass; the "final size" shown in the UI is the post-upscale size.
- **Timing on the 3090**: a cold model switch (LTX-2 <-> Wan 14B) costs 5-7 minutes of loading before the first
  step; the render itself is ~20 s/step for LTX-2 stage 1 at 704x1280. Measured 2026-09-05: LTX-2 49 frames
  704x1280 = 650 s total; Wan 14B single image 832x1216 = 406 s (mostly loading). Keep the same model for
  batches. Outputs land on White-PC in `output/video/localvidgen/` or `output/images/localvidgen/` too.
- `/api/comfy/outputs`, `/api/comfy/view`, `/api/comfy/import` expose White-PC's `output/` folder through the
  Mac so older manual renders can be played and imported.
- **Chaining longer videos**: `POST /api/jobs/<id>/extend` extracts the parent's last frame with ffmpeg
  (`/opt/homebrew/bin/ffmpeg` on tigerclaw, not on the ssh PATH) and submits an `ltx2_i2v` job with `parent` /
  `chain_index` set; `POST /api/stitch {ids}` concatenates finished clips with per-clip audio fades into a
  `story-*` gallery entry. First 2x10 s chain verified 2026-09-05: identity held across the seam, but the seam
  quality depends on the parent's last frame, so end each shot prompt with the character clearly in frame
  ("she stops and faces the camera"). Stronger continuity is available via `LTXVAddGuide` with the last 9
  frames as a video guide (node is installed; not wired up yet).

## Related infrastructure

- Tiger HQ portal (port 5050 on tigerclaw, `~/.openclaw/workspace/portal/index.html`) links every dashboard;
  the LocalVidGen card is in the "Tigerclaw Mac" column. Back the file up (`index.html.bak-pre-*`) before editing.
- Ports already in use on tigerclaw: 5000/5001/5004/5005/5006/5008/5009/5010/5012-5015/5020/5021/5050/5055/5056/5090.
- White-PC also runs Ollama on `:11434` and the Polly weather forecaster; ComfyUI shares the single GPU with them.
