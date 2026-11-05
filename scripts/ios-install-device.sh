#!/usr/bin/env bash
# Build the iPhone app (Debug) signed with your free Personal Team and
# install it on the iPhone plugged into this Mac (docs/ios.md).
#
# Usage: API_BASE_URL=https://api.example.org scripts/ios-install-device.sh
#
# Needs:
#   - API_BASE_URL: your server's address, https only. The phone cannot
#     reach the 127.0.0.1 the Simulator build uses.
#   - ios/Signing.local.xcconfig with your Team ID
#     (copy ios/Signing.local.xcconfig.example and edit it)
#   - Xcode signed in to your Apple ID (Xcode → Settings → Accounts)
#   - the iPhone plugged in, unlocked, trusted, Developer Mode on
#
# Signing stays on this Mac: Xcode makes the certificate and profile
# itself (-allowProvisioningUpdates). Nothing is exported or copied.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
ios_dir="$repo_root/ios"
signing_file="$ios_dir/Signing.local.xcconfig"

fail() {
    echo "" >&2
    echo "error: $1" >&2
    shift
    for line in "$@"; do echo "       $line" >&2; done
    exit 1
}

# 1. The server address: https, a host, nothing after it.
api_base_url="${API_BASE_URL:-}"
url_re='^https://[][A-Za-z0-9.:-]+/?$'
if [[ ! "$api_base_url" =~ $url_re ]]; then
    fail "Set API_BASE_URL to your server's address, for example:" \
        "  API_BASE_URL=https://api.example.org scripts/ios-install-device.sh" \
        "https only, with no path. It is API_HOSTNAME on the server."
fi
api_base_url="${api_base_url%/}"

# 2. Signing file: present, and the Team ID is not the placeholder.
if [[ ! -f "$signing_file" ]]; then
    fail "ios/Signing.local.xcconfig is missing." \
        "Create it once:" \
        "  cp ios/Signing.local.xcconfig.example ios/Signing.local.xcconfig" \
        "Then put your Team ID in it (Xcode → Settings → Accounts → your team)."
fi
if ! grep -Eq '^[[:space:]]*DEVELOPMENT_TEAM[[:space:]]*=[[:space:]]*[A-Z0-9]{10}[[:space:]]*$' "$signing_file"; then
    fail "ios/Signing.local.xcconfig has no Team ID yet." \
        "Replace YOUR_TEAM_ID with the 10-character id from" \
        "Xcode → Settings → Accounts → your Personal Team."
fi

# 3. Exactly one real iPhone that this Mac can reach.
# devicectl also lists simulators; keep physical iPhones only. A phone
# that is plugged in (or reachable over Wi-Fi) has a transport type.
if ! command -v jq >/dev/null 2>&1; then
    fail "jq is missing. It ships with macOS 15 and later at /usr/bin/jq."
fi
devices_json="$(mktemp)"
trap 'rm -f "$devices_json"' EXIT
if ! xcrun devicectl list devices --quiet --json-output "$devices_json" >/dev/null 2>&1; then
    fail "Could not list devices with 'xcrun devicectl'." \
        "Check that Xcode is installed and selected: xcode-select -p"
fi

phones="$(jq -r '
    .result.devices[]
    | (.properties.hardware // .hardwareProperties) as $hw
    | (.properties.connection // .connectionProperties // {}) as $conn
    | select($hw.reality == "physical" and $hw.deviceType == "iPhone")
    | select(($conn.transportType // "") != "")
    | [$hw.udid, ($conn.pairingState // "unknown"), (.properties.state.name // .deviceProperties.name // "iPhone")]
    | @tsv
' "$devices_json")"

if [[ -z "$phones" ]]; then
    fail "No iPhone is connected." \
        "Plug in the iPhone with a cable, unlock it, and tap Trust if it asks." \
        "Then run this script again."
fi
if [[ "$(printf '%s\n' "$phones" | wc -l | tr -d ' ')" != "1" ]]; then
    fail "More than one iPhone is connected. Unplug all but one."
fi

IFS=$'\t' read -r udid pairing name <<<"$phones"
if [[ "$pairing" != "paired" ]]; then
    fail "The iPhone \"$name\" is not paired with this Mac." \
        "Unlock it and tap Trust on the \"Trust This Computer?\" prompt."
fi

# 4. Build Debug for this iPhone, against API_BASE_URL. Xcode creates or renews the free
# certificate and profile because of -allowProvisioningUpdates.
build_dir="$ios_dir/build/device"
build_log="$build_dir/xcodebuild.log"
mkdir -p "$build_dir"
echo "Building New2U (Debug) for \"$name\", server $api_base_url… (log: ios/build/device/xcodebuild.log)"
if ! xcodebuild \
    -project "$ios_dir/New2U.xcodeproj" \
    -scheme New2U \
    -configuration Debug \
    -destination "id=$udid" \
    -derivedDataPath "$build_dir" \
    -allowProvisioningUpdates \
    "API_BASE_URL=${api_base_url/:\/\//:/\$()/}" \
    build >"$build_log" 2>&1; then
    grep -E 'error:' "$build_log" | sort -u | tail -n 10 >&2 || true
    fail "The build failed. The errors are above; the full log is" \
        "ios/build/device/xcodebuild.log." \
        "If it says the bundle id is not available, change" \
        "PRODUCT_BUNDLE_IDENTIFIER in ios/Signing.local.xcconfig." \
        "If it says there is no account for the team, sign in to Xcode:" \
        "Xcode → Settings → Accounts → + → Apple ID."
fi

app="$build_dir/Build/Products/Debug-iphoneos/New2U.app"
if [[ ! -d "$app" ]]; then
    fail "Build said it succeeded, but $app is not there."
fi

# 5. Install.
echo "Installing on \"$name\"…"
xcrun devicectl device install app --device "$udid" "$app"

echo ""
echo "Installed. First time only: on the iPhone open Settings → General →"
echo "VPN & Device Management → your Apple ID → Trust. Then open New to You."
echo "A free install stops opening after 7 days; run this script again then."
