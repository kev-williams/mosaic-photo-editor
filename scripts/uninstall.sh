#!/bin/zsh
# Remove Mosaic Photo-Editor from this Mac: the app icon and its Python environment.
# Your photos (~/Pictures/Mosaic Photo-Editor) are never touched. You are asked before the
# app data (AI models, history, crop adjustments) and logs are removed.
set -euo pipefail
APP_NAME="Mosaic Photo-Editor"
SUPPORT="$HOME/Library/Application Support/$APP_NAME"

rm -rf "$HOME/Applications/$APP_NAME.app" "$SUPPORT/venv"
echo "Removed the app and its Python environment."
if [[ -d "$SUPPORT" ]]; then
  print -n "Also remove app data (AI models, history, crop adjustments) and logs? [y/N] "
  read -r answer || answer="n"
  if [[ "$answer" == [yY]* ]]; then
    rm -rf "$SUPPORT" "$HOME/Library/Logs/$APP_NAME"
    echo "Removed app data and logs."
  fi
fi
echo "Your photos in ~/Pictures/$APP_NAME were kept. Delete this folder to remove the code."
