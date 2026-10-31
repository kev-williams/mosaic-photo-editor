#!/bin/sh
# Privacy guard: fails when files that must never be published are in the commit or the repo.
#   scripts/check-privacy.sh            check staged files (used by the pre-commit hook)
#   scripts/check-privacy.sh --all      check every tracked file (used by CI and release.sh)
#
# Blocked: photos, raw files, video and other media; .env and key files; databases and app state;
# anything over 5 MB. The app's own artwork and the face detection model are explicitly allowed.
set -eu

ALLOWED='^(assets/icon\.svg|assets/logo\.svg|assets/AppIcon\.icns|mosaic/models/face_detection_yunet_2023mar\.onnx)$'
BLOCKED='\.(jpe?g|jfif|png|gif|webp|avif|heic|heif|tiff?|bmp|ico|icns|svg|psd|psb|ai|eps|indd|xcf|kra|raw|dng|cr2|cr3|nef|nrw|arw|srf|sr2|orf|raf|rw2|pef|srw|x3f|3fr|erf|mef|mos|iiq|mp4|mov|m4v|avi|mkv|webm|mp3|wav|m4a|aac|flac|pdf|zip|7z|rar|tar|gz|tgz|dmg|pkg|sqlite3?|db|onnx|pt|pth|ckpt|safetensors|bin|h5|npz|npy|pkl|pickle|csv|xlsx?|numbers|key|pem|p12|pfx|crt|cer|keychain)$'
SECRETS='(^|/)(\.env(\..*)?|id_rsa.*|id_ed25519.*|credentials.*|secrets?\..*|crop-adjustments\.json|recent-folders\.json)$'
MAX_BYTES=5242880

if [ "${1:-}" = "--all" ]; then
  files=$(git ls-files)
else
  files=$(git diff --cached --name-only --diff-filter=ACMR)
fi

fail=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if printf '%s\n' "$f" | grep -Eq "$ALLOWED"; then
    continue
  fi
  lower=$(printf '%s' "$f" | tr '[:upper:]' '[:lower:]')
  if printf '%s\n' "$lower" | grep -Eq "$BLOCKED"; then
    echo "BLOCKED (media/data file): $f"; fail=1; continue
  fi
  if printf '%s\n' "$lower" | grep -Eq "$SECRETS"; then
    echo "BLOCKED (private settings or secrets): $f"; fail=1; continue
  fi
  if [ -f "$f" ]; then
    size=$(wc -c < "$f" | tr -d ' ')
    if [ "$size" -gt "$MAX_BYTES" ]; then
      echo "BLOCKED (larger than 5 MB): $f"; fail=1; continue
    fi
    # image data hidden under another extension (magic bytes: JPEG, PNG, GIF, WebP/RIFF, HEIC/AVIF ftyp, TIFF)
    sig=$(head -c 12 "$f" | od -An -tx1 | tr -d ' \n')
    case "$sig" in
      ffd8ff*|89504e47*|47494638*|52494646????????57454250*|????????66747970*|49492a00*|4d4d002a*)
        echo "BLOCKED (contains image data): $f"; fail=1; continue ;;
    esac
  fi
done <<EOF
$files
EOF

if [ "$fail" -ne 0 ]; then
  echo ""
  echo "Commit stopped: the files above must not be published (photos, private data or large files)."
  echo "Remove them from the commit with:  git restore --staged <file>"
  exit 1
fi
echo "Privacy check passed."
