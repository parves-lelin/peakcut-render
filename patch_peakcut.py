#!/usr/bin/env python3
"""Applies the small changes PeakCut needs to run on Render as the 'light' server.

Run:  python patch_peakcut.py PATH_TO_PEAKCUT [--extras-only]
--extras-only adds only the Settings page "Server & cookies" (for Cloud Shell, whose PeakCut is not the light one).
Each change is looked up by its exact text. The two essential changes (light mode, the Render address) stop the build if PeakCut
changed that text; nothing is half-applied. The other changes only add friendlier messages, so if PeakCut changed those lines they are
skipped with a warning.
"""
import os
import shutil
import sys

ESSENTIAL = {"light mode: show everything, process nothing", "accept the Render address"}
EXTRAS = {"settings: server and cookies", "settings: tab in the list", "settings: tab page", "settings: tab code"}
HERE = os.path.dirname(os.path.abspath(__file__))

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
    ("studio/server.py", "settings: server and cookies",
     "\n\ndef main():\n",
     "\n\ntry:\n    from . import pcserver\n    pcserver.register(app, _admin)\nexcept Exception as _e:   # the Server & cookies settings are optional\n    print(f\"  (Server & cookies settings not loaded: {_e})\")\n\n\ndef main():\n"),
    ("web/index.html", "settings: tab in the list",
     '  ...(S.status.user?.is_admin ? [["storage", "Storage", "folder", "Where videos and clips are kept"]] : [])];\n',
     '  ...(S.status.user?.is_admin ? [["storage", "Storage", "folder", "Where videos and clips are kept"],\n'
     '                                 ["pcserver", "Server & cookies", "radio", "Which server shows PeakCut, YouTube cookies"]] : [])];\n'),
    ("web/index.html", "settings: tab page",
     "  } catch (e) { body.innerHTML = `<div class=\"set-load\">${esc(e.message)}</div>`; }\n}\nasync function saveSettings()",
     "    if (tab === \"pcserver\") await pcServerTab(body);\n  } catch (e) { body.innerHTML = `<div class=\"set-load\">${esc(e.message)}</div>`; }\n}\nasync function saveSettings()"),
    ("web/index.html", "settings: tab code",
     "async function drawSettings() {\n",
     "@@UI@@async function drawSettings() {\n"),
]


def read_sibling(name):
    for d in (HERE, os.path.dirname(HERE)):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                return f.read()
    return None


def main(root, extras_only=False):
    ui = read_sibling("pcserver_ui.js")
    server_py = read_sibling("pcserver.py")
    todo = {}
    for rel, what, old, new in PATCHES:
        if extras_only and what not in EXTRAS:
            continue
        if what in EXTRAS and (ui is None or server_py is None):
            print(f"skipped (optional): '{what}': pcserver.py or pcserver_ui.js is missing next to this script")
            continue
        new = new.replace("@@UI@@", (ui or "") + "\n")
        path = os.path.join(root, rel)
        text = todo.get(path)
        if text is None:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        if what == "settings: tab code" and "async function pcServerTab(" in text:
            print("already applied:", what)
            todo[path] = text
            continue
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
        if path.endswith(".py"):
            compile(text, path, "exec")          # a change that breaks the file is never written
    for path, text in todo.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    if server_py is not None and os.path.isdir(os.path.join(root, "studio")):
        with open(os.path.join(root, "studio", "pcserver.py"), "w", encoding="utf-8") as f:
            f.write(server_py)
        print("ok: studio/pcserver.py")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(args[0] if args else "peakcut-src", extras_only="--extras-only" in sys.argv))
