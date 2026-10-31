#!/bin/zsh
# Build the app icon: ~/Applications/Mosaic Photo-Editor.app
# A small macOS app bundle that starts launcher.py from this folder with the app's Python.
# Called by install.sh; run it again if you move this folder.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Mosaic Photo-Editor"
EXEC="mosaic-photo-editor"
VENV="$HOME/Library/Application Support/$APP_NAME/venv"
APP="${MOSAIC_APP_DIR:-$HOME/Applications}/$APP_NAME.app"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/mosaic/__init__.py")
BUNDLE_ID=$(sed -n 's/^BUNDLE_ID = "\(.*\)"/\1/p' "$REPO/mosaic/__init__.py")

xml() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<< "$1"; }
q() { printf "%q" "$1"; }                               # quote for the shell script below

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$REPO/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$(xml "$APP_NAME")</string>
  <key>CFBundleDisplayName</key><string>$(xml "$APP_NAME")</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>$EXEC</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.photography</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string><string>x86_64</string></array>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>MIT License</string>
</dict>
</plist>
EOF

cat > "$APP/Contents/MacOS/$EXEC" <<EOF
#!/bin/zsh
# Starts Mosaic Photo-Editor (built by scripts/build-app.sh)
REPO=$(q "$REPO")
VENV=$(q "$VENV")
export LANG="\${LANG:-en_US.UTF-8}" PYTHONDONTWRITEBYTECODE=1
if [[ ! -x "\$VENV/bin/python" || ! -f "\$REPO/launcher.py" ]]; then
  /usr/bin/osascript -e 'display alert "Mosaic Photo-Editor needs to be set up" message "Open Terminal in the app folder and run ./scripts/install.sh"'
  exit 1
fi
# Apple silicon: macOS may start script-based apps under Rosetta (Intel emulation), which can't load
# the Apple silicon packages. Always start Python natively when the Mac and Python support it.
if [[ "\$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" == "1" ]] && /usr/bin/arch -arm64 "\$VENV/bin/python" -c "" 2>/dev/null; then
  exec /usr/bin/arch -arm64 "\$VENV/bin/python" "\$REPO/launcher.py" "\$@"
fi
exec "\$VENV/bin/python" "\$REPO/launcher.py" "\$@"
EOF
chmod +x "$APP/Contents/MacOS/$EXEC"

# refresh Finder / Dock so the new icon shows
touch "$APP"
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[[ -x "$LSREGISTER" ]] && "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true
echo "Built $APP"
