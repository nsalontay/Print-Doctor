#!/usr/bin/env bash
# Sign a built Print Doctor.app for Developer ID distribution.
#
# Usage:
#   export SIGNING_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   ./scripts/sign_app.sh [path/to/Print Doctor.app]
#
# Default app path is /tmp/Print Doctor.app — we copy the bundle out of
# the (often iCloud-synced) project tree before signing because macOS
# File Provider re-adds xattrs (com.apple.fileprovider.fpfs#P) faster
# than codesign can tolerate them.
#
# This script signs INSIDE-OUT: every nested Mach-O binary first, then
# the top-level .app with hardened runtime + entitlements. PyInstaller
# bundles have hundreds of .so/.dylib files; codesign --deep alone is
# unreliable on them.

set -euo pipefail

APP="${1:-/tmp/Print Doctor.app}"
ENTITLEMENTS="$(dirname "$0")/../resources/entitlements.plist"

if [[ -z "${SIGNING_IDENTITY:-}" ]]; then
    echo "ERROR: SIGNING_IDENTITY env var not set." >&2
    echo "Example: export SIGNING_IDENTITY=\"Developer ID Application: Your Name (TEAMID)\"" >&2
    exit 1
fi

if [[ ! -d "$APP" ]]; then
    echo "ERROR: $APP does not exist." >&2
    echo "Build first with ./build_macos.sh, then ditto to /tmp:" >&2
    echo "  ditto --noextattr --noqtn \"dist/Print Doctor.app\" \"$APP\"" >&2
    exit 1
fi

if [[ ! -f "$ENTITLEMENTS" ]]; then
    echo "ERROR: entitlements file not found at $ENTITLEMENTS" >&2
    exit 1
fi

echo "==> Stripping any lingering xattrs (defense in depth)…"
xattr -cr "$APP"

echo "==> Signing nested .so / .dylib files inside-out…"
# -depth makes find process leaves before parents — important for nested
# frameworks where the framework dir's signature must come last.
find "$APP" -depth -type f \( -name "*.so" -o -name "*.dylib" \) -print0 |
while IFS= read -r -d '' f; do
    codesign --force --options runtime --timestamp \
        --sign "$SIGNING_IDENTITY" "$f"
done

echo "==> Signing nested executables (Mach-O, no extension)…"
# Catch the bundled python and any other executables PyInstaller embeds.
find "$APP/Contents" -depth -type f -perm +111 \
    ! -name "*.so" ! -name "*.dylib" -print0 |
while IFS= read -r -d '' f; do
    if file "$f" 2>/dev/null | grep -q "Mach-O"; then
        codesign --force --options runtime --timestamp \
            --sign "$SIGNING_IDENTITY" "$f"
    fi
done

echo "==> Signing top-level .app with entitlements…"
codesign --force --options runtime --timestamp \
    --entitlements "$ENTITLEMENTS" \
    --sign "$SIGNING_IDENTITY" "$APP"

echo "==> Verifying signature…"
codesign --verify --deep --strict --verbose=2 "$APP"
codesign -dvv "$APP" 2>&1 | head -15

echo
echo "==> Done. Next: notarize."
echo "    xcrun notarytool submit … (see notarize_app.sh)"
