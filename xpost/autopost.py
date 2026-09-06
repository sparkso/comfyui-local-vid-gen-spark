#!/usr/bin/env python3
"""Hourly funny-clip poster for @VweeterLimited.

Runs on this Mac from launchd (com.vweeter.xpost-hourly). Each tick:
  1. If Xquik is up and a rendered clip is queued, post the oldest queued clip (media URL on S3),
     read the tweet back to verify, log it, DM Telegram.
  2. Then render the next idea from ideas.json on White-PC via the LocalVidGen dashboard
     (LTX-2.5, 10 s, portrait), download it, upload to S3, and queue it for posting.
State: data/xpost/state.json (used slugs, queue, history). Nothing here contains secrets; the Xquik key and
Telegram token are grepped from the marketing project's memory files, like cron/post-x-via-api.py does.

Usage: autopost.py [--dry-run] [--no-render] [--post-only]
"""
import argparse, hashlib, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATE_DIR = ROOT / "data" / "xpost"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE = STATE_DIR / "state.json"
LOG = STATE_DIR / "log.md"
IDEAS = HERE / "ideas.json"

DASH = "http://100.114.171.88:5030"
XQUIK = "https://xquik.com/api/v1"
ACCOUNT = "@VweeterLimited"
S3_BUCKET, S3_PREFIX = "locai", "xposts"
S3_PUBLIC = "https://locai.s3.ap-south-1.amazonaws.com"
AWS = "/usr/local/bin/aws"
MEM = Path.home() / ".claude/projects/-Users-sparkso-Code-vweeter-marketing/memory"
HKT = timezone(timedelta(hours=8))
WIDTH, HEIGHT = 480, 832          # MiniMax H3 comfort zone on the 3090; LTX ideas use the same portrait size
RENDER_TIMEOUT = 40 * 60          # per shot; a 15 s H3 shot is ~10 min


def now():
    return datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT")


def grep(path, pat):
    try:
        m = re.search(pat, Path(path).read_text())
        return m.group(0) if m else None
    except OSError:
        return None


def load_env_file():
    """Optional KEY=VALUE lines in ~/.config/localvidgen/env (e.g. TIGERCLAW_PASS for wake-on-LAN). Never in the repo."""
    p = Path.home() / ".config/localvidgen/env"
    try:
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


def secrets():
    load_env_file()
    return {
        "xquik": os.environ.get("XQUIK_API_KEY") or grep(MEM / "xquik-api-key.md", r"\bxq_[a-f0-9]{64}\b"),
        "tg": grep(MEM / "telegram-activity-notifications.md", r"\b\d{9,10}:AA[\w-]{30,}\b"),
    }


def http(url, *, headers=None, data=None, json_body=None, method=None, timeout=60):
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers = dict(headers or {}, **{"Content-Type": "application/json"})
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"), headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip().startswith(("{", "[")) else {"raw": body})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body[:300]}
    except Exception as e:
        return 0, {"error": str(e)}


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"used": [], "queue": [], "posted": [], "xquik_down_since": None}


def save_state(s):
    STATE.write_text(json.dumps(s, indent=1))


def log(line):
    with open(LOG, "a") as fh:
        fh.write(f"- {now()} {line}\n")
    print(line, flush=True)


def telegram(token, text):
    if not token:
        return
    http("https://api.telegram.org/bot%s/sendMessage" % token,
         data=urllib.parse.urlencode({"chat_id": "194069935", "text": text, "disable_web_page_preview": "true"}).encode(),
         timeout=30)


def telegram_video(token, path, caption):
    """Manual-post fallback: hand the owner the clip + caption so it can be posted from the phone."""
    if not token:
        return False
    # Explicit width/height, or Telegram squashes portrait clips into the wrong aspect.
    r = subprocess.run(["curl", "-s", "-m", "300", "-X", "POST", f"https://api.telegram.org/bot{token}/sendVideo",
                        "-F", "chat_id=194069935", "-F", f"caption={caption}", "-F", "supports_streaming=true",
                        "-F", f"width={WIDTH}", "-F", f"height={HEIGHT}",
                        "-F", f"video=@{path}"], capture_output=True, text=True)
    return '"ok":true' in r.stdout


# ----------------------------------------------------------------------------- render
def whitepc_online():
    st, b = http(f"{DASH}/api/status", timeout=15)
    return st == 200 and b.get("online", False)


def wake_whitepc():
    """Ask tigerclaw to send the magic packet (whitepc.py lives there)."""
    pw = os.environ.get("TIGERCLAW_PASS")
    if not pw:
        return False
    cmd = ["sshpass", "-e", "ssh", "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password",
           "-o", "ConnectTimeout=10", "tigerclaw@100.114.171.88",
           "cd ~/.openclaw/workspace/localvidgen && /opt/homebrew/bin/python3 whitepc.py wake"]
    r = subprocess.run(cmd, env=dict(os.environ, SSHPASS=pw), capture_output=True, text=True, timeout=300)
    return "ComfyUI up" in r.stdout


MODELS = {  # (t2v mode, i2v mode for continuation shots, frames for N seconds)
    "ltx25": ("ltx25_t2v", "ltx25_i2v", lambda s: max(9, min(241, (round(s * 24) // 8) * 8 + 1))),
    "minimax": ("minimax_t2v", "minimax_i2v", lambda s: (lambda n: n + (5 - (n % 17)) % 17)(max(5, round(s * 24)))),
}


def wait_job(job_id, timeout=RENDER_TIMEOUT):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st, jobs = http(f"{DASH}/api/jobs", timeout=30)
        j = next((x for x in jobs if x["id"] == job_id), None) if st == 200 else None
        if j and j["status"] == "done":
            return j
        if j and j["status"] in ("error", "cancelled"):
            raise RuntimeError(f"render {job_id} {j['status']}: {j.get('error')}")
        time.sleep(20)
    raise RuntimeError(f"render {job_id} timed out")


def render(idea):
    """One or more shots. Shot 1 is text-to-video; each later shot continues from the previous shot's last frame
    (the dashboard's extend path); multi-shot results are stitched into one MP4 by the dashboard."""
    shots = idea.get("shots") or [idea["prompt"]]
    t2v, i2v, frames_for = MODELS[idea.get("model", "ltx25")]
    frames = frames_for(idea.get("seconds", 10))
    seed = int(hashlib.sha256(idea["slug"].encode()).hexdigest()[:8], 16) % 10**9
    form = urllib.parse.urlencode({"mode": t2v, "prompt": shots[0], "width": WIDTH, "height": HEIGHT,
                                   "frames": frames, "fps": 24, "seed": seed, "count": 1}).encode()
    st, b = http(f"{DASH}/api/jobs", data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=180)
    if st != 200:
        raise RuntimeError(f"submit failed {st}: {b}")
    ids = [b[0]["id"]]
    job = wait_job(ids[0])
    for n, prompt in enumerate(shots[1:], start=2):
        st, b = http(f"{DASH}/api/jobs/{ids[-1]}/extend",
                     json_body={"mode": i2v, "frames": frames, "seed": seed + n, "prompt": prompt}, timeout=300)
        if st != 200:
            raise RuntimeError(f"extend shot {n} failed {st}: {b}")
        ids.append(b["id"])
        job = wait_job(ids[-1])
    if len(ids) > 1:
        st, b = http(f"{DASH}/api/stitch", json_body={"ids": ids}, timeout=1200)
        if st != 200:
            raise RuntimeError(f"stitch failed {st}: {b}")
        return b["id"], b["file"]
    return ids[0], job["file"]


def fetch_and_host(job_id, filename):
    local = STATE_DIR / filename
    urllib.request.urlretrieve(f"{DASH}/videos/{filename}", local)
    key = f"{S3_PREFIX}/{job_id}.mp4"
    subprocess.run([AWS, "s3", "cp", str(local), f"s3://{S3_BUCKET}/{key}", "--content-type", "video/mp4"],
                   check=True, capture_output=True, timeout=600)
    url = f"{S3_PUBLIC}/{key}"
    st, _ = http(url, method="HEAD", timeout=30)
    if st != 200:
        raise RuntimeError(f"S3 object not public: {url} -> {st}")
    return url, local.stat().st_size


# ----------------------------------------------------------------------------- post
def post_clip(key, item, attempt):
    idem = "vwx-" + hashlib.sha256(f"{item['slug']}|{item['text']}|{attempt}".encode()).hexdigest()[:32]
    st, b = http(f"{XQUIK}/x/tweets", json_body={"account": ACCOUNT, "text": item["text"], "media": [item["media_url"]]},
                 headers={"Authorization": f"Bearer {key}", "Idempotency-Key": idem}, timeout=180)
    if b.get("success"):
        return b.get("tweetId") or (b.get("result") or {}).get("id"), int(str(b.get("chargedCredits") or 0) or 0), None
    code = b.get("error") or ""
    msg = f"HTTP {st} [{code}]: {b.get('message') or b}"
    permanent = code in ("x_rejected", "x_daily_limit", "x_write_ambiguous") or st in (400, 401, 403, 422)
    return None, 0, (msg, permanent, bool(b.get("terminal") and not b.get("charged")))


def verify(key, tweet_id):
    for i in range(3):
        st, b = http(f"{XQUIK}/x/tweets/{tweet_id}", headers={"Authorization": f"Bearer {key}"}, timeout=45)
        t = b.get("tweet") or b
        if st == 200 and str(t.get("id")) == str(tweet_id):
            return t.get("url") or f"https://x.com/VweeterLimited/status/{tweet_id}"
        time.sleep(3 + i * 4)
    return None


def xquik_up(key):
    st, _ = http(f"{XQUIK}/credits", headers={"Authorization": f"Bearer {key}"}, timeout=25)
    return st == 200


def hand_off(s, st, item, reason):
    """Browser automation cannot attach video on x.com, so when the API cannot post, the clip goes to the owner
    on Telegram with its caption and LEAVES the queue: the owner posts it by hand, and it can never double-post."""
    local = STATE_DIR / Path(item["media_url"]).name
    cap = f"{reason}. This clip is yours to post by hand now (it will NOT auto-post later).\n\n{item['text']}"
    if local.exists() and telegram_video(s["tg"], local, cap):
        st["queue"].remove(item)
        st["posted"].append(dict(item, manual=f"handed to owner via Telegram {now()}"))
        log(f"HANDED OFF {item['slug']} to Telegram for manual posting")
        return True
    return False


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--post-only", action="store_true")
    ap.add_argument("--now", action="store_true", help="skip the random 1-30 min delay (launchd ticks use the delay)")
    a = ap.parse_args()
    if not (a.now or a.dry_run):
        import random
        delay = random.randint(60, 1800)   # owner: "don't post every hour exactly, add 1-30 min randomly"
        print(f"jitter: sleeping {delay // 60} min", flush=True)
        time.sleep(delay)
    s = secrets()
    if not s["xquik"]:
        print("no Xquik key", file=sys.stderr)
        return 1
    st = load_state()
    ideas = json.loads(IDEAS.read_text())

    # 1. post the oldest queued clip if Xquik answers
    if st["queue"] and not a.dry_run:
        if xquik_up(s["xquik"]):
            if st.get("xquik_down_since"):
                telegram(s["tg"], f"xpost: Xquik back (was down since {st['xquik_down_since']}); {len(st['queue'])} clip(s) queued will post hourly.")
                st["xquik_down_since"] = None
            item = st["queue"][0]
            item["attempts"] = item.get("attempts", 0)
            tid, credits, err = post_clip(s["xquik"], item, item["attempts"])
            if tid:
                url = verify(s["xquik"], tid) or f"https://x.com/VweeterLimited/status/{tid}"
                st["queue"].pop(0)
                st["posted"].append(dict(item, tweet_id=tid, url=url, posted_at=now(), credits=credits))
                log(f"POSTED {item['slug']} -> {url} ({credits} credits) text: {item['text']}")
                telegram(s["tg"], f"xpost landed: {item['slug']}\n{url}\n{item['text']}")
            else:
                msg, permanent, fresh_key_ok = err
                if fresh_key_ok:
                    item["attempts"] += 1
                if permanent:
                    st["queue"].pop(0)
                    log(f"DROPPED {item['slug']}: {msg}")
                    telegram(s["tg"], f"xpost dropped {item['slug']} (X refused): {msg[:200]}")
                else:
                    log(f"POST FAILED {item['slug']}: {msg}")
                    hand_off(s, st, item, f"Xquik write failed ({msg[:80]})")
        else:
            if not st.get("xquik_down_since"):
                st["xquik_down_since"] = now()
                telegram(s["tg"], f"xpost: Xquik not answering; {len(st['queue'])} clip(s) queued. Will keep rendering hourly and post when it recovers.")
            log(f"xquik down, {len(st['queue'])} queued")
            hand_off(s, st, st["queue"][0], "Xquik is down")
        save_state(st)
    if a.post_only:
        return 0

    # 2. render the next idea and queue it
    if a.no_render or len(st["queue"]) >= 6:
        log(f"render skipped (queue={len(st['queue'])})")
        return 0
    remaining = [i for i in ideas if i["slug"] not in st["used"]]
    if not remaining:
        telegram(s["tg"], "xpost: idea bank is empty. Add more ideas to xpost/ideas.json.")
        log("idea bank empty")
        return 0
    idea = remaining[0]
    idea.setdefault("punchline", idea.get("caption", ""))
    if a.dry_run:
        print("would render:", idea["slug"], "|", idea.get("model", "ltx25"), len(idea.get("shots") or [1]), "shot(s) |", idea["punchline"])
        return 0
    if not whitepc_online():
        log("White-PC offline, trying wake")
        if not wake_whitepc() and not whitepc_online():
            telegram(s["tg"], "xpost: White-PC is offline and did not wake; skipping this hour's render.")
            return 0
    try:
        job_id, filename = render(idea)
        media_url, size = fetch_and_host(job_id, filename)
    except Exception as e:
        log(f"RENDER FAILED {idea['slug']}: {e}")
        telegram(s["tg"], f"xpost render failed for {idea['slug']}: {str(e)[:200]}")
        return 0
    st["used"].append(idea["slug"])
    st["queue"].append({"slug": idea["slug"], "text": idea["punchline"], "media_url": media_url, "job_id": job_id,
                        "rendered_at": now(), "size": size})
    save_state(st)
    log(f"RENDERED {idea['slug']} -> {media_url} ({size} bytes); queue={len(st['queue'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
