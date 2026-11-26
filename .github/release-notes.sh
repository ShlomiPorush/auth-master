#!/usr/bin/env bash
set -euo pipefail

version=${1:?usage: release-notes.sh VERSION}
heading="## [${version}]"

if ! awk -v hdr="$heading" 'index($0, hdr) == 1 { found = 1 } END { exit !found }' CHANGELOG.md; then
    echo "CHANGELOG.md has no '$heading' section." >&2
    echo "Describe the release there on main, then move the tag." >&2
    exit 1
fi

notes=$(awk -v hdr="$heading" '
    index($0, hdr) == 1 { inside = 1; next }
    inside && /^## / { exit }
    inside && /^\[[^]]+\]: / { exit }
    inside { print }
' CHANGELOG.md)

if ! grep -q '[^[:space:]]' <<<"$notes"; then
    echo "The '$heading' section in CHANGELOG.md is empty." >&2
    echo "Describe the release there on main, then move the tag." >&2
    exit 1
fi
printf '%s\n' "$notes"
