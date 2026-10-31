#!/bin/zsh
# Install or update Mosaic Photo-Editor on this Mac.
#
#   ./scripts/install.sh            install everything, including the AI features (asks first)
#   ./scripts/install.sh --no-ai    skip the AI features (about 1 GB smaller)
#   ./scripts/install.sh --yes      don't ask, install the AI features
#
# It creates:
#   ~/Applications/Mosaic Photo-Editor.app                     the app icon (drag it to the Dock)
#   ~/Pictures/Mosaic Photo-Editor/Input and /Output           your photo folders
#   ~/Library/Application Support/Mosaic Photo-Editor/venv     the Python environment
# Run it again after updating the code (git pull); only what changed is reinstalled.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Mosaic Photo-Editor"
SUPPORT="$HOME/Library/Application Support/$APP_NAME"
VENV="$SUPPORT/venv"
PHOTOS="$HOME/Pictures/$APP_NAME"
WITH_AI="ask"
for arg in "$@"; do
  case "$arg" in
    --no-ai) WITH_AI="no" ;;
    --yes|--with-ai) WITH_AI="yes" ;;
    *) echo "Unknown option: $arg"; exit 2 ;;
  esac
done

say() { print -P "%F{red}==>%f %B$1%b"; }

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This installer is for macOS. On other systems use Docker (see the README) or run: streamlit run app.py"
  exit 1
fi

# --- Python 3.10 - 3.13 ------------------------------------------------------------------
find_python() {
  local c v
  for c in "${PYTHON:-}" python3.13 python3.12 python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /opt/anaconda3/bin/python3 python3; do
    [[ -z "$c" ]] && continue
    command -v "$c" >/dev/null 2>&1 || continue
    v=$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || continue
    case "$v" in 3.10|3.11|3.12|3.13) echo "$c"; return 0 ;; esac
  done
  return 1
}
PY=$(find_python) || {
  echo "Python 3.10 to 3.13 is needed. Install it with Homebrew (brew install python@3.12) or from python.org, then run this again."
  exit 1
}
say "Using $("$PY" -c 'import sys; print(sys.executable, sys.version.split()[0])')"

# On Apple silicon, always run Python natively (never under Rosetta), so the packages installed
# here match how the app icon starts the app.
NATIVE=()
if [[ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" == "1" ]]; then
  if /usr/bin/arch -arm64 "$PY" -c "" 2>/dev/null; then
    NATIVE=(/usr/bin/arch -arm64)
  else
    echo "Note: $PY is an Intel-only Python. It works, but an Apple silicon Python is faster:"
    echo "      brew install python@3.12, then run this again with PYTHON=python3.12"
  fi
fi

# --- environment -------------------------------------------------------------------------
mkdir -p "$SUPPORT"
if [[ -x "$VENV/bin/python" ]] && ! "$VENV/bin/python" -c 'import sys' >/dev/null 2>&1; then
  rm -rf "$VENV"                                  # broken (e.g. Python was upgraded): start again
fi
if [[ ! -x "$VENV/bin/python" ]]; then
  say "Creating the Python environment (first run only)"
  "${NATIVE[@]}" "$PY" -m venv "$VENV"
  "${NATIVE[@]}" "$VENV/bin/python" -m pip install --quiet --upgrade pip
fi

if [[ "$WITH_AI" == "ask" ]]; then
  if "${NATIVE[@]}" "$VENV/bin/python" -c 'import torch, transformers' >/dev/null 2>&1; then
    WITH_AI="yes"
  else
    print -n "Install the AI features (background replacement, AI extender)? Downloads about 1 GB. [Y/n] "
    read -r answer || answer=""
    [[ "$answer" == [nN]* ]] && WITH_AI="no" || WITH_AI="yes"
  fi
fi

install_reqs() {        # $1 = requirements file; skipped when unchanged since the last install
  local f="$REPO/$1" stamp="$VENV/.$1.sha"
  local sum=$(shasum "$f" | cut -d' ' -f1)
  if [[ "$(cat "$stamp" 2>/dev/null)" != "$sum" ]]; then
    say "Installing packages from $1"
    "${NATIVE[@]}" "$VENV/bin/python" -m pip install --quiet -r "$f"
    echo "$sum" > "$stamp"
  fi
}
install_reqs requirements.txt
[[ "$WITH_AI" == "yes" ]] && install_reqs requirements-ai.txt

# quick check that the app's main packages load the way the app icon starts them
if ! "${NATIVE[@]}" "$VENV/bin/python" -c 'import PIL._imaging, cv2, numpy, streamlit' 2>/dev/null; then
  say "Repairing packages built for the wrong processor type"
  "${NATIVE[@]}" "$VENV/bin/python" -m pip install --quiet --force-reinstall --no-cache-dir -r "$REPO/requirements.txt"
  [[ "$WITH_AI" == "yes" ]] && "${NATIVE[@]}" "$VENV/bin/python" -m pip install --quiet --force-reinstall --no-cache-dir -r "$REPO/requirements-ai.txt"
fi

# --- folders, app icon, git hooks ----------------------------------------------------------
mkdir -p "$PHOTOS/Input" "$PHOTOS/Output"
"$REPO/scripts/build-app.sh"
if [[ -d "$REPO/.git" ]]; then
  git -C "$REPO" config core.hooksPath .githooks
fi

say "$APP_NAME is installed."
echo "  App:      ~/Applications/$APP_NAME.app  (drag it to the Dock)"
echo "  Photos:   ~/Pictures/$APP_NAME/Input and Output"
echo "  AI:       $([[ "$WITH_AI" == yes ]] && echo "installed; models download on first use (about 1.1 GB)" || echo "not installed (run again without --no-ai to add)")"
print -n "Open it now? [Y/n] "
read -r answer || answer="n"
[[ "$answer" == [nN]* ]] || open "$HOME/Applications/$APP_NAME.app"
