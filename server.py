#!/usr/bin/env python3
"""LocalVidGen dashboard: drives ComfyUI on White-PC (LTX-2 / Wan 2.2) from tigerclaw.

Runs as a launchd service on tigerclaw (port 5030). Submits API-format prompts, polls
ComfyUI every 10s, downloads finished MP4s into data/outputs/, optionally forwards them to
Telegram via the OpenClaw bot, and serves a single-page UI + JSON API.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

import workflows

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
OUTPUTS = DATA / "outputs"
UPLOADS = DATA / "uploads"
JOBS_FILE = DATA / "jobs.json"
SETTINGS_FILE = DATA / "settings.json"
for d in (DATA, OUTPUTS, UPLOADS):
    d.mkdir(parents=True, exist_ok=True)

CONFIG = {
    "port": 5030,
    "comfy_url": "http://10.0.0.20:8000",
    "poll_seconds": 10,
    "openclaw_dir": os.path.expanduser("~/.openclaw"),
    "ffmpeg": "/opt/homebrew/bin/ffmpeg",
}
cfg_path = BASE / "config.json"
if cfg_path.exists():
    CONFIG.update(json.loads(cfg_path.read_text()))
COMFY = CONFIG["comfy_url"].rstrip("/")
CLIENT_ID = "localvidgen-" + uuid.uuid4().hex[:12]

ACTIVE = ("queued", "running")
VIDEO_EXTS = (".mp4", ".webm", ".mkv")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(*a):
    print(now_iso(), *a, flush=True)


# ----------------------------------------------------------------------------- job store
class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = []
        if JOBS_FILE.exists():
            try:
                self.jobs = json.loads(JOBS_FILE.read_text())
            except Exception as e:
                log("jobs.json unreadable, starting empty:", e)

    def save(self):
        with self.lock:
            tmp = JOBS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.jobs, indent=1, ensure_ascii=False))
            tmp.replace(JOBS_FILE)

    def add(self, job):
        with self.lock:
            self.jobs.insert(0, job)
            self.save()

    def get(self, job_id):
        with self.lock:
            for j in self.jobs:
                if j["id"] == job_id:
                    return j
        return None

    def update(self, job_id, **fields):
        with self.lock:
            j = self.get(job_id)
            if j is None:
                return None
            j.update(fields)
            self.save()
            return j

    def remove(self, job_id):
        with self.lock:
            self.jobs = [j for j in self.jobs if j["id"] != job_id]
            self.save()

    def all(self):
        with self.lock:
            return list(self.jobs)


STORE = Store()


def load_settings():
    s = {"auto_telegram": True}
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text()))
        except Exception:
            pass
    return s


def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=1))


# ----------------------------------------------------------------------------- telegram
def telegram_creds():
    """Bot token + owner chat id from the OpenClaw install on this Mac. Never stored in this repo."""
    oc = Path(CONFIG["openclaw_dir"])
    token = chat_id = None
    try:
        token = json.loads((oc / "openclaw.json").read_text())["channels"]["telegram"]["botToken"]
    except Exception:
        pass
    for name in ("telegram-allowFrom.json", "telegram-default-allowFrom.json"):
        try:
            ids = json.loads((oc / "credentials" / name).read_text()).get("allowFrom") or []
            if ids:
                chat_id = str(ids[0])
                break
        except Exception:
            continue
    return token, chat_id


def video_dims(path: Path):
    """width/height/duration via ffprobe (next to the configured ffmpeg); empty dict if unavailable."""
    probe = CONFIG["ffmpeg"].replace("ffmpeg", "ffprobe")
    try:
        out = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height:format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=30).stdout.split()
        w, h = out[0].split(",")[:2]
        dur = out[1] if len(out) > 1 else "0"
        return {"width": int(w), "height": int(h), "duration": int(float(dur))}
    except Exception:
        return {}


def telegram_send_video(path: Path, caption: str):
    token, chat_id = telegram_creds()
    if not token or not chat_id:
        return False, "telegram credentials not found in ~/.openclaw"
    is_image = path.suffix.lower() in IMAGE_EXTS
    try:
        with open(path, "rb") as fh:
            if is_image:
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": caption[:1000]},
                    files={"photo": (path.name, fh, "image/png")},
                    timeout=300,
                )
            else:
                # Without explicit width/height Telegram guesses the aspect and shows portrait clips squashed.
                data = {"chat_id": chat_id, "caption": caption[:1000], "supports_streaming": "true"}
                data.update(video_dims(path))
                r = requests.post(
                    f"https://api.telegram.org/bot{token}/sendVideo",
                    data=data,
                    files={"video": (path.name, fh, "video/mp4")},
                    timeout=300,
                )
        if r.ok and r.json().get("ok"):
            return True, "sent"
        return False, r.text[:300]
    except Exception as e:
        return False, str(e)


# ----------------------------------------------------------------------------- comfy client
def comfy_get(path, timeout=8, **kw):
    return requests.get(f"{COMFY}{path}", timeout=timeout, **kw)


def comfy_status():
    """One call the UI polls: online flag, GPU memory, queue depth."""
    out = {"online": False, "url": COMFY, "queue_running": 0, "queue_pending": 0}
    try:
        st = comfy_get("/system_stats", timeout=4).json()
        dev = (st.get("devices") or [{}])[0]
        out.update(
            online=True,
            gpu=dev.get("name", "").replace(" : cudaMallocAsync", ""),
            vram_total=dev.get("vram_total", 0),
            vram_free=dev.get("vram_free", 0),
            comfy_version=st.get("system", {}).get("comfyui_version"),
        )
        q = comfy_get("/queue", timeout=4).json()
        out["queue_running"] = len(q.get("queue_running", []))
        out["queue_pending"] = len(q.get("queue_pending", []))
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def comfy_upload_image(path: Path):
    with open(path, "rb") as fh:
        r = requests.post(
            f"{COMFY}/upload/image",
            files={"image": (path.name, fh)},
            data={"overwrite": "true", "type": "input"},
            timeout=60,
        )
    r.raise_for_status()
    return r.json()["name"]


def comfy_submit(graph):
    r = requests.post(f"{COMFY}/prompt", json={"prompt": graph, "client_id": CLIENT_ID}, timeout=30)
    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"ComfyUI rejected prompt: {json.dumps(detail)[:1500]}")
    return r.json()["prompt_id"]


def comfy_history(prompt_id):
    r = comfy_get(f"/history/{prompt_id}", timeout=10)
    r.raise_for_status()
    return r.json().get(prompt_id)


def find_output(history_entry):
    """SaveVideo/SaveImage report under outputs[node]['images'] (type 'output'); scan defensively."""
    fallback = None
    for node_out in (history_entry.get("outputs") or {}).values():
        for key in ("images", "video", "videos", "gifs"):
            for item in node_out.get(key) or []:
                if not isinstance(item, dict) or item.get("type", "output") != "output":
                    continue  # LoadVideo/LoadImage echo their *input* file here; only saved outputs count
                fn = str(item.get("filename", "")).lower()
                if fn.endswith(VIDEO_EXTS):
                    return item
                if fn.endswith(IMAGE_EXTS):
                    fallback = fallback or item
    return fallback


def comfy_download(item, dest: Path):
    params = {"filename": item["filename"], "subfolder": item.get("subfolder", ""), "type": item.get("type", "output")}
    with requests.get(f"{COMFY}/view", params=params, stream=True, timeout=600) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(r.raw, fh)
        tmp.replace(dest)
    return dest


# ----------------------------------------------------------------------------- progress (websocket)
PROGRESS = {}  # prompt_id -> {"value","max","node","updated"}


def ws_listener():
    try:
        import websocket  # websocket-client
    except ImportError:
        log("websocket-client not installed; live progress disabled")
        return

    def on_message(ws, msg):
        if not isinstance(msg, str):
            return
        try:
            m = json.loads(msg)
        except Exception:
            return
        t, d = m.get("type"), m.get("data") or {}
        pid = d.get("prompt_id")
        if not pid:
            return
        if t == "progress":
            PROGRESS[pid] = {"value": d.get("value"), "max": d.get("max"), "node": d.get("node"), "updated": time.time()}
        elif t == "executing":
            p = PROGRESS.setdefault(pid, {})
            p.update(node=d.get("node"), updated=time.time())
        elif t == "execution_error":
            STORE_ERR[pid] = d.get("exception_message") or "execution_error"

    while True:
        try:
            ws = websocket.WebSocketApp(
                COMFY.replace("http", "ws", 1) + f"/ws?clientId={CLIENT_ID}", on_message=on_message
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log("ws error:", e)
        time.sleep(5)


STORE_ERR = {}


# ----------------------------------------------------------------------------- poller (10s, like poll_comfy.py)
def poll_once():
    active = [j for j in STORE.all() if j["status"] in ACTIVE]
    if not active:
        return
    try:
        q = comfy_get("/queue", timeout=6).json()
    except Exception as e:
        for j in active:
            STORE.update(j["id"], last_error=f"White-PC unreachable: {e}"[:200])
        return
    running_ids = {item[1] for item in q.get("queue_running", [])}
    pending_ids = {item[1] for item in q.get("queue_pending", [])}

    for j in active:
        pid = j["prompt_id"]
        if pid in running_ids:
            if j["status"] != "running":
                STORE.update(j["id"], status="running", started_at=now_iso(), last_error=None)
            continue
        if pid in pending_ids:
            continue
        # Not in queue: finished, errored, or dropped (e.g. ComfyUI restart).
        try:
            h = comfy_history(pid)
        except Exception as e:
            STORE.update(j["id"], last_error=f"history fetch failed: {e}"[:200])
            continue
        if not h:
            STORE.update(j["id"], status="error", finished_at=now_iso(),
                         error=STORE_ERR.pop(pid, None) or "Prompt vanished from ComfyUI queue (server restarted?)")
            continue
        status = (h.get("status") or {}).get("status_str")
        if status == "error" or STORE_ERR.get(pid):
            msgs = (h.get("status") or {}).get("messages") or []
            detail = STORE_ERR.pop(pid, None)
            for m in msgs:
                if isinstance(m, list) and m and m[0] == "execution_error":
                    detail = (m[1] or {}).get("exception_message") or detail
            STORE.update(j["id"], status="error", finished_at=now_iso(), error=(detail or "execution error")[:800])
            continue
        item = find_output(h)
        if not item:
            STORE.update(j["id"], status="error", finished_at=now_iso(), error="Finished but no video/image output found")
            continue
        dest = OUTPUTS / f"{j['id']}{Path(item['filename']).suffix.lower() or '.mp4'}"
        try:
            comfy_download(item, dest)
        except Exception as e:
            STORE.update(j["id"], last_error=f"download failed: {e}"[:200])
            continue
        elapsed = None
        if j.get("started_at"):
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(j["started_at"])).total_seconds()
        STORE.update(
            j["id"], status="done", finished_at=now_iso(), elapsed=elapsed, last_error=None,
            file=dest.name, size=dest.stat().st_size,
            remote={"filename": item["filename"], "subfolder": item.get("subfolder", "")},
        )
        PROGRESS.pop(pid, None)
        log(f"job {j['id']} done -> {dest.name} ({dest.stat().st_size} bytes)")
        if load_settings().get("auto_telegram"):
            deliver_telegram(j["id"])


def deliver_telegram(job_id):
    j = STORE.get(job_id)
    if not j or not j.get("file"):
        return False, "no file"
    preset = workflows.PRESETS.get(j["mode"], {})
    task = workflows.TASKS.get(preset.get("task", ""), "")
    frames = f" | {j['frames']}f" if j.get("frames", 1) > 1 else ""
    cap = f"🎬 {task} · {preset.get('label', j['mode'])} | {j['width']}×{j['height']}{frames} | seed {j['seed']}\n{j['prompt']}"
    ok, msg = telegram_send_video(OUTPUTS / j["file"], cap)
    STORE.update(job_id, telegram={"ok": ok, "msg": msg, "at": now_iso()})
    log(f"telegram job {job_id}: {ok} {msg}")
    return ok, msg


def poller():
    while True:
        try:
            poll_once()
        except Exception as e:
            log("poll error:", e)
        time.sleep(CONFIG["poll_seconds"])


# ----------------------------------------------------------------------------- submission
def ffmpeg():
    exe = CONFIG["ffmpeg"]
    if not Path(exe).exists():
        raise RuntimeError(f"ffmpeg not found at {exe}")
    return exe


def extract_last_frame(video: Path) -> Path:
    """Last frame of a clip as PNG (input for the next clip in a chain)."""
    out = UPLOADS / f"{video.stem}-last.png"
    cmd = [ffmpeg(), "-y", "-v", "error", "-sseof", "-0.05", "-i", str(video), "-update", "1", "-frames:v", "1", str(out)]
    subprocess.run(cmd, check=True, timeout=120)
    if not out.exists() or out.stat().st_size == 0:
        # Fallback: decode the whole clip and keep the final frame.
        cmd = [ffmpeg(), "-y", "-v", "error", "-i", str(video), "-vf", "reverse", "-frames:v", "1", str(out)]
        subprocess.run(cmd, check=True, timeout=300)
    return out


def stitch_clips(paths, dest: Path, fade=0.12):
    """Concatenate same-size clips; short audio fades at each seam avoid clicks without changing length."""
    args = [ffmpeg(), "-y", "-v", "error"]
    for p in paths:
        args += ["-i", str(p)]
    parts, fc = [], []
    for i, p in enumerate(paths):
        dur = float(subprocess.run(
            [ffmpeg().replace("ffmpeg", "ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=60).stdout.strip() or 0)
        st = max(0.0, dur - fade)
        fc.append(f"[{i}:a]afade=t=in:st=0:d={fade},afade=t=out:st={st:.3f}:d={fade}[a{i}]")
        parts.append(f"[{i}:v][a{i}]")
    fc.append("".join(parts) + f"concat=n={len(paths)}:v=1:a=1[v][a]")
    args += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", str(dest)]
    subprocess.run(args, check=True, timeout=1800)
    return dest


def submit_job(params, image_path=None, extra=None):
    mode = params["mode"]
    if mode not in workflows.PRESETS:
        raise ValueError("unknown mode")
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    image_name = None
    if workflows.PRESETS[mode]["needs_image"]:
        if not image_path:
            raise ValueError("this mode needs an input image")
        image_name = comfy_upload_image(image_path)
    p = dict(params, image=image_name)
    preset = workflows.PRESETS[mode]
    sub = "video" if preset["output"] == "video" else "images"
    graph = workflows.build(mode, p, filename_prefix=f"{sub}/localvidgen/{job_id}")
    prompt_id = comfy_submit(graph)
    w, h = workflows.final_size(mode, graph)
    is_video = preset["output"] == "video"
    job = {
        "id": job_id, "prompt_id": prompt_id, "status": "queued", "created_at": now_iso(),
        "mode": mode, "task": preset["task"], "output": preset["output"],
        "prompt": p["prompt"], "negative": p.get("negative") or "",
        "width": w, "height": h,
        "frames": (p.get("frames") or preset["default_frames"]) if is_video else 1,
        "fps": (p.get("fps") or preset["fps"]) if is_video else 0, "seed": p["seed"],
        "strength": p.get("strength") if preset.get("has_strength") else None,
        "image": image_name, "image_local": Path(image_path).name if image_path else None,
    }
    if extra:
        job.update(extra)
    STORE.add(job)
    log(f"submitted {job_id} -> prompt {prompt_id} ({mode})")
    return job


# ----------------------------------------------------------------------------- flask
app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="/static")


@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.get("/api/status")
def api_status():
    s = comfy_status()
    s["auto_telegram"] = load_settings().get("auto_telegram", True)
    tok, chat = telegram_creds()
    s["telegram_ready"] = bool(tok and chat)
    s["client_id"] = CLIENT_ID
    try:
        du = shutil.disk_usage(OUTPUTS)
        s["disk_free"] = du.free
        s["disk_total"] = du.total
        s["library_bytes"] = sum(p.stat().st_size for d in (OUTPUTS, UPLOADS) for p in d.iterdir() if p.is_file())
    except OSError:
        pass
    return jsonify(s)


@app.get("/api/presets")
def api_presets():
    return jsonify({"presets": workflows.PRESETS, "tasks": workflows.TASKS,
                    "wan_negative": workflows.WAN_NEGATIVE_DEFAULT, "models": workflows.MODELS})


@app.get("/api/jobs")
def api_jobs():
    jobs = STORE.all()
    now = time.time()
    for j in jobs:
        if j["status"] in ACTIVE:
            p = PROGRESS.get(j["prompt_id"])
            if p and p.get("max"):
                j["progress"] = {"value": p.get("value"), "max": p.get("max"), "node": p.get("node"),
                                 "age": round(now - p.get("updated", now))}
    return jsonify(jobs)


@app.post("/api/jobs")
def api_submit():
    f = request.form
    try:
        params = {
            "mode": f["mode"],
            "prompt": f["prompt"].strip(),
            "negative": f.get("negative", "").strip(),
            "width": int(f["width"]),
            "height": int(f["height"]),
            "frames": int(f["frames"]) if f.get("frames") else None,
            "fps": float(f["fps"]) if f.get("fps") else None,
            "strength": float(f["strength"]) if f.get("strength") else 0.6,
            "seed": int(f["seed"]) if f.get("seed") not in (None, "", "-1") else int.from_bytes(os.urandom(6), "big"),
        }
        if not params["prompt"]:
            return jsonify({"error": "prompt is empty"}), 400
        image_path = None
        up = request.files.get("image")
        if up and up.filename:
            ext = Path(up.filename).suffix.lower() or ".png"
            image_path = UPLOADS / f"{uuid.uuid4().hex[:10]}{ext}"
            up.save(image_path)
        count = max(1, min(8, int(f.get("count", 1))))
        jobs = []
        for i in range(count):
            p = dict(params, seed=params["seed"] + i)
            jobs.append(submit_job(p, image_path))
        return jsonify(jobs)
    except (KeyError, ValueError) as e:
        return jsonify({"error": f"bad request: {e}"}), 400
    except requests.RequestException as e:
        return jsonify({"error": f"White-PC unreachable: {e}"}), 502
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502


@app.post("/api/jobs/r2v")
def api_r2v():
    """MiniMax H3 reference-to-video: multipart with prompt/width/height/frames/seed/turbo plus repeated
    ref_images / ref_videos / ref_audios file fields. Refer to them in the prompt as <Picture 1>, <Video 1>, <Audio 1>."""
    f = request.form
    try:
        prompt = f["prompt"].strip()
        if not prompt:
            return jsonify({"error": "prompt is empty"}), 400
        width, height = int(f.get("width", 480)), int(f.get("height", 832))
        frames = int(f.get("frames", 124))
        seed = int(f["seed"]) if f.get("seed") not in (None, "", "-1") else int.from_bytes(os.urandom(6), "big")
        turbo = f.get("turbo", "1") != "0"
        names = {"ref_images": [], "ref_videos": [], "ref_audios": []}
        for key in names:
            for up in request.files.getlist(key):
                if not up or not up.filename:
                    continue
                ext = Path(up.filename).suffix.lower() or ".bin"
                local = UPLOADS / f"ref-{uuid.uuid4().hex[:8]}{ext}"
                up.save(local)
                names[key].append(comfy_upload_file(local))
        if not any(names.values()):
            return jsonify({"error": "add at least one reference image, video or audio"}), 400
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-r2v" + uuid.uuid4().hex[:2]
        graph = workflows.build_minimax_r2v(prompt, width, height, frames, seed, turbo=turbo,
                                            ref_image_size=f.get("ref_image_size", "match"),
                                            ref_images=names["ref_images"], ref_videos=names["ref_videos"],
                                            ref_audios=names["ref_audios"], filename_prefix=f"video/localvidgen/{job_id}")
        prompt_id = comfy_submit(graph)
    except (KeyError, ValueError) as e:
        return jsonify({"error": f"bad request: {e}"}), 400
    except (requests.RequestException, RuntimeError) as e:
        return jsonify({"error": str(e)}), 502
    job = {"id": job_id, "prompt_id": prompt_id, "status": "queued", "created_at": now_iso(),
           "mode": "minimax_r2v", "task": "r2v", "output": "video", "prompt": prompt, "negative": "",
           "width": width, "height": height, "frames": frames, "fps": 24, "seed": seed,
           "image": (names["ref_images"] or [None])[0], "refs": names}
    STORE.add(job)
    log(f"submitted {job_id} -> prompt {prompt_id} (minimax_r2v, refs {[len(v) for v in names.values()]})")
    return jsonify(job)


@app.post("/api/jobs/<job_id>/rerun")
def api_rerun(job_id):
    j = STORE.get(job_id) or abort(404)
    body = request.get_json(silent=True) or {}
    params = {k: j.get(k) for k in ("mode", "prompt", "negative", "width", "height", "frames", "fps")}
    params["strength"] = j.get("strength") or 0.6
    params["seed"] = int(body["seed"]) if body.get("seed") is not None else int.from_bytes(os.urandom(6), "big")
    image_path = None
    if j.get("image_local"):
        image_path = UPLOADS / j["image_local"]
        if not image_path.exists():
            return jsonify({"error": "original upload no longer on disk"}), 400
    try:
        return jsonify(submit_job(params, image_path))
    except (requests.RequestException, RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 502


@app.post("/api/jobs/<job_id>/extend")
def api_extend(job_id):
    """Continue a finished clip: its last frame seeds an image-to-video job with a new shot prompt."""
    parent = STORE.get(job_id) or abort(404)
    if parent.get("status") != "done" or not parent.get("file") or Path(parent["file"]).suffix.lower() in IMAGE_EXTS:
        return jsonify({"error": "parent must be a finished video"}), 400
    body = request.get_json(silent=True) or {}
    mode = body.get("mode") or "ltx2_i2v"
    if mode not in workflows.PRESETS or not workflows.PRESETS[mode]["needs_image"] or workflows.PRESETS[mode]["output"] != "video":
        return jsonify({"error": "mode must be an image-to-video pipeline"}), 400
    prompt = (body.get("prompt") or parent["prompt"]).strip()
    try:
        frame = extract_last_frame(OUTPUTS / parent["file"])
    except Exception as e:
        return jsonify({"error": f"last-frame extraction failed: {e}"}), 500
    params = {
        "mode": mode, "prompt": prompt, "negative": parent.get("negative") or "",
        "width": parent["width"], "height": parent["height"],
        "frames": int(body.get("frames") or parent.get("frames") or workflows.PRESETS[mode]["default_frames"]),
        "fps": parent.get("fps") or workflows.PRESETS[mode]["fps"],
        "seed": int(body["seed"]) if body.get("seed") is not None else int.from_bytes(os.urandom(6), "big"),
    }
    try:
        job = submit_job(params, frame, extra={"parent": job_id, "chain_index": (parent.get("chain_index") or 0) + 1})
    except (requests.RequestException, RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(job)


def comfy_upload_file(path: Path):
    """Any file (mp4 included) into ComfyUI's input dir; LoadVideo reads from there."""
    with open(path, "rb") as fh:
        r = requests.post(f"{COMFY}/upload/image", files={"image": (path.name, fh)},
                          data={"overwrite": "true", "type": "input"}, timeout=600)
    r.raise_for_status()
    return r.json()["name"]


@app.post("/api/jobs/<job_id>/faceswap")
def api_faceswap(job_id):
    """Post-process a finished clip: ReActor swaps the reference face into every frame. Multipart 'face' optional;
    falls back to the clip's own start image (i2v jobs) or its parent's."""
    parent = STORE.get(job_id) or abort(404)
    if parent.get("status") != "done" or not parent.get("file") or Path(parent["file"]).suffix.lower() in IMAGE_EXTS:
        return jsonify({"error": "job must be a finished video"}), 400
    face_path = None
    up = request.files.get("face")
    if up and up.filename:
        face_path = UPLOADS / f"face-{uuid.uuid4().hex[:8]}{Path(up.filename).suffix.lower() or '.png'}"
        up.save(face_path)
    else:
        cur = parent
        while cur and not face_path:
            if cur.get("image_local") and not cur["image_local"].endswith("-last.png") and (UPLOADS / cur["image_local"]).exists():
                face_path = UPLOADS / cur["image_local"]
            cur = STORE.get(cur["parent"]) if cur.get("parent") else None
    if not face_path:
        return jsonify({"error": "no reference face: upload one"}), 400
    restore = (request.form.get("restore", "1") != "0")
    try:
        video_name = comfy_upload_file(OUTPUTS / parent["file"])
        face_name = comfy_upload_image(face_path)
        job_id_new = datetime.now().strftime("%Y%m%d-%H%M%S") + "-fs" + uuid.uuid4().hex[:2]
        graph = workflows.build_faceswap(video_name, face_name, restore=restore,
                                         filename_prefix=f"video/localvidgen/{job_id_new}")
        prompt_id = comfy_submit(graph)
    except (requests.RequestException, RuntimeError) as e:
        return jsonify({"error": str(e)}), 502
    job = {
        "id": job_id_new, "prompt_id": prompt_id, "status": "queued", "created_at": now_iso(),
        "mode": "faceswap", "task": "fix", "output": "video",
        "prompt": f"Face fix of {job_id}: " + parent.get("prompt", "")[:200], "negative": "",
        "width": parent.get("width"), "height": parent.get("height"), "frames": parent.get("frames"),
        "fps": parent.get("fps"), "seed": 0, "image": face_name, "image_local": face_path.name,
        "faceswap_of": job_id, "parent": parent.get("parent"), "chain_index": parent.get("chain_index"),
    }
    STORE.add(job)
    log(f"faceswap {job_id} -> {job_id_new} (prompt {prompt_id})")
    return jsonify(job)


@app.post("/api/stitch")
def api_stitch():
    """Join finished clips (in order) into one MP4 that becomes its own gallery entry."""
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    if len(ids) < 2:
        return jsonify({"error": "need at least two job ids"}), 400
    paths = []
    for i in ids:
        j = STORE.get(i)
        if not j or j.get("status") != "done" or not j.get("file") or Path(j["file"]).suffix.lower() in IMAGE_EXTS:
            return jsonify({"error": f"{i} is not a finished video"}), 400
        paths.append(OUTPUTS / j["file"])
    first = STORE.get(ids[0])
    job_id = "story-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = OUTPUTS / f"{job_id}.mp4"
    try:
        stitch_clips(paths, dest)
    except Exception as e:
        return jsonify({"error": f"stitch failed: {e}"}), 500
    total_frames = sum((STORE.get(i) or {}).get("frames") or 0 for i in ids)
    job = {"id": job_id, "prompt_id": None, "status": "done", "created_at": now_iso(), "finished_at": now_iso(),
           "mode": first["mode"], "task": "t2v", "output": "video",
           "prompt": "Stitched: " + " → ".join((STORE.get(i) or {}).get("prompt", "")[:80] for i in ids),
           "negative": "", "width": first["width"], "height": first["height"], "frames": total_frames,
           "fps": first.get("fps"), "seed": first.get("seed"), "file": dest.name, "size": dest.stat().st_size,
           "stitched": ids}
    STORE.add(job)
    log(f"stitched {ids} -> {dest.name}")
    if load_settings().get("auto_telegram"):
        deliver_telegram(job_id)
    return jsonify(job)


@app.post("/api/jobs/<job_id>/cancel")
def api_cancel(job_id):
    j = STORE.get(job_id) or abort(404)
    try:
        if j["status"] == "running":
            requests.post(f"{COMFY}/interrupt", timeout=8)
        else:
            requests.post(f"{COMFY}/queue", json={"delete": [j["prompt_id"]]}, timeout=8)
    except Exception as e:
        return jsonify({"error": f"cancel failed: {e}"}), 502
    STORE.update(job_id, status="cancelled", finished_at=now_iso())
    return jsonify(STORE.get(job_id))


@app.delete("/api/jobs/<job_id>")
def api_delete(job_id):
    j = STORE.get(job_id) or abort(404)
    if j["status"] in ACTIVE:
        return jsonify({"error": "cancel it first"}), 400
    if j.get("file"):
        try:
            (OUTPUTS / j["file"]).unlink()
        except FileNotFoundError:
            pass
    STORE.remove(job_id)
    return jsonify({"ok": True})


@app.post("/api/jobs/<job_id>/telegram")
def api_telegram(job_id):
    STORE.get(job_id) or abort(404)
    ok, msg = deliver_telegram(job_id)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 502)


@app.get("/videos/<path:name>")
def video(name):
    return send_from_directory(OUTPUTS, name, conditional=True)


@app.get("/uploads/<path:name>")
def upload_file(name):
    return send_from_directory(UPLOADS, name, conditional=True)


@app.get("/api/settings")
def api_settings_get():
    return jsonify(load_settings())


@app.post("/api/settings")
def api_settings_set():
    s = load_settings()
    body = request.get_json(silent=True) or {}
    if "auto_telegram" in body:
        s["auto_telegram"] = bool(body["auto_telegram"])
    save_settings(s)
    return jsonify(s)


@app.get("/api/comfy/outputs")
def api_comfy_outputs():
    """Videos already sitting in ComfyUI's output dir on White-PC (older manual runs included)."""
    try:
        r = comfy_get("/internal/files/output", timeout=8)
        r.raise_for_status()
        names = [n.split(" [")[0] for n in r.json() if n.lower().endswith(VIDEO_EXTS + IMAGE_EXTS)]
        return jsonify(names)
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/comfy/view")
def api_comfy_view():
    """Stream a file from White-PC's output dir (proxy so the browser needs no LAN route)."""
    fn = request.args.get("filename") or abort(400)
    sub = request.args.get("subfolder", "")
    try:
        r = requests.get(f"{COMFY}/view", params={"filename": fn, "subfolder": sub, "type": "output"}, stream=True, timeout=600)
        r.raise_for_status()
    except Exception as e:
        abort(502, str(e))
    ctype = r.headers.get("Content-Type") or ("image/png" if fn.lower().endswith(IMAGE_EXTS) else "video/mp4")
    return Response(r.iter_content(64 * 1024), content_type=ctype)


@app.get("/api/comfy/import")
def api_comfy_import():
    """Copy one remote output into the local gallery as a job record."""
    fn = request.args.get("filename") or abort(400)
    job_id = "import-" + Path(fn).stem
    if STORE.get(job_id):
        return jsonify(STORE.get(job_id))
    ext = Path(fn).suffix.lower() or ".mp4"
    dest = OUTPUTS / f"{job_id}{ext}"
    try:
        comfy_download({"filename": fn, "subfolder": "", "type": "output"}, dest)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    is_img = ext in IMAGE_EXTS
    job = {"id": job_id, "prompt_id": None, "status": "done", "created_at": now_iso(), "finished_at": now_iso(),
           "mode": "wan22_t2v" if "wan" in fn.lower() else "ltx2_t2v", "task": "t2i" if is_img else "t2v",
           "output": "image" if is_img else "video", "prompt": f"(imported from White-PC: {fn})",
           "negative": "", "width": 0, "height": 0, "frames": 0, "fps": 0, "seed": 0,
           "file": dest.name, "size": dest.stat().st_size, "imported": True}
    STORE.add(job)
    return jsonify(job)


if __name__ == "__main__":
    threading.Thread(target=ws_listener, daemon=True, name="ws").start()
    threading.Thread(target=poller, daemon=True, name="poller").start()
    log(f"LocalVidGen on http://0.0.0.0:{CONFIG['port']} -> ComfyUI {COMFY}")
    app.run(host="0.0.0.0", port=int(CONFIG["port"]), threaded=True, debug=False, use_reloader=False)
