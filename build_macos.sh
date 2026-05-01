#!/usr/bin/env bash
# Build Print Doctor.app via PyInstaller.
#
# Usage:
#   ./build_macos.sh                  # build for the current arch
#
# Prereqs: a Python venv with the project installed (pip install -e ".[build]").

set -euo pipefail

APP_NAME="Print Doctor"
BUNDLE_ID="com.niklos.printdoctor"
ICON_PATH="resources/icon.icns"
ENTRY="src/stl_repair/__main__.py"

cd "$(dirname "$0")"

rm -rf build dist

PYI_ARGS=(
    --windowed
    --name "$APP_NAME"
    --osx-bundle-identifier "$BUNDLE_ID"
    --noconfirm
    --clean
    # pymeshfix ships a native _meshfix extension; PyInstaller picks it up via
    # hiddenimports when we add the package.
    --collect-all pymeshfix
    --collect-all fast_simplification
    --collect-submodules trimesh
)

if [[ -f "$ICON_PATH" ]]; then
    PYI_ARGS+=(--icon "$ICON_PATH")
fi

# Prefer the project venv's pyinstaller if present — saves the user from
# having to `source venv/bin/activate` first. Falls back to PATH for CI,
# which installs pyinstaller into the runner's system Python.
if [[ -x "venv/bin/pyinstaller" ]]; then
    PYI=venv/bin/pyinstaller
else
    PYI=pyinstaller
fi

"$PYI" "${PYI_ARGS[@]}" "$ENTRY"

# Zip the .app for GitHub Release upload
cd dist
ditto -c -k --sequesterRsrc --keepParent "$APP_NAME.app" "$APP_NAME.zip"
cd ..

echo
echo "Built: dist/$APP_NAME.app"
echo "Zip:   dist/$APP_NAME.zip"
