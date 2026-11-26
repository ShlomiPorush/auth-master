#!/usr/bin/env bash
set -euo pipefail

# The oldest running release that a new release may run beside during a zero-downtime
# rollout (image label auth-master.rollout-from). Raise it to the version being
# published when that release cannot run beside older ones; see docs/deploy.md.
ROLLOUT_FROM=2.5.0

version=${1:?usage: rollout-from.sh VERSION}
semver_core='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'

if [[ ! $version =~ $semver_core ]]; then
    echo "The version '$version' is not X.Y.Z (digits, no leading zeros, no prefix or suffix)." >&2
    exit 1
fi
if [[ ! $ROLLOUT_FROM =~ $semver_core ]]; then
    echo "ROLLOUT_FROM '$ROLLOUT_FROM' is not X.Y.Z." >&2
    exit 1
fi
if [ "$(printf '%s\n%s\n' "$ROLLOUT_FROM" "$version" | sort -V | head -n 1)" != "$ROLLOUT_FROM" ]; then
    echo "ROLLOUT_FROM $ROLLOUT_FROM is newer than the version $version being published." >&2
    exit 1
fi
echo "$ROLLOUT_FROM"
