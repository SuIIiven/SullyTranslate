"""Add (or remove) SullyTranslate.lua in OBS's current scene collection, so users never open Tools -> Scripts.

OBS must be closed: it rewrites the scene collection on exit. A backup is kept next to the edited file.
Exit codes: 0 done / already there, 2 OBS is running, 3 no OBS scene collection found (start OBS once first).
"""

import argparse
import configparser
import json
import os
import shutil
import subprocess
import sys

WINDOWS = os.name == "nt"


def obs_config_dir():
    if WINDOWS:
        return os.path.join(os.environ["APPDATA"], "obs-studio")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "obs-studio")


def obs_running():
    if WINDOWS:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"],
                             capture_output=True, text=True).stdout
        return "obs64.exe" in out.lower()
    return subprocess.run(["pgrep", "-x", "obs"], capture_output=True).returncode == 0


def current_collection(cfg_dir):
    """Path of the scene collection OBS opens on start (user.ini on OBS 31+, global.ini before)."""
    for ini in ("user.ini", "global.ini"):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        try:
            parser.read(os.path.join(cfg_dir, ini), encoding="utf-8-sig")
        except configparser.Error:
            continue
        name = parser.get("Basic", "SceneCollectionFile", fallback=None)
        if name:
            path = os.path.join(cfg_dir, "basic", "scenes", name + ".json")
            if os.path.exists(path):
                return path
    scenes = os.path.join(cfg_dir, "basic", "scenes")
    found = sorted(f for f in os.listdir(scenes) if f.endswith(".json")) if os.path.isdir(scenes) else []
    return os.path.join(scenes, found[0]) if len(found) == 1 else None


def same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("script", help="path to SullyTranslate.lua")
    ap.add_argument("--remove", action="store_true", help="remove the script instead of adding it")
    args = ap.parse_args()
    script = os.path.abspath(args.script)

    if obs_running():
        print("OBS is running: close it and run this again (OBS overwrites its scenes when it exits).")
        return 2
    collection = current_collection(obs_config_dir())
    if collection is None:
        print("No OBS scene collection found: start OBS once, close it, then run this again.\n"
              f"Or add the script by hand: OBS -> Tools -> Scripts -> + -> {script}")
        return 3

    with open(collection, encoding="utf-8") as f:
        data = json.load(f)
    scripts = data.setdefault("modules", {}).setdefault("scripts-tool", [])
    present = [s for s in scripts if same_path(s.get("path", ""), script)]
    if args.remove:
        if not present:
            print(f"Not registered in {collection}")
            return 0
        scripts[:] = [s for s in scripts if s not in present]
    else:
        if present:
            print(f"Already in OBS ({os.path.basename(collection)}).")
            return 0
        scripts.append({"path": script.replace("\\", "/"), "settings": {}})

    shutil.copy2(collection, collection + ".sullytranslate-backup")
    tmp = collection + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, collection)
    action = "Removed from" if args.remove else "Added to"
    print(f"{action} OBS scene collection '{os.path.splitext(os.path.basename(collection))[0]}' "
          f"(backup: {os.path.basename(collection)}.sullytranslate-backup).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
