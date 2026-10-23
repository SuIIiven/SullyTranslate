#!/usr/bin/env bash
# SullyTranslate uninstaller for Linux. Run it from the install folder:  ./uninstall.sh
# Removes the OBS script and both caption sources from every OBS scene collection, the script's link or copy
# in OBS's own scripts folder, and every file the installer created (Python, libraries, models, caches).
set -euo pipefail

DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OBS_SCRIPT="/usr/share/obs/obs-plugins/frontend-tools/scripts/SullyTranslate.lua"
step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

if [ ! -f "$DEST/sullytranslate.py" ] || [ "$DEST" = "/" ] || [ "$DEST" = "$HOME" ]; then
  echo "This doesn't look like a SullyTranslate folder: $DEST"; exit 1
fi
read -rp "Remove SullyTranslate ($DEST) and its sources from OBS? [y/N] " answer
[[ "$answer" =~ ^[yY] ]] || { echo "Nothing changed."; exit 0; }

step "Removing SullyTranslate from OBS"
PY="$DEST/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
while true; do
  set +e; "$PY" "$DEST/setup_obs.py" --remove; rc=$?; set -e
  [ "$rc" -eq 2 ] && { read -rp "Close OBS, then press Enter to continue"; continue; }
  break
done
[ "$rc" -eq 0 ] || { echo "Could not update OBS's scene collections (exit $rc); nothing deleted."; exit 1; }

step "Stopping the speech engine"
pkill -f "$DEST/sullytranslate.py" 2>/dev/null && sleep 1 || echo "Not running."

# Only remove a script in OBS's folder that belongs to this install (a link to it, or a copy).
if [ -L "$OBS_SCRIPT" ] || [ -e "$OBS_SCRIPT" ]; then
  if [ ! -L "$OBS_SCRIPT" ] || [ "$(readlink -f "$OBS_SCRIPT")" = "$DEST/SullyTranslate.lua" ]; then
    step "Removing $OBS_SCRIPT (asks for your password)"
    if [ -w "$(dirname "$OBS_SCRIPT")" ]; then rm -f "$OBS_SCRIPT"; else sudo rm -f "$OBS_SCRIPT"; fi
  else
    echo "Leaving $OBS_SCRIPT: it belongs to another SullyTranslate install ($(readlink -f "$OBS_SCRIPT"))."
  fi
fi

step "Deleting installed files"
cd /
if [ -d "$DEST/.git" ]; then
  # A development checkout: keep the source code, delete only what the installer generated.
  rm -rf "$DEST/.venv" "$DEST/models" "$DEST/tools" "$DEST/state" "$DEST/__pycache__"
  echo "Kept the source code in $DEST (it is a git checkout); removed the environment, models and state."
else
  rm -rf "$DEST"
  echo "Deleted $DEST."
fi

printf '\n\033[32mSullyTranslate has been removed.\033[0m\n'
