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


SOURCE_NAMES = {"SullyTranslate: translation", "SullyTranslate: original"}


def is_our_script(entry):
    return os.path.basename(entry.get("path", "").replace("\\", "/")).lower() == "sullytranslate.lua"


def strip_collection(data):
    """Remove SullyTranslate's script entries and caption sources from one scene collection (in place).
    Returns a description of what was removed, empty if nothing was there."""
    removed = []
    modules = data.get("modules") or {}
    scripts = modules.get("scripts-tool")
    if isinstance(scripts, list) and any(is_our_script(s) for s in scripts):
        modules["scripts-tool"] = [s for s in scripts if not is_our_script(s)]
        removed.append("script")
    for key in ("sources", "groups"):
        sources = data.get(key)
        if not isinstance(sources, list):
            continue
        kept = [s for s in sources if s.get("name") not in SOURCE_NAMES]
        if len(kept) != len(sources):
            removed.append(f"{len(sources) - len(kept)} caption source(s)")
            data[key] = kept
        for s in kept:                                   # scenes and groups list their contents by name
            items = (s.get("settings") or {}).get("items")
            if isinstance(items, list):
                s["settings"]["items"] = [i for i in items if i.get("name") not in SOURCE_NAMES]
    return removed


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, path)


def remove_everywhere():
    """Uninstall: clean every scene collection and delete the backups the installer made."""
    scenes = os.path.join(obs_config_dir(), "basic", "scenes")
    if not os.path.isdir(scenes):
        print("No OBS scene collections found; nothing to remove from OBS.")
        return 0
    for name in sorted(os.listdir(scenes)):
        path = os.path.join(scenes, name)
        if name.endswith(".sullytranslate-backup"):
            os.remove(path)
            continue
        if not name.endswith(".json"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            print(f"Skipped {name}: not readable as a scene collection.")
            continue
        removed = strip_collection(data)
        if removed:
            write_json(path, data)
            print(f"Removed from OBS scene collection '{name[:-5]}': {', '.join(removed)}.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("script", nargs="?", help="path to SullyTranslate.lua (needed to add it)")
    ap.add_argument("--remove", action="store_true",
                    help="uninstall: remove the script and both caption sources from every scene collection")
    args = ap.parse_args()

    if obs_running():
        print("OBS is running: close it and run this again (OBS overwrites its scenes when it exits).")
        return 2
    if args.remove:
        return remove_everywhere()
    if not args.script:
        ap.error("give the path to SullyTranslate.lua")
    script = os.path.abspath(args.script)
    collection = current_collection(obs_config_dir())
    if collection is None:
        print("No OBS scene collection found: start OBS once, close it, then run this again.\n"
              f"Or add the script by hand: OBS -> Tools -> Scripts -> + -> {script}")
        return 3

    with open(collection, encoding="utf-8") as f:
        data = json.load(f)
    scripts = data.setdefault("modules", {}).setdefault("scripts-tool", [])
    if any(same_path(s.get("path", ""), script) for s in scripts):
        print(f"Already in OBS ({os.path.basename(collection)}).")
        return 0
    scripts.append({"path": script.replace("\\", "/"), "settings": {}})
    shutil.copy2(collection, collection + ".sullytranslate-backup")
    write_json(collection, data)
    print(f"Added to OBS scene collection '{os.path.splitext(os.path.basename(collection))[0]}' "
          f"(backup: {os.path.basename(collection)}.sullytranslate-backup).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
