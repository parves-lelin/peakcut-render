#!/usr/bin/env python3
"""takeover: lets Cloud Shell and Render take turns serving PeakCut on the same data.

Only one server serves at a time. When a server starts, it first claims the turn, waits for the other one to let go,
and then pulls every file the other server changed (settings, creators, jobs, accounts, queue, deleted clips) from
Aiven and Backblaze, so it carries on exactly where the other one stopped.

Commands:  claim-pull  (Cloud Shell, before PeakCut starts)
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pcsync  # noqa: E402

SERVING = "__serving__:render"       # touched every few seconds while Render serves PeakCut
RELEASED = "__released__:render"     # touched by Render once it has stopped serving and saved everything
CLAIM = "__claim__:cloudshell"       # touched by Cloud Shell when it wants the turn
log = pcsync.log


# ------------------------------------------------------------------ the turn
def stamp(meta, key):
    r = meta.q("SELECT updated FROM pc_files WHERE path = ?", (key,)).fetchone()
    return float(r[0]) if r and r[0] is not None else None


def claim_turn(meta, writer, sleep=time.sleep, max_wait=300, say=log) -> str:
    """Cloud Shell asks for the turn. Returns 'free' (Render was not serving), 'released' (Render let go), or 'timeout'."""
    t0 = time.time()
    meta.touch(CLAIM, writer)
    meta.touch(pcsync.HEARTBEAT, writer)
    waited = 0
    while waited < max_wait:                     # Render may be in the middle of one upload (poster-only mode)
        b = meta.age(pcsync.BUSY)
        if b is None or b > 900:
            break
        say("Render is in the middle of a post, waiting...")
        sleep(10)
        waited += 10
        meta.touch(pcsync.HEARTBEAT, writer)
    a = meta.age(SERVING)
    if a is None or a > 60:
        return "free"
    while waited < max_wait:
        released = stamp(meta, RELEASED)
        if released is not None and released >= t0:
            return "released"
        a = meta.age(SERVING)
        if a is None or a > 60:
            return "free"
        say("Render is saving its work before it hands over...")
        sleep(5)
        waited += 5
        meta.touch(pcsync.HEARTBEAT, writer)
    return "timeout"


# ------------------------------------------------------------------ merging the two files that both servers can change
def merge_posts_union(local: list, remote: list) -> list:
    merged, _changed = pcsync.merge_posts(local, remote)
    have = {p.get("id") for p in merged}
    merged += [p for p in remote if p.get("id") not in have]      # queued on the other server
    return merged


def merge_accounts_union(local: list, remote: list) -> list:
    """The other server's list wins (accounts added or removed there); keep whichever sign-in token lasts longer."""
    mine = {a.get("id"): a for a in local}
    out = []
    for r in remote:
        l = mine.get(r.get("id"))
        if l is not None and (l.get("expires") or 0) > (r.get("expires") or 0):
            r = dict(r, access_token=l.get("access_token"), expires=l.get("expires"))
        out.append(r)
    return out


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if "accounts" in path.parts:
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
    tmp.replace(path)


# ------------------------------------------------------------------ pulling everything that changed
def pull_all(meta, blobs, data: Path, writer: str, fetch_blobs=True, now=None, restore_db=True) -> dict:
    """Bring this server's files up to date with the shared copy. Call it only while PeakCut is NOT running here
    (the sign-in database is replaced). restore_db=False leaves the database alone."""
    now = now or time.time()
    st = pcsync.load_state()
    tracked = st["files"]
    out = {"written": 0, "merged": 0, "deleted": 0, "blobs": 0, "pending_blobs": 0, "db": False, "conflicts": [], "errors": []}
    rows = meta.q("SELECT path, kind, data, size, sha1, version, writer, deleted FROM pc_files WHERE kind != 'meta' ORDER BY path").fetchall()

    def adopt(rel, kind, version, text=None, force_push=False):
        p = data / rel
        try:
            stt = p.stat()
            mtime, size = stt.st_mtime_ns, stt.st_size
        except OSError:
            mtime, size = 0, 0
        sha = "" if force_push else (_sha(text) if text is not None else "")
        tracked[rel] = {"mtime": 0 if force_push else mtime, "size": 0 if force_push else size, "sha1": sha, "version": version, "kind": kind}

    for path, kind, text, size, sha1, version, wr, deleted in rows:
        rel = path
        local = data / rel
        t = tracked.get(rel)
        try:
            if deleted:
                if local.exists() and t:
                    cur = _sha(local.read_text(encoding="utf-8")) if kind == "small" else None
                    if kind == "blob" or cur == t.get("sha1"):
                        local.unlink()
                        out["deleted"] += 1
                    else:
                        out["conflicts"].append(f"{rel} was deleted on the other server but changed here: kept")
                        continue
                tracked.pop(rel, None)
                continue
            in_sync = bool(t) and t.get("version") == version
            if kind == "blob" and rel == pcsync.DB_NAME:
                if not restore_db or (in_sync and local.exists()):
                    continue
                if local.exists() and (not t or t.get("fp") != pcsync.db_fingerprint(local)):
                    import shutil
                    shutil.copy2(local, local.with_name(local.name + f".local-{int(now)}"))      # ours changed too: keep a copy
                    out["conflicts"].append("the sign-in database changed on both servers: the other server's was used, ours is kept as a backup")
                pcsync.restore_db(blobs, data)
                tracked[rel] = {"mtime": 0, "size": 0, "sha1": sha1 or "", "version": version, "kind": "blob",
                                "sig": pcsync.db_signature(data), "at": now, "fp": pcsync.db_fingerprint(data / pcsync.DB_NAME)}
                out["db"] = True
                continue
            if kind == "small":
                if in_sync and local.exists():
                    continue
                rsha = _sha(text or "")
                if local.exists():
                    ltext = local.read_text(encoding="utf-8")
                    lsha = _sha(ltext)
                    if lsha == rsha:
                        adopt(rel, kind, version, text)
                        continue
                    edited_here = (not t) or lsha != t.get("sha1")
                    if edited_here:
                        name = local.name
                        if name == "posts.json":
                            merged = merge_posts_union(json.loads(ltext or "[]"), json.loads(text or "[]"))
                            _write(local, json.dumps(merged, indent=1))
                            adopt(rel, kind, version, force_push=True)      # our next push carries the merge
                            out["merged"] += 1
                            continue
                        if name == "accounts.json":
                            merged = merge_accounts_union(json.loads(ltext or "[]"), json.loads(text or "[]"))
                            _write(local, json.dumps(merged, indent=1))
                            adopt(rel, kind, version, force_push=True)
                            out["merged"] += 1
                            continue
                        backup = local.with_name(local.name + f".local-{int(now)}")
                        backup.write_text(ltext, encoding="utf-8")
                        out["conflicts"].append(f"{rel}: both servers changed it; the other server's copy was used, yours is saved as {backup.name}")
                _write(local, text or "")
                adopt(rel, kind, version, text)
                out["written"] += 1
            else:
                ok_local = local.exists() and local.stat().st_size == size
                if ok_local:
                    adopt(rel, kind, version)
                    tracked[rel]["sha1"] = sha1 or ""
                    continue
                if not fetch_blobs:
                    out["pending_blobs"] += 1
                    continue
                blobs.get("data/" + rel, local)
                if local.stat().st_size != size:
                    local.unlink(missing_ok=True)
                    raise RuntimeError("downloaded size differs")
                adopt(rel, kind, version)
                tracked[rel]["sha1"] = sha1 or ""
                out["blobs"] += 1
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"{rel}: {e}")
    pcsync.save_state(st)
    return out


def fetch_some(meta, blobs, data: Path, limit=5, max_bytes=2_000_000_000) -> int:
    """Download up to `limit` clips this server does not have yet, newest first, until max_bytes are held. Returns how many."""
    st = pcsync.load_state()
    tracked = st["files"]
    have = sum(f.stat().st_size for f in data.rglob("*.mp4") if f.is_file()) if data.is_dir() else 0
    rows = meta.q("SELECT path, size, sha1, version, updated FROM pc_files WHERE kind = 'blob' AND deleted = 0 AND path != ? "
                  "ORDER BY updated DESC", (pcsync.DB_NAME,)).fetchall()
    n = 0
    for path, size, sha1, version, _u in rows:
        local = data / path
        if local.exists() and local.stat().st_size == size:
            continue
        if have + size > max_bytes:
            continue
        blobs.get("data/" + path, local)
        if local.stat().st_size != size:
            local.unlink(missing_ok=True)
            continue
        tracked[path] = {"mtime": local.stat().st_mtime_ns, "size": local.stat().st_size, "sha1": sha1 or "", "version": version, "kind": "blob"}
        have += size
        n += 1
        if n >= limit:
            break
    pcsync.save_state(st)
    return n


def claim_and_pull(s, data: Path = None, sleep=time.sleep) -> dict:
    meta = pcsync.Meta(s["PG_URI"])
    try:
        turn = claim_turn(meta, s["SERVER_NAME"], sleep=sleep)
        log("turn:", {"free": "Render was not serving", "released": "Render handed over", "timeout": "Render did not answer, carrying on"}[turn])
        res = pull_all(meta, pcsync.open_blobs(s), data or pcsync.DATA, s["SERVER_NAME"], fetch_blobs=False)
        res["turn"] = turn
        return res
    finally:
        meta.close()


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "help"
    s = pcsync.load_secrets()
    if cmd == "claim-pull":
        if not all(s.get(k) for k in ("B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET", "PG_URI")):
            print("Missing settings in ~/.pc-secrets"); return 2
        r = claim_and_pull(s)
        log(f"pulled from the other server: {r['written']} files, {r['merged']} merged, {r['deleted']} deleted, "
            f"{r['pending_blobs']} clips not needed here" + (", sign-in database replaced" if r["db"] else ""))
        for c in r["conflicts"]:
            log("CONFLICT:", c)
        for e in r["errors"][:5]:
            log("problem:", e)
        return 0
    print(__doc__); return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
