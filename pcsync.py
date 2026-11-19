#!/usr/bin/env python3
"""pcsync: copies PeakCut's data folder to a shared place, without touching PeakCut itself.

Small files (settings, jobs, the post queue, connected accounts) go to a Postgres table on Aiven.
Clips and avatars go to a Backblaze B2 bucket.

Commands:  selftest | once | loop | pull | status | forget | wipe-remote
Settings are read from ~/.pc-secrets (B2_KEY_ID, B2_APP_KEY, B2_BUCKET, PG_URI, optional SERVER_NAME).
"""
import base64
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
SECRETS = Path(os.environ.get("PCSYNC_SECRETS") or HOME / ".pc-secrets")
STATE = Path(os.environ.get("PCSYNC_STATE") or HOME / ".pcsync-state.json")
DATA = Path(os.environ.get("PCSYNC_DATA") or HOME / "peakcut" / "data")

SMALL_MAX = 2_000_000            # bigger json files are not copied
BLOB_MAX = 300_000_000           # bigger clips are not copied
SMALL_SKIP = {"words.json", "words_en.json"}     # big transcripts, only needed to re-make a clip
BLOB_EXT = {".mp4", ".jpg", ".jpeg", ".png", ".webp"}
EVERY = 30                       # seconds between scans
MASS_DELETE = 0.5                # never delete more than this share of what is tracked in one scan
HEARTBEAT = "__heartbeat__:cloudshell"   # touched while PeakCut runs on Cloud Shell: Render stays out of the way
BUSY = "__busy__:render"                 # touched while Render is in the middle of a post
GRACE = 180                              # seconds of silence before Render takes over posting


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_secrets() -> dict:
    s = {}
    try:
        for line in SECRETS.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                s[k.strip()] = v.strip()
    except OSError:
        pass
    for k in ("B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET", "PG_URI", "SERVER_NAME"):
        if os.environ.get(k):
            s[k] = os.environ[k]
    s.setdefault("SERVER_NAME", "cloudshell")
    return s


# ------------------------------------------------------------------ what gets copied
def classify(rel: str, size: int):
    """'small', 'blob' or None for a path relative to the data folder (forward slashes)."""
    parts = rel.split("/")
    name = parts[-1]
    if parts[0] in ("tmp", "models", "fonts") or name.endswith((".tmp", ".db", ".db-wal", ".db-shm")):
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext in (".json", ".jsonl"):
        return "small" if size <= SMALL_MAX and name not in SMALL_SKIP else None
    if ext in BLOB_EXT and "clips" in parts[:-1] and size <= BLOB_MAX:
        return "blob"
    if name.startswith("avatar-") and size <= 5_000_000:
        return "blob"
    return None


def scan(data: Path) -> dict:
    out = {}
    if not data.is_dir():
        return out
    for root, dirs, files in os.walk(data):
        dirs[:] = [d for d in dirs if d not in ("tmp", "models", "fonts", "buffer", "__pycache__")]
        for f in files:
            p = Path(root) / f
            try:
                st = p.stat()
            except OSError:
                continue
            rel = p.relative_to(data).as_posix()
            kind = classify(rel, st.st_size)
            if kind:
                out[rel] = (kind, st.st_mtime_ns, st.st_size)
    return out


def sha1_of(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ the small-file table (Postgres, or sqlite in tests)
class Meta:
    def __init__(self, uri: str):
        self.sqlite = uri.startswith("sqlite:")
        if self.sqlite:
            self.con = sqlite3.connect(uri.split("sqlite:///", 1)[1], isolation_level=None)
            self.ph = "?"
        else:
            import psycopg
            self.con = psycopg.connect(uri, connect_timeout=20, autocommit=True)
            self.ph = "%s"
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS pc_files ("
            "path TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT, size BIGINT NOT NULL DEFAULT 0, sha1 TEXT, "
            "version BIGINT NOT NULL DEFAULT 0, writer TEXT, updated DOUBLE PRECISION, deleted INTEGER NOT NULL DEFAULT 0)")

    def q(self, sql: str, args=()):
        cur = self.con.execute(sql.replace("?", self.ph), args)
        return cur

    def row(self, path):
        cur = self.q("SELECT version, writer, deleted FROM pc_files WHERE path = ?", (path,))
        return cur.fetchone()

    def put(self, path, kind, data, size, sha1, writer, base) -> bool:
        cur = self.q(
            "INSERT INTO pc_files (path, kind, data, size, sha1, version, writer, updated, deleted) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, 0) "
            "ON CONFLICT (path) DO UPDATE SET kind = EXCLUDED.kind, data = EXCLUDED.data, size = EXCLUDED.size, "
            "sha1 = EXCLUDED.sha1, version = pc_files.version + 1, writer = EXCLUDED.writer, "
            "updated = EXCLUDED.updated, deleted = 0 WHERE pc_files.version = ?",
            (path, kind, data, size, sha1, writer, time.time(), base))
        return cur.rowcount == 1

    def mark_deleted(self, path, writer):
        self.q("UPDATE pc_files SET deleted = 1, data = NULL, version = version + 1, writer = ?, updated = ? "
               "WHERE path = ?", (writer, time.time(), path))

    def counts(self):
        cur = self.q("SELECT kind, COUNT(*), COALESCE(SUM(size), 0) FROM pc_files WHERE deleted = 0 AND kind != 'meta' GROUP BY kind")
        return cur.fetchall()

    def all_paths(self):
        return [(r[0], r[1]) for r in self.q("SELECT path, kind FROM pc_files").fetchall()]

    def get(self, path):
        """(text, version, writer, deleted) or None."""
        return self.q("SELECT data, version, writer, deleted FROM pc_files WHERE path = ?", (path,)).fetchone()

    def rows(self, like):
        """(path, text, version) of small files whose path matches the LIKE pattern."""
        return self.q("SELECT path, data, version FROM pc_files WHERE kind = 'small' AND deleted = 0 AND path LIKE ? ORDER BY path",
                      (like,)).fetchall()

    def touch(self, key, writer):
        self.q("INSERT INTO pc_files (path, kind, data, size, version, writer, updated, deleted) VALUES (?, 'meta', NULL, 0, 1, ?, ?, 0) "
               "ON CONFLICT (path) DO UPDATE SET updated = EXCLUDED.updated, writer = EXCLUDED.writer, version = pc_files.version + 1",
               (key, writer, time.time()))

    def clear(self, key):
        self.q("DELETE FROM pc_files WHERE path = ?", (key,))

    def age(self, key):
        r = self.q("SELECT updated FROM pc_files WHERE path = ?", (key,)).fetchone()
        return None if not r or r[0] is None else time.time() - float(r[0])

    def wipe(self):
        self.q("DELETE FROM pc_files")

    def close(self):
        try:
            self.con.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ the clip store: Backblaze B2 (native API), or a folder in tests
class B2:
    AUTH = "https://api.backblazeb2.com/b2api/v2/b2_authorize_account"

    def __init__(self, key_id, app_key, bucket, auth_url=None):
        self.key_id, self.app_key, self.bucket = key_id, app_key, bucket
        self.auth_url = auth_url or self.AUTH
        self.tok = None

    def _call(self, req, retry=True):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code == 401 and retry and self.tok:
                self.tok = None
                self.login()
                req.remove_header("Authorization")
                req.add_header("Authorization", self.tok["authorizationToken"])
                return self._call(req, retry=False)
            raise RuntimeError(f"B2 {e.code}: {body}") from None

    def login(self):
        basic = base64.b64encode(f"{self.key_id}:{self.app_key}".encode()).decode()
        req = urllib.request.Request(self.auth_url, headers={"Authorization": "Basic " + basic})
        with urllib.request.urlopen(req, timeout=60) as r:
            self.tok = json.loads(r.read())
        self.bucket_id = None

    def api(self, name, body):
        if not self.tok:
            self.login()
        req = urllib.request.Request(self.tok["apiUrl"] + "/b2api/v2/" + name, data=json.dumps(body).encode(),
                                     headers={"Authorization": self.tok["authorizationToken"]}, method="POST")
        return json.loads(self._call(req))

    def bid(self):
        if not self.tok:
            self.login()
        if not getattr(self, "bucket_id", None):
            r = self.api("b2_list_buckets", {"accountId": self.tok["accountId"], "bucketName": self.bucket})
            found = [b for b in r["buckets"] if b["bucketName"] == self.bucket]
            if not found:
                raise RuntimeError(f"Backblaze bucket '{self.bucket}' was not found")
            self.bucket_id = found[0]["bucketId"]
        return self.bucket_id

    def put(self, name: str, path: Path):
        size = path.stat().st_size
        sha1 = sha1_of(path)
        up = self.api("b2_get_upload_url", {"bucketId": self.bid()})
        with open(path, "rb") as f:
            req = urllib.request.Request(up["uploadUrl"], data=f, method="POST", headers={
                "Authorization": up["authorizationToken"], "X-Bz-File-Name": urllib.parse.quote(name, safe="/"),
                "Content-Type": "b2/x-auto", "Content-Length": str(size), "X-Bz-Content-Sha1": sha1})
            self._call(req, retry=False)
        self.prune(name, keep_latest=True)
        return sha1

    def versions(self, name):
        out, body = [], {"bucketId": self.bid(), "startFileName": name, "prefix": name, "maxFileCount": 100}
        while True:
            r = self.api("b2_list_file_versions", body)
            out += [f for f in r["files"] if f["fileName"] == name]
            if r.get("nextFileName") == name and r.get("nextFileId"):
                body = dict(body, startFileId=r["nextFileId"])
                continue
            return out

    def prune(self, name, keep_latest):
        vs = sorted(self.versions(name), key=lambda f: f.get("uploadTimestamp", 0), reverse=True)
        for f in vs[1 if keep_latest else 0:]:
            self.api("b2_delete_file_version", {"fileName": name, "fileId": f["fileId"]})

    def delete(self, name):
        self.prune(name, keep_latest=False)

    def get(self, name, dest: Path):
        if not self.tok:
            self.login()
        url = f"{self.tok['downloadUrl']}/file/{self.bucket}/{urllib.parse.quote(name, safe='/')}"
        req = urllib.request.Request(url, headers={"Authorization": self.tok["authorizationToken"]})
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        tmp.replace(dest)

    def count(self):
        n, size, start = 0, 0, None
        while True:
            body = {"bucketId": self.bid(), "maxFileCount": 1000}
            if start:
                body["startFileName"] = start
            r = self.api("b2_list_file_names", body)
            n += len(r["files"])
            size += sum(f.get("contentLength", 0) for f in r["files"])
            start = r.get("nextFileName")
            if not start:
                return n, size


class DirBlobs:
    """Stands in for B2 in tests: a plain folder."""

    def __init__(self, root):
        self.root = Path(root)

    def put(self, name, path: Path):
        d = self.root / name
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(path.read_bytes())
        return sha1_of(path)

    def delete(self, name):
        (self.root / name).unlink(missing_ok=True)

    def get(self, name, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((self.root / name).read_bytes())

    def count(self):
        files = [p for p in self.root.rglob("*") if p.is_file()]
        return len(files), sum(p.stat().st_size for p in files)


def open_blobs(s):
    if os.environ.get("PCSYNC_BLOBDIR"):
        return DirBlobs(os.environ["PCSYNC_BLOBDIR"])
    return B2(s["B2_KEY_ID"], s["B2_APP_KEY"], s["B2_BUCKET"])


# ------------------------------------------------------------------ one pass
def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"files": {}, "last": 0}


def save_state(st):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    tmp.replace(STATE)


def sync_once(meta: Meta, blobs, writer: str, data: Path = None) -> dict:
    data = data or DATA
    st = load_state()
    tracked = st["files"]
    now = scan(data)
    res = {"pushed": 0, "deleted": 0, "skipped": 0, "conflicts": [], "errors": []}

    for rel, (kind, mtime, size) in sorted(now.items()):
        old = tracked.get(rel)
        if old and old["mtime"] == mtime and old["size"] == size:
            continue
        p = data / rel
        try:
            if kind == "small":
                text = p.read_text(encoding="utf-8")
                sha = hashlib.sha1(text.encode("utf-8")).hexdigest()
                if old and old.get("sha1") == sha:
                    tracked[rel].update(mtime=mtime, size=size)
                    res["skipped"] += 1
                    continue
                payload = text
            else:
                sha = sha1_of(p)
                if old and old.get("sha1") == sha:
                    tracked[rel].update(mtime=mtime, size=size)
                    res["skipped"] += 1
                    continue
                blobs.put("data/" + rel, p)        # the file first, then the row that says it exists
                payload = None
            base = old["version"] if old else 0
            ok = meta.put(rel, kind, payload, size, sha, writer, base)
            if not ok:
                r = meta.row(rel)
                if r and r[1] == writer:           # our own earlier copy (the state file was lost): adopt it and go on
                    ok = meta.put(rel, kind, payload, size, sha, writer, r[0])
                    base = r[0]
                if not ok:
                    res["conflicts"].append(rel)
                    continue
            tracked[rel] = {"mtime": mtime, "size": size, "sha1": sha, "version": base + 1, "kind": kind}
            res["pushed"] += 1
        except Exception as e:  # noqa: BLE001
            res["errors"].append(f"{rel}: {e}")

    gone = [rel for rel in tracked if rel not in now]
    if gone and data.is_dir():
        if len(gone) > max(3, MASS_DELETE * len(tracked)):
            res["errors"].append(f"{len(gone)} of {len(tracked)} tracked files are missing here: "
                                 f"nothing was deleted. If you wiped PeakCut on purpose, run: pcsync forget")
        else:
            for rel in gone:
                try:
                    if tracked[rel].get("kind") == "blob":
                        blobs.delete("data/" + rel)
                    meta.mark_deleted(rel, writer)
                    del tracked[rel]
                    res["deleted"] += 1
                except Exception as e:  # noqa: BLE001
                    res["errors"].append(f"delete {rel}: {e}")

    st["last"] = time.time()
    save_state(st)
    return res


# ------------------------------------------------------------------ commands
def cmd_selftest(s) -> int:
    bad = 0
    try:
        m = Meta(s["PG_URI"])
        m.put("__selftest__", "small", '{"ok": true}', 12, "x", s["SERVER_NAME"], m.row("__selftest__")[0] if m.row("__selftest__") else 0)
        r = m.row("__selftest__")
        m.q("DELETE FROM pc_files WHERE path = ?", ("__selftest__",))
        m.close()
        print("Database (Aiven): OK, wrote and read a row, version", r[0])
    except Exception as e:  # noqa: BLE001
        print("Database (Aiven): FAILED", str(e)[:200]); bad += 1
    try:
        b = open_blobs(s)
        tmp = HOME / ".pcsync-selftest.bin"
        tmp.write_bytes(os.urandom(2048))
        want = sha1_of(tmp)
        b.put("data/__selftest__.bin", tmp)
        back = HOME / ".pcsync-selftest.back"
        b.get("data/__selftest__.bin", back)
        same = sha1_of(back) == want
        b.delete("data/__selftest__.bin")
        tmp.unlink(missing_ok=True); back.unlink(missing_ok=True)
        print("Storage (Backblaze): OK, uploaded, downloaded and deleted a test file" if same else "Storage (Backblaze): FAILED, the downloaded file differs")
        bad += 0 if same else 1
    except Exception as e:  # noqa: BLE001
        print("Storage (Backblaze): FAILED", str(e)[:200]); bad += 1
    return bad


def peakcut_alive() -> bool:
    if os.environ.get("PCSYNC_ALIVE"):
        return os.environ["PCSYNC_ALIVE"] == "1"
    port = os.environ.get("PEAKCUT_PORT") or "8765"
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3)
        return True
    except urllib.error.HTTPError:
        return True          # it answered, even if with an error page
    except Exception:  # noqa: BLE001
        return False


def run_once(s):
    meta = Meta(s["PG_URI"])
    try:
        res = sync_once(meta, open_blobs(s), s["SERVER_NAME"])
        if peakcut_alive():
            meta.touch(HEARTBEAT, s["SERVER_NAME"])
        return res
    finally:
        meta.close()


def report(res):
    if not (res["pushed"] or res["deleted"] or res["conflicts"] or res["errors"]):
        return
    msg = f"pushed {res['pushed']}, deleted {res['deleted']}, unchanged {res['skipped']}"
    if res["conflicts"]:
        msg += f", CONFLICTS {len(res['conflicts'])} (changed elsewhere: {res['conflicts'][:3]})"
    log(msg)
    for e in res["errors"][:5]:
        log("problem:", e)


# ------------------------------------------------------------------ taking over from Render
RANK = {"scheduled": 0, "posting": 1, "failed": 2, "cancelled": 3, "posted": 4, "deleted": 5}


def merge_posts(local: list, remote: list):
    """Take what Render did into the local post list. For a post both sides have, the one further along wins."""
    by = {p.get("id"): p for p in local}
    changed = []
    for r in remote:
        l = by.get(r.get("id"))
        if l is None:
            continue                     # a post only Render has: it was deleted here, leave it deleted
        rr, lr = RANK.get(r.get("status"), 0), RANK.get(l.get("status"), 0)
        if rr > lr or (rr == lr and (r.get("tries") or 0) > (l.get("tries") or 0)):
            l.clear()
            l.update(r)
            changed.append(l)
    return local, changed


def merge_accounts(local: list, remote: list) -> int:
    """Render may have renewed a YouTube sign-in: keep whichever access token lasts longer."""
    by = {a.get("id"): a for a in local}
    n = 0
    for r in remote:
        l = by.get(r.get("id"))
        if l is not None and (r.get("expires") or 0) > (l.get("expires") or 0):
            l["access_token"], l["expires"] = r.get("access_token"), r.get("expires")
            n += 1
    return n


def _write_json(p: Path, obj):
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(p)


def pull_from_render(meta: Meta, writer: str, data: Path = None, wait=True, sleep=time.sleep) -> dict:
    """Run before PeakCut starts on Cloud Shell. Tells Render to stand down, waits for any post Render is in the middle
    of, then brings Render's results (posted / failed, renewed sign-ins) into the local files."""
    data = data or DATA
    st = load_state()
    tracked = st["files"]
    out = {"posts": 0, "accounts": 0, "waited": 0}
    meta.touch(HEARTBEAT, writer)
    while wait:
        a = meta.age(BUSY)
        if a is None or a > 900 or out["waited"] >= 900:
            break
        log("Render is in the middle of a post, waiting...")
        sleep(10)
        out["waited"] += 10
        meta.touch(HEARTBEAT, writer)

    def adopt(rel, version):
        e = tracked.get(rel) or {"mtime": 0, "size": 0, "sha1": "", "kind": "small"}
        e["version"] = version           # our next push is then based on Render's version; the stale hash forces that push
        tracked[rel] = e

    row = meta.get("posts.json")
    local_file = data / "posts.json"
    if row and row[0] and not row[3] and local_file.exists():
        try:
            local = json.loads(local_file.read_text(encoding="utf-8"))
            merged, changed = merge_posts(local, json.loads(row[0]))
            if changed:
                _write_json(local_file, merged)
                out["posts"] = len(changed)
                # show the new state on the clip cards too
                jobs = {}
                for f in data.glob("users/*/**/job.json"):
                    try:
                        j = json.loads(f.read_text(encoding="utf-8"))
                        jobs[j.get("id")] = (f, j)
                    except (OSError, ValueError):
                        pass
                touched = {}
                for p in changed:
                    f, j = jobs.get(p.get("job"), (None, None))
                    clip = next((c for c in (j or {}).get("clips", []) if c.get("id") == p.get("clip")), None)
                    if clip is not None:
                        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "error")}
                        touched[f] = j
                for f, j in touched.items():
                    _write_json(f, j)
            adopt("posts.json", row[1])
        except (OSError, ValueError) as e:
            log("could not merge posts:", e)
    for path, text, version in meta.rows("users/%/accounts/accounts.json"):
        f = data / path
        if not f.exists():
            continue
        try:
            local = json.loads(f.read_text(encoding="utf-8"))
            n = merge_accounts(local, json.loads(text))
            if n:
                _write_json(f, local)
                out["accounts"] += n
            adopt(path, version)
        except (OSError, ValueError):
            pass
    save_state(st)
    return out


def cmd_pull(s):
    meta = Meta(s["PG_URI"])
    try:
        r = pull_from_render(meta, s["SERVER_NAME"])
    finally:
        meta.close()
    log(f"took over from Render: {r['posts']} posts updated, {r['accounts']} sign-ins renewed")


def cmd_status(s):
    st = load_state()
    log(f"tracked here: {len(st['files'])} files, last sync {time.strftime('%H:%M:%S', time.localtime(st['last'])) if st['last'] else 'never'}")
    m = Meta(s["PG_URI"])
    for kind, n, size in m.counts():
        print(f"  database: {kind}: {n} files, {float(size) / 1e6:.1f} MB")
    m.close()
    n, size = open_blobs(s).count()
    print(f"  Backblaze: {n} files, {float(size) / 1e6:.1f} MB")


def cmd_wipe_remote(s):
    if input("This deletes EVERYTHING PeakCut copied to Aiven and Backblaze. Type YES: ").strip() != "YES":
        print("Cancelled."); return
    m = Meta(s["PG_URI"]); b = open_blobs(s)
    paths = m.all_paths()
    for p, kind in paths:
        if kind != "blob":
            continue
        try:
            b.delete("data/" + p)
        except Exception:  # noqa: BLE001
            pass
    m.wipe(); m.close()
    STATE.unlink(missing_ok=True)
    print(f"Deleted {len(paths)} entries.")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "help"
    s = load_secrets()
    if cmd != "help" and not os.environ.get("PCSYNC_BLOBDIR") and not all(s.get(k) for k in ("B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET", "PG_URI")):
        print("Missing settings in ~/.pc-secrets (need B2_KEY_ID, B2_APP_KEY, B2_BUCKET, PG_URI)."); return 2
    if cmd == "selftest":
        return cmd_selftest(s)
    if cmd == "once":
        report(run_once(s)); return 0
    if cmd == "loop":
        log("sync started, every", EVERY, "seconds")
        while True:
            try:
                report(run_once(s))
            except Exception as e:  # noqa: BLE001
                log("sync failed, will retry:", str(e)[:200])
            time.sleep(EVERY)
    if cmd == "status":
        cmd_status(s); return 0
    if cmd == "pull":
        cmd_pull(s); return 0
    if cmd == "forget":
        STATE.unlink(missing_ok=True); print("Forgot what was synced. Nothing was deleted on Aiven or Backblaze."); return 0
    if cmd == "wipe-remote":
        cmd_wipe_remote(s); return 0
    print(__doc__); return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
