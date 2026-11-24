"""Settings > Server & cookies. Added to PeakCut by patch_peakcut.py (studio/pcserver.py).

  GET    /api/pc/server    which server is serving, whether each one is running, the choice, and the cookies status
  POST   /api/pc/server    {"prefer": "auto" | "cloudshell" | "render"}
  POST   /api/pc/cookies   {"text": "<cookies.txt>"}  saved in the shared data; a new upload replaces the old one
  DELETE /api/pc/cookies   removes them

Admins only. The cookies are kept in data/yt-cookies.json, which the sync tool copies to the shared database, so both servers use the same file.
"""
import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

PREFERENCES = ("auto", "cloudshell", "render")
FRESH = 60          # seconds: a signal newer than this means "running"
MAX_TEXT = 400_000


# ------------------------------------------------------------------ where things are
def data_dir() -> Path:
    if os.environ.get("PEAKCUT_DATA"):
        return Path(os.environ["PEAKCUT_DATA"])
    return Path(__file__).resolve().parent.parent / "data"


def cookies_json() -> Path:
    return data_dir() / "yt-cookies.json"


def cookies_txt() -> Path:
    return data_dir() / "yt-cookies.txt"


def this_server() -> str:
    return "render" if os.environ.get("PEAKCUT_LIGHT") == "1" else "cloudshell"


def _pcsync():
    """The sync tool lives next to PeakCut (on Render) or in ~/bin (on Cloud Shell)."""
    here = Path(__file__).resolve()
    for d in (Path.home() / "bin", here.parents[2], here.parents[1]):
        if (d / "pcsync.py").is_file():
            if str(d) not in sys.path:
                sys.path.insert(0, str(d))
            import pcsync
            return pcsync
    return None


# ------------------------------------------------------------------ cookies
def parse_cookies(text: str):
    """What was pasted or uploaded, as a proper cookies file. (text, count) or (None, reason)."""
    out, n, google = ["# Netscape HTTP Cookie File"], 0, False
    for raw in (text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        f = re.split(r"\s+", line, maxsplit=6)               # pasting turns tabs into spaces: split on any whitespace
        if len(f) < 7 or not re.fullmatch(r"-?\d+", f[4]):
            continue
        if f[3].upper() not in ("TRUE", "FALSE") or f[1].upper() not in ("TRUE", "FALSE"):
            continue
        out.append("\t".join(f))
        n += 1
        google = google or "youtube.com" in f[0] or "google.com" in f[0]
    if n == 0:
        return None, "That doesn't look like a cookies.txt file (no cookie lines found)."
    if not google:
        return None, "These cookies are not for YouTube or Google. Export them while you are on youtube.com."
    return "\n".join(out) + "\n", n


def cookies_info() -> dict:
    j = cookies_json()
    if not j.is_file():
        return {"present": False}
    try:
        d = json.loads(j.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"present": False}
    if not d.get("netscape"):
        return {"present": False}
    return {"present": True, "count": d.get("count", 0), "age_days": round((time.time() - d.get("saved_at", time.time())) / 86400, 1),
            "saved_on": d.get("saved_on", ""), "accepted": d.get("accepted")}


def save_cookies(text: str, count: int, accepted=None):
    j = cookies_json()
    j.parent.mkdir(parents=True, exist_ok=True)
    body = {"netscape": text, "count": count, "saved_at": time.time(), "saved_on": this_server(), "accepted": accepted}
    tmp = j.with_suffix(".tmp")
    tmp.write_text(json.dumps(body), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(j)


def clear_cookies():
    """Removing writes an empty file instead of deleting it, so the removal reaches the other server too."""
    save_cookies("", 0)
    for p in (cookies_txt(), Path.home() / "yt-cookies.txt"):
        try:
            p.unlink()
        except OSError:
            pass


def cookiefile():
    """The cookies file for downloads: made from the shared copy, or the older file in the home folder."""
    try:
        j = cookies_json()
        if j.is_file():
            d = json.loads(j.read_text(encoding="utf-8"))
            if not d.get("netscape"):
                return None                                   # removed on purpose
            t = cookies_txt()
            if not t.is_file() or t.stat().st_mtime < j.stat().st_mtime:
                t.write_text(d["netscape"], encoding="utf-8")
                t.chmod(0o600)
            return str(t)
    except (OSError, ValueError):
        pass
    old = Path.home() / "yt-cookies.txt"
    return str(old) if old.is_file() else None


def install_downloader_hook():
    """Makes every yt-dlp call in PeakCut use the shared cookies."""
    try:
        import yt_dlp
    except ImportError:
        return False
    if getattr(yt_dlp.YoutubeDL, "_pc_cookies", False):
        return True
    orig = yt_dlp.YoutubeDL.__init__

    def init(self, params=None, auto_init=True):
        params = dict(params or {})
        f = cookiefile()
        if f:
            params["cookiefile"] = f
        orig(self, params, auto_init)

    yt_dlp.YoutubeDL.__init__ = init
    yt_dlp.YoutubeDL._pc_cookies = True
    return True


def test_cookies():
    """Asks YouTube for the account's watch history through the cookies. (accepted, message)."""
    f = cookiefile()
    if not f:
        return None, "No cookies to test."
    cmd = shlex.split(os.environ["PCX_YTDLP"]) if os.environ.get("PCX_YTDLP") else [sys.executable, "-m", "yt_dlp"]
    cmd += ["--cookies", f, "--flat-playlist", "--playlist-end", "1", "--print", "title", ":ythistory"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, f"Saved, but the test could not run ({type(e).__name__})."
    if "ERROR" in (r.stderr or "") or r.returncode != 0:
        return False, "Saved, but YouTube did not accept them. Export them again from a fresh private tab."
    return True, "YouTube accepted the cookies."


# ------------------------------------------------------------------ the shared state
def server_state() -> dict:
    out = {"this": this_server(), "shared": False, "prefer": "auto", "cookies": cookies_info(),
           "cloudshell": "unknown", "render": "unknown", "serving": this_server()}
    pc = _pcsync()
    if not pc:
        out["error"] = "The sync tool is not installed here."
        return out
    try:
        s = pc.load_secrets()
        meta = pc.Meta(s["PG_URI"])
    except Exception as e:  # noqa: BLE001
        out["error"] = f"The shared database is not reachable ({type(e).__name__})."
        return out
    try:
        age = meta.age
        out["shared"] = True
        out["prefer"] = meta.getv(pc.PREFER, "auto")
        hb, loop = age(pc.HEARTBEAT), age(pc.LOOP)
        serving, rloop = age("__serving__:render"), age(pc.RENDER_LOOP)
        out["cloudshell"] = "serving" if hb is not None and hb < pc.GRACE else ("standing by" if loop is not None and loop < FRESH else "off")
        out["render"] = "serving" if serving is not None and serving < FRESH else ("standing by" if rloop is not None and rloop < FRESH else "off")
        if out["this"] == "render":
            out["render"] = "serving"
        else:
            out["cloudshell"] = "serving"
        out["serving"] = "cloudshell" if out["cloudshell"] == "serving" else "render"
    except Exception as e:  # noqa: BLE001
        out["shared"] = False
        out["error"] = f"Could not read the shared state ({type(e).__name__})."
    finally:
        meta.close()
    return out


def set_preference(value: str):
    pc = _pcsync()
    if not pc:
        raise RuntimeError("The sync tool is not installed here.")
    s = pc.load_secrets()
    meta = pc.Meta(s["PG_URI"])
    try:
        meta.setv(pc.PREFER, value, this_server())
    finally:
        meta.close()


# ------------------------------------------------------------------ the routes
def register(app, admin):
    from fastapi import HTTPException, Request

    @app.get("/api/pc/server")
    async def pc_server(request: Request):
        admin(request)
        return await asyncio.to_thread(server_state)

    @app.post("/api/pc/server")
    async def pc_server_set(request: Request):
        admin(request)
        try:
            d = await request.json()
        except ValueError:
            raise HTTPException(400, "Bad request.")
        value = (d or {}).get("prefer")
        if value not in PREFERENCES:
            raise HTTPException(400, "Choose Auto, Cloud Shell or Render.")
        try:
            await asyncio.to_thread(set_preference, value)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, f"Could not save the choice: {e}")
        return await asyncio.to_thread(server_state)

    @app.post("/api/pc/cookies")
    async def pc_cookies_save(request: Request):
        admin(request)
        try:
            d = await request.json()
        except ValueError:
            raise HTTPException(400, "Bad request.")
        text = str((d or {}).get("text") or "")
        if not text.strip() or len(text) > MAX_TEXT:
            raise HTTPException(400, "The cookies are empty or too big.")
        parsed, count = parse_cookies(text)
        if parsed is None:
            raise HTTPException(400, count)
        await asyncio.to_thread(save_cookies, parsed, count)
        if this_server() == "render":
            return {"ok": True, "count": count, "accepted": None,
                    "message": "Saved. They are tested the next time Cloud Shell runs, because downloads happen there."}
        accepted, msg = await asyncio.to_thread(test_cookies)       # up to a minute: must not block PeakCut
        await asyncio.to_thread(save_cookies, parsed, count, accepted)
        return {"ok": True, "count": count, "accepted": accepted, "message": msg}

    @app.delete("/api/pc/cookies")
    async def pc_cookies_remove(request: Request):
        admin(request)
        await asyncio.to_thread(clear_cookies)
        return {"ok": True}

    install_downloader_hook()
