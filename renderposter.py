#!/usr/bin/env python3
"""renderposter: posts PeakCut clips to YouTube from Render while Cloud Shell is off.

It reads the queue, accounts and clips that pcsync copied to Aiven and Backblaze. It only posts when Cloud Shell has
been silent for a few minutes, and claims each post with a compare-and-swap, so a post can never go out twice.

Settings (environment variables on Render, or ~/.pc-secrets): PG_URI, B2_KEY_ID, B2_APP_KEY, B2_BUCKET.
Commands:  run | once | dry-run
"""
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcsync  # noqa: E402

WRITER = "render"
EVERY = 20
GOOGLE_TOKEN_URL = os.environ.get("PC_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
YT_UPLOAD_URL = os.environ.get("PC_YT_UPLOAD_URL", "https://www.googleapis.com/upload/youtube/v3/videos")
CREATOR_DEFAULTS = {"privacy": "public", "post_footer": "", "post_gap_min": 45, "daily_cap": 6}
STATUS = {"line": "starting", "at": time.time()}
log = pcsync.log


def say(msg):
    STATUS.update(line=msg, at=time.time())
    log(msg)


# ------------------------------------------------------------------ reading the shared store
def load(meta, path, default):
    row = meta.get(path)
    if not row or row[0] is None or row[3]:
        return default, (row[1] if row else 0)
    try:
        return json.loads(row[0]), row[1]
    except ValueError:
        return default, row[1]


def save(meta, path, obj, base) -> bool:
    text = json.dumps(obj, indent=1)
    return meta.put(path, "small", text, len(text), hashlib.sha1(text.encode("utf-8")).hexdigest(), WRITER, base)


def find_job(meta, job_id):
    """(path of its job.json, the job) for a job id."""
    for path, text, _v in meta.rows("%/job.json"):
        if f'"{job_id}"' not in (text or ""):
            continue
        try:
            j = json.loads(text)
        except ValueError:
            continue
        if j.get("id") == job_id:
            return path, j
    return None, None


# ------------------------------------------------------------------ YouTube
def check(resp_fn, what):
    try:
        return resp_fn()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{what} failed ({e.code}): {e.read().decode('utf-8', 'replace')[:300]}") from None


def youtube_token(acc, apps):
    """(access token, True if it was just renewed)."""
    if (acc.get("expires") or 0) > time.time() and acc.get("access_token"):
        return acc["access_token"], False
    g = (apps or {}).get("google") or {}
    cid = g.get("id") or os.environ.get("GOOGLE_CLIENT_ID", "")
    secret = g.get("secret") or os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not (cid and secret and acc.get("refresh_token")):
        raise RuntimeError("The YouTube sign-in can't be renewed here: the app keys or refresh token are missing.")
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "refresh_token": acc["refresh_token"],
                                   "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=body, method="POST")
    tok = json.loads(check(lambda: urllib.request.urlopen(req, timeout=30).read(), "Refreshing the YouTube sign-in"))
    acc.update(access_token=tok["access_token"], expires=time.time() + tok.get("expires_in", 3600) - 60)
    return acc["access_token"], True


def youtube_upload(token, path: Path, title, description, tags, privacy):
    meta = {"snippet": {"title": re.sub(r"[<>]", "", title)[:100] or "Clip", "description": re.sub(r"[<>]", "", description)[:4900],
                        "tags": tags[:15], "categoryId": "20"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    size = path.stat().st_size
    start = urllib.request.Request(YT_UPLOAD_URL + "?uploadType=resumable&part=snippet,status", data=json.dumps(meta).encode(), method="POST",
                                   headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                                            "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)})
    loc = check(lambda: urllib.request.urlopen(start, timeout=60).headers["Location"], "Starting the YouTube upload")
    with open(path, "rb") as f:
        put = urllib.request.Request(loc, data=f, method="PUT", headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4",
                                                                         "Content-Length": str(size)})
        done = json.loads(check(lambda: urllib.request.urlopen(put, timeout=900).read(), "Uploading to YouTube"))
    return done["id"], f"https://www.youtube.com/shorts/{done['id']}"


def caption(clip, creator):
    tags = " ".join("#" + h for h in clip.get("hashtags", []))
    return "\n\n".join(x for x in (clip.get("description", ""), tags, creator.get("post_footer", "")) if x)


# ------------------------------------------------------------------ one cycle
def cloudshell_running(meta) -> bool:
    if os.environ.get("PC_IGNORE_HEARTBEAT") == "1":     # for testing the posting path while Cloud Shell is on
        return False
    a = meta.age(pcsync.HEARTBEAT)
    return a is not None and a < pcsync.GRACE


def waiting(meta, posts, now):
    """Due YouTube posts, oldest first. Other platforms stay queued for Cloud Shell."""
    out = []
    for p in sorted(posts, key=lambda p: p.get("due_at") or 0):
        if p.get("status") != "scheduled" or (p.get("due_at") or 0) > now or not p.get("user") or p.get("platform") != "youtube":
            continue
        settings, _v = load(meta, f"users/{p['user']}/accounts/settings.json", {})
        if settings.get("paused"):
            continue
        out.append(p)
    return out


def gap_ok(posts, p, creator, now) -> bool:
    """Same rule as the timer in the app: not within 'minutes between posts' of the last one on this account, and not past the daily cap."""
    mine = [q for q in posts if q.get("account") == p["account"] and q.get("status") == "posted" and q.get("posted_at")]
    last = max((q["posted_at"] for q in mine), default=0)
    if now - last < float(creator.get("post_gap_min", 45)) * 60:
        return False
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    return sum(1 for q in mine if time.strftime("%Y-%m-%d", time.localtime(q["posted_at"])) == today) < int(creator.get("daily_cap", 6))


def claim(meta, post_id):
    """Mark the post 'posting' in the shared queue. Only one server can win this: False if anything changed meanwhile."""
    posts, ver = load(meta, "posts.json", [])
    p = next((x for x in posts if x.get("id") == post_id), None)
    if p is None or p.get("status") != "scheduled":
        return None
    p["status"] = "posting"
    return (posts, ver + 1) if save(meta, "posts.json", posts, ver) else None


def finish(meta, post_id, **fields):
    for _ in range(6):
        posts, ver = load(meta, "posts.json", [])
        p = next((x for x in posts if x.get("id") == post_id), None)
        if p is None:
            return False
        p.update(fields)
        if save(meta, "posts.json", posts, ver):
            return True
        time.sleep(0.2)
    return False


def post_one(meta, blobs, p, tmpdir, dry=False):
    job_path, job = find_job(meta, p["job"])
    clip = next((c for c in (job or {}).get("clips", []) if c.get("id") == p["clip"]), None)
    uid = p["user"]
    accs, accs_ver = load(meta, f"users/{uid}/accounts/accounts.json", [])
    acc = next((a for a in accs if a.get("id") == p["account"]), None)
    if dry:
        say(f"would post '{(clip or {}).get('title') or p['id']}' to {acc and acc.get('name')}")
        return
    got = claim(meta, p["id"])
    if not got:
        return
    meta.touch(pcsync.BUSY, WRITER)
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        creator, _v = load(meta, f"users/{uid}/creators/{job.get('creator')}/creator.json", {})
        creator = {**CREATOR_DEFAULTS, **creator}
        rel = f"{job_path.rsplit('/', 1)[0]}/clips/{clip['file']}"
        dest = Path(tmpdir) / (p["id"] + ".mp4")
        blobs.get("data/" + rel, dest)
        apps, _a = load(meta, "apps.json", {})
        token, renewed = youtube_token(acc, apps)
        if renewed:
            for _ in range(4):
                fresh, v = load(meta, f"users/{uid}/accounts/accounts.json", [])
                for a in fresh:
                    if a.get("id") == acc["id"]:
                        a["access_token"], a["expires"] = acc["access_token"], acc["expires"]
                if save(meta, f"users/{uid}/accounts/accounts.json", fresh, v):
                    break
        title = clip.get("title") or clip.get("hook") or "Clip"
        rid, url = youtube_upload(token, dest, title, caption(clip, creator) + "\n#Shorts", clip.get("hashtags", []), creator["privacy"])
        finish(meta, p["id"], status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
        say(f"posted '{title}' to {acc['name']}")
    except Exception as e:  # noqa: BLE001
        posts, _v = load(meta, "posts.json", [])
        cur = next((x for x in posts if x.get("id") == p["id"]), {})
        tries = (cur.get("tries") or 0) + 1
        if tries < 3 and clip and acc:
            finish(meta, p["id"], status="scheduled", tries=tries, error=str(e)[-300:], due_at=time.time() + 600)
        else:
            finish(meta, p["id"], status="failed", tries=tries, error=str(e)[-300:])
        say(f"post failed ({tries}): {str(e)[:120]}")
    finally:
        meta.clear(pcsync.BUSY)
        try:
            Path(tmpdir, p["id"] + ".mp4").unlink(missing_ok=True)
        except OSError:
            pass


def cycle(meta, blobs, dry=False, now=None) -> int:
    now = now or time.time()
    if cloudshell_running(meta):
        say("Cloud Shell is running, it does the posting")
        return 0
    posts, _v = load(meta, "posts.json", [])
    if dry:
        yt = [p for p in posts if p.get("status") == "scheduled" and p.get("platform") == "youtube"]
        nxt = min((p.get("due_at") or 0 for p in yt), default=None)
        say(f"queue: {len(yt)} YouTube posts scheduled" + (f", next due in {int((nxt - now) / 60)} min" if nxt else "")
            + f"; Cloud Shell last seen {int(meta.age(pcsync.HEARTBEAT) or -1)}s ago")
    posted = 0
    with tempfile.TemporaryDirectory() as tmp:
        for p in waiting(meta, posts, now):
            if cloudshell_running(meta):
                break
            creator, _c = load(meta, f"users/{p['user']}/creators/{(find_job(meta, p['job'])[1] or {}).get('creator')}/creator.json", {})
            if not gap_ok(posts, p, {**CREATOR_DEFAULTS, **creator}, now):
                continue
            post_one(meta, blobs, p, tmp, dry)
            posted += 1
            posts, _v = load(meta, "posts.json", [])
    if not posted:
        say("nothing to post")
    return posted


# ------------------------------------------------------------------ running it
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"renderposter: {STATUS['line']} ({int(time.time() - STATUS['at'])}s ago)\n".encode()
        self.send_response(200); self.send_header("Content-Type", "text/plain"); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    s = pcsync.load_secrets()
    if not all(s.get(k) for k in ("B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET", "PG_URI")):
        print("Missing settings: PG_URI, B2_KEY_ID, B2_APP_KEY, B2_BUCKET."); return 2
    if cmd in ("once", "dry-run"):
        meta = pcsync.Meta(s["PG_URI"])
        try:
            cycle(meta, pcsync.open_blobs(s), dry=(cmd == "dry-run"))
        finally:
            meta.close()
        return 0
    port = int(os.environ.get("PORT") or "10000")
    threading.Thread(target=lambda: ThreadingHTTPServer(("0.0.0.0", port), Health).serve_forever(), daemon=True).start()
    say(f"started, checking every {EVERY} seconds")
    blobs = pcsync.open_blobs(s)
    while True:
        try:
            meta = pcsync.Meta(s["PG_URI"])
            try:
                cycle(meta, blobs)
            finally:
                meta.close()
        except Exception as e:  # noqa: BLE001
            say("problem, will retry: " + str(e)[:150])
        time.sleep(EVERY)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
