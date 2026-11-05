#!/usr/bin/env bash
# Fail if git tracks any signing secret or per-machine signing file
# (WHO-16 S-3, S-4). Runs in CI; you can run it locally too.
set -euo pipefail

cd "$(dirname "$0")/.."

found="$(git ls-files -- \
    '*.p12' '*.mobileprovision' '*.cer' '*.p8' \
    '*Signing.local.xcconfig')"

if [[ -n "$found" ]]; then
    echo "error: these signing files are tracked by git and must not be:" >&2
    printf '%s\n' "$found" | sed 's/^/  /' >&2
    echo "Remove them with 'git rm --cached <file>' and keep them only on your Mac." >&2
    exit 1
fi
echo "OK: no signing files tracked."
