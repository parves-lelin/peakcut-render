#!/usr/bin/env python3
"""Applies the small changes PeakCut needs to run on Render as the 'light' server.

Run:  python patch_peakcut.py PATH_TO_PEAKCUT
Each change is looked up by its exact text. The two essential changes (light mode, the Render address) stop the build if PeakCut
changed that text; nothing is half-applied. The other changes only add friendlier messages, so if PeakCut changed those lines they are
skipped with a warning.
"""
import os
import sys

ESSENTIAL = {"light mode: show everything, process nothing", "accept the Render address"}

PATCHES = [
    ("studio/server.py", "light mode: show everything, process nothing",
     "    jobs.start()\n    live.start()\n    posting.start()\n",
     "    if config.env(\"PEAKCUT_LIGHT\") == \"1\":        # Render: shows and posts, but does not process videos or record\n"
     "        jobs.load.start()\n"
     "        jobs.load_all()\n"
     "    else:\n"
     "        jobs.start()\n"
     "        live.start()\n"
     "    posting.start()\n"),
    ("studio/server.py", "accept the Render address",
     "    if name in (\"localhost\", urlparse(config.PUBLIC_URL).hostname or \"\") or name.endswith(\".localhost\"):\n        return True\n",
     "    if name in (\"localhost\", urlparse(config.PUBLIC_URL).hostname or \"\") or name.endswith(\".localhost\"):\n        return True\n"
     "    import os as _os\n"
     "    extra = {h.strip().lower() for h in (config.env(\"PEAKCUT_EXTRA_HOSTS\", \"\") + \",\" + _os.environ.get(\"RENDER_EXTERNAL_HOSTNAME\", \"\")).split(\",\") if h.strip()}\n"
     "    if name in extra:\n        return True\n"),
    ("studio/server.py", "no file uploads on the light server",
     "def create_from_file(file: UploadFile = File(...), options: str = Form(\"{}\"), creator: str = Form(\"\")):\n    _need_verified()\n",
     "def create_from_file(file: UploadFile = File(...), options: str = Form(\"{}\"), creator: str = Form(\"\")):\n    _need_verified()\n"
     "    if config.env(\"PEAKCUT_LIGHT\") == \"1\":\n"
     "        raise HTTPException(409, \"Uploading a video needs Cloud Shell. Turn Cloud Shell on and upload it there.\")\n"),
    ("studio/server.py", "no chunked uploads on the light server",
     "def upload_start(body: dict = Body(...)):\n    _need_verified()\n",
     "def upload_start(body: dict = Body(...)):\n    _need_verified()\n"
     "    if config.env(\"PEAKCUT_LIGHT\") == \"1\":\n"
     "        raise HTTPException(409, \"Uploading a video needs Cloud Shell. Turn Cloud Shell on and upload it there.\")\n"),
    ("studio/live.py", "no recording on the light server",
     "    from . import tools\n    if tools.missing():\n        raise ValueError(tools.HELP)\n    job = jobs.create({\"type\": \"live\"",
     "    if config.env(\"PEAKCUT_LIGHT\") == \"1\":\n"
     "        raise ValueError(\"Recording needs Cloud Shell. Turn Cloud Shell on and start it there.\")\n"
     "    from . import tools\n    if tools.missing():\n        raise ValueError(tools.HELP)\n    job = jobs.create({\"type\": \"live\""),
]


def main(root):
    todo = {}
    for rel, what, old, new in PATCHES:
        path = os.path.join(root, rel)
        text = todo.get(path)
        if text is None:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        if new in text:
            print("already applied:", what)
            todo[path] = text
            continue
        if text.count(old) != 1:
            if what in ESSENTIAL:
                print(f"STOP: cannot apply '{what}' in {rel} ({text.count(old)} matches). PeakCut changed here; nothing was written.")
                return 1
            print(f"skipped (optional): '{what}' in {rel}, PeakCut changed those lines")
            todo[path] = text
            continue
        todo[path] = text.replace(old, new)
        print("ok:", what)
    for path, text in todo.items():
        compile(text, path, "exec")          # a change that breaks the file is never written
    for path, text in todo.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "peakcut-src"))
