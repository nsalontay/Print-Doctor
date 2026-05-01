#!/usr/bin/env bash
# Submit a signed Print Doctor.app to Apple for notarization, wait for
# the verdict, then staple the resulting ticket onto the bundle.
#
# Usage:
#   ./scripts/notarize_app.sh [path/to/Print Doctor.app]
#
# Default app path: /tmp/Print Doctor.app
#
# Authentication (pick one):
#   A) Local — profile pre-stored in your keychain:
#        xcrun notarytool store-credentials "print-doctor-notary" \
#          --apple-id you@email.com --team-id TF2CR7ND5S
#      Then: NOTARY_PROFILE=print-doctor-notary ./scripts/notarize_app.sh
#      (NOTARY_PROFILE defaults to "print-doctor-notary" if unset.)
#
#   B) CI — inline env vars from GitHub Secrets:
#        APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD, APPLE_TEAM_ID
#      The script auto-detects mode B when the profile isn't found.

set -euo pipefail

APP="${1:-/tmp/Print Doctor.app}"
PROFILE="${NOTARY_PROFILE:-print-doctor-notary}"
ZIP="/tmp/Print-Doctor-notarize.zip"

if [[ ! -d "$APP" ]]; then
    echo "ERROR: $APP does not exist." >&2
    exit 1
fi

# Decide auth mode. Profile takes precedence; fall back to env vars.
NOTARY_AUTH=()
if xcrun notarytool history --keychain-profile "$PROFILE" >/dev/null 2>&1; then
    echo "==> Using keychain profile: $PROFILE"
    NOTARY_AUTH=(--keychain-profile "$PROFILE")
elif [[ -n "${APPLE_ID:-}" && -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" && -n "${APPLE_TEAM_ID:-}" ]]; then
    echo "==> Using inline credentials from APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD / APPLE_TEAM_ID env vars"
    NOTARY_AUTH=(
        --apple-id "$APPLE_ID"
        --password "$APPLE_APP_SPECIFIC_PASSWORD"
        --team-id "$APPLE_TEAM_ID"
    )
else
    echo "ERROR: no auth method available." >&2
    echo "Either store a keychain profile (see header) or set" >&2
    echo "APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD, APPLE_TEAM_ID env vars." >&2
    exit 1
fi

echo "==> Zipping bundle for submission…"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
echo "    $(du -h "$ZIP" | cut -f1)  $ZIP"

echo
echo "==> Submitting to Apple notarization service (this blocks 3-15 min)…"
SUBMIT_OUT=$(xcrun notarytool submit "$ZIP" \
    "${NOTARY_AUTH[@]}" \
    --wait \
    --output-format plist 2>&1)
echo "$SUBMIT_OUT"

# Extract submission ID and status from the plist output.
SUBMISSION_ID=$(echo "$SUBMIT_OUT" | /usr/libexec/PlistBuddy -c "Print :id" /dev/stdin 2>/dev/null || echo "")
STATUS=$(echo "$SUBMIT_OUT" | /usr/libexec/PlistBuddy -c "Print :status" /dev/stdin 2>/dev/null || echo "Unknown")

echo
echo "==> Verdict: $STATUS"

if [[ "$STATUS" != "Accepted" ]]; then
    echo
    echo "==> Notarization NOT accepted. Fetching log for $SUBMISSION_ID…"
    xcrun notarytool log "$SUBMISSION_ID" "${NOTARY_AUTH[@]}"
    exit 1
fi

echo
echo "==> Stapling notarization ticket onto the bundle…"
xcrun stapler staple "$APP"

echo
echo "==> Final Gatekeeper assessment…"
spctl -a -vv "$APP"

echo
echo "==> SUCCESS. Bundle is signed, notarized, and stapled."
echo "    $APP"
