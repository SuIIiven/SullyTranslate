#!/usr/bin/env bash
# SullyTranslate installer for Linux:  ./install.sh [install-dir]   (default ~/.local/share/SullyTranslate)
# Installs a private Python, the speech engine, the models (~4-7 GB) and the OBS script, then adds the
# script to OBS. Needs PipeWire or PulseAudio (pactl/parec) and OBS installed from your distro (not Flatpak).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$(realpath -m "${1:-$HOME/.local/share/SullyTranslate}")"
step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

for tool in pactl parec curl tar; do
  command -v "$tool" >/dev/null || { echo "missing '$tool' (install pipewire-pulse/pulseaudio-utils, curl, tar)"; exit 1; }
done

step "Installing SullyTranslate into $DEST"
mkdir -p "$DEST/state" "$DEST/tools"
if [ "$SRC" != "$DEST" ]; then
  cp "$SRC"/{sullytranslate.py,SullyTranslate.lua,setup_obs.py,requirements.txt,requirements-gpu.txt,README.md,LICENSE,install.sh,uninstall.sh} "$DEST/"
  cp -r "$SRC/samples" "$DEST/"
fi

step "Getting uv (private Python manager)"
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
  UV="$DEST/tools/uv"
  if [ ! -x "$UV" ]; then
    arch="$(uname -m)"
    curl -LsSf "https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-unknown-linux-gnu.tar.gz" \
      | tar -xz -C "$DEST/tools" --strip-components=1 "uv-$arch-unknown-linux-gnu/uv"
  fi
fi
export UV_PYTHON_INSTALL_DIR="$DEST/tools/python"   # keep Python inside the install folder
export UV_CACHE_DIR="${UV_CACHE_DIR:-$DEST/tools/uv-cache}"   # and the download cache, so uninstall removes it

step "Creating Python environment"
"$UV" venv --python 3.12 --clear "$DEST/.venv"
PY="$DEST/.venv/bin/python"
"$UV" pip install --python "$PY" -r "$DEST/requirements.txt"
if nvidia-smi -L >/dev/null 2>&1; then
  step "NVIDIA GPU found: installing CUDA libraries"
  "$UV" pip install --python "$PY" -r "$DEST/requirements-gpu.txt"
else
  echo "No NVIDIA GPU found: SullyTranslate will run on the CPU (slower, uses smaller models)."
fi

step "Downloading speech + translation models (one time, several GB)"
"$PY" "$DEST/sullytranslate.py" --write-meta "$DEST/state/meta.json"
"$PY" "$DEST/sullytranslate.py" --download-models

step "Adding SullyTranslate to OBS"
while true; do
  set +e; "$PY" "$DEST/setup_obs.py" "$DEST/SullyTranslate.lua"; rc=$?; set -e
  [ "$rc" -eq 2 ] && { read -rp "Close OBS, then press Enter to continue"; continue; }
  break
done

printf '\n\033[32mDone! Start OBS: the captions appear as "SullyTranslate: translation" in your scene.\033[0m\n'
echo "Settings: OBS -> Tools -> Scripts -> SullyTranslate.lua"
