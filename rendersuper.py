#!/usr/bin/env python3
"""rendersuper: runs on Render. Serves the real PeakCut while Cloud Shell is off, and steps aside the moment Cloud Shell is back.

  passive   Cloud Shell is serving. Render only answers a status page (and the keep-awake pings).
  active    Cloud Shell has been silent for a few minutes. Render pulls everything the other server saved, starts PeakCut in light
            mode (shows and posts, does not process videos or record) and copies its changes back every 30 seconds.
  fallback  PeakCut would not stay up here. Only the small YouTube poster runs, until Cloud Shell is back.

Settings (Render environment variables): PG_URI, B2_KEY_ID, B2_APP_KEY, B2_BUCKET, PEAKCUT_PUBLIC_URL, PEAKCUT_AI_*.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("PC_RENDER_DATA") or "/tmp/pcdata")
os.environ.setdefault("PCSYNC_DATA", str(DATA))              # these three are read when pcsync is imported
os.environ.setdefault("PCSYNC_STATE", "/tmp/pcsync-state.json")
os.environ.setdefault("PCSYNC_SNAP", "/tmp/pcsync-db.snapshot")
sys.path.insert(0, str(HERE))
import pcsync  # noqa: E402
import renderposter  # noqa: E402
import takeover as tk  # noqa: E402

WRITER = "render"
SRC = HERE / "peakcut-src"
RELEASE_WITHIN = 60          # Cloud Shell heard from this recently: hand over
SYNC_EVERY = 30
TICK = 15
MAX_CRASHES = 3              # PeakCut stopping this often within 10 minutes: give up and use the small poster
say = renderposter.say


class HealthServer:
    def __init__(self, port):
        self.port, self.srv = port, None

    def start(self):
        if self.srv:
            return
        for _ in range(20):
            try:
                self.srv = ThreadingHTTPServer(("0.0.0.0", self.port), renderposter.Health)
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("the port is still busy")
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None


class Runner:
    """PeakCut itself, as a child process."""

    def __init__(self, src, data, port, public_url):
        self.src, self.data, self.port, self.public_url, self.p = src, data, port, public_url, None

    def start(self):
        env = dict(os.environ, PEAKCUT_DATA=str(self.data), PEAKCUT_HOST="0.0.0.0", PEAKCUT_PORT=str(self.port), PEAKCUT_LIGHT="1",
                   PEAKCUT_FETCH_MODEL="0", PEAKCUT_ACCOUNTS="1", PEAKCUT_TRUST_PROXY="1", PEAKCUT_PUBLIC_URL=self.public_url,
                   PYTHONUNBUFFERED="1")
        self.p = subprocess.Popen([sys.executable, "-m", "studio.server"], cwd=str(self.src), env=env)

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def stop(self, wait=25):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(wait)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(10)
        self.p = None


class Supervisor:
    def __init__(self, open_meta, blobs, data, runner, health, clock=time.time, sleep=time.sleep):
        self.open_meta, self.blobs, self.data, self.runner, self.health = open_meta, blobs, Path(data), runner, health
        self.clock, self.sleep = clock, sleep
        self.state, self.last_sync, self.crashes = "passive", 0.0, []

    def tick(self) -> str:
        meta = self.open_meta()
        try:
            return getattr(self, "_" + self.state)(meta)
        finally:
            meta.close()

    # ---- passive: Cloud Shell has the turn
    def _passive(self, meta):
        age = meta.age(pcsync.HEARTBEAT)
        if age is None or age > pcsync.GRACE:
            return self.take_over(meta)
        say("passive: Cloud Shell is serving")
        return "passive"

    def take_over(self, meta):
        say("Cloud Shell is off: taking over")
        self.health.stop()
        res = tk.pull_all(meta, self.blobs, self.data, WRITER, fetch_blobs=False)
        for e in res["errors"][:3]:
            pcsync.log("problem:", e)
        self.runner.start()
        meta.touch(tk.SERVING, WRITER)
        self.state, self.last_sync, self.crashes = "active", self.clock(), []
        return "took over"

    # ---- active: Render serves PeakCut
    def _active(self, meta):
        meta.touch(tk.SERVING, WRITER)
        age = meta.age(pcsync.HEARTBEAT)
        if age is not None and age < RELEASE_WITHIN:
            return self.release(meta)
        now = self.clock()
        if not self.runner.alive():
            self.crashes = [t for t in self.crashes if now - t < 600] + [now]
            if len(self.crashes) > MAX_CRASHES:
                return self.give_up(meta)
            say("PeakCut stopped, starting it again")
            self.runner.start()
            return "restarted"
        if now - self.last_sync >= SYNC_EVERY:
            self.last_sync = now
            r = pcsync.sync_once(meta, self.blobs, WRITER, self.data)
            for e in r["errors"][:3]:
                pcsync.log("problem:", e)
        try:
            tk.fetch_some(meta, self.blobs, self.data, limit=3)      # clips arrive a few at a time
        except Exception as e:  # noqa: BLE001
            pcsync.log("clip download:", str(e)[:100])
        say("active: serving PeakCut")
        return "active"

    def waiting_posts(self):
        try:
            return any(p.get("status") == "posting" for p in json.loads((self.data / "posts.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return False

    def release(self, meta):
        say("Cloud Shell is back: handing over")
        deadline = self.clock() + 600
        while self.waiting_posts() and self.clock() < deadline:     # let a running upload finish
            self.sleep(5)
            meta.touch(tk.SERVING, WRITER)
        self.runner.stop()
        for _ in range(2):
            r = pcsync.sync_once(meta, self.blobs, WRITER, self.data, force_db=True)
            for e in r["errors"][:3]:
                pcsync.log("problem:", e)
        meta.touch(tk.RELEASED, WRITER)
        meta.clear(tk.SERVING)
        self.health.start()
        self.state = "passive"
        return "released"

    # ---- fallback: PeakCut would not stay up
    def give_up(self, meta):
        say("PeakCut keeps stopping here: using the small poster until Cloud Shell is back")
        self.runner.stop()
        r = pcsync.sync_once(meta, self.blobs, WRITER, self.data, force_db=True)
        meta.clear(tk.SERVING)
        self.health.start()
        self.state = "fallback"
        return "fallback"

    def _fallback(self, meta):
        age = meta.age(pcsync.HEARTBEAT)
        if age is not None and age < RELEASE_WITHIN:
            self.state = "passive"
            return "passive"
        renderposter.cycle(meta, self.blobs)
        return "fallback"

    def shutdown(self):
        """Render is stopping this service (a new deploy): save everything first."""
        if self.state == "active":
            meta = self.open_meta()
            try:
                self.release(meta)
            finally:
                meta.close()


def main():
    s = pcsync.load_secrets()
    if not all(s.get(k) for k in ("B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET", "PG_URI")):
        print("Missing settings: PG_URI, B2_KEY_ID, B2_APP_KEY, B2_BUCKET."); return 2
    port = int(os.environ.get("PORT") or "10000")
    DATA.mkdir(parents=True, exist_ok=True)
    public = os.environ.get("PEAKCUT_PUBLIC_URL") or "https://mypeakcut.dpdns.org"
    health = HealthServer(port)
    health.start()
    sup = Supervisor(lambda: pcsync.Meta(s["PG_URI"]), pcsync.open_blobs(s), DATA, Runner(SRC, DATA, port, public), health)
    signal.signal(signal.SIGTERM, lambda *a: (sup.shutdown(), sys.exit(0)))
    say(f"started, checking every {TICK} seconds")
    while True:
        try:
            sup.tick()
        except Exception as e:  # noqa: BLE001
            say("problem, will retry: " + str(e)[:150])
        time.sleep(TICK)


if __name__ == "__main__":
    sys.exit(main())
