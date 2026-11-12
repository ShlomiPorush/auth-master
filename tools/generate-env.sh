#!/bin/sh
# Create .env in the current directory with new random secrets for docker compose.
#
#   sh tools/generate-env.sh
#
# Run it once, in the directory that holds docker-compose.yml. It never
# overwrites or changes an existing .env: it refuses if anything already exists
# at that path. The file is readable by its owner only.
set -eu

out=.env
generated="ADMIN_API_KEY SESSION_SECRET APP_ENCRYPTION_KEY REDIS_PASSWORD"

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

if [ "$#" -ne 0 ]; then
    fail "Usage: sh tools/generate-env.sh (no arguments). It writes .env in the current directory."
fi

if [ -e "$out" ] || [ -L "$out" ]; then
    fail "$out already exists, so nothing was changed. To replace one value in it, edit the file and use a new value from: od -An -tx1 -N32 /dev/urandom | tr -d ' \\t\\n'"
fi

# Inside a git work tree, both the result and the temporary file must be ignored,
# so the secrets cannot be committed by accident.
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    for name in "$out" ".env.tmpcheck"; do
        if ! git check-ignore -q -- "$name"; then
            fail "Git does not ignore '$name' in this directory. Add '.env' and '.env.*' to .gitignore first. Nothing was changed."
        fi
    done
fi

[ -r /dev/urandom ] || fail "/dev/urandom is not readable. Nothing was changed."

random_hex() {
    value=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \t\n')
    case $value in
        *[!0-9a-f]*) return 1 ;;
    esac
    [ "${#value}" -eq 64 ] || return 1
    printf '%s' "$value"
}

umask 077
tmp=$(mktemp ./.env.XXXXXX) || fail "Could not create a temporary file here. Nothing was changed."
trap 'rm -f "$tmp"' EXIT
trap 'exit 1' HUP INT TERM

{
    printf '# Created by tools/generate-env.sh. Keep this file private; never commit it.\n'
    printf '# Optional settings: see .env.example.\n'
    for name in $generated; do
        value=$(random_hex) || fail "Could not read 32 random bytes from /dev/urandom. Nothing was changed."
        printf '%s=%s\n' "$name" "$value"
    done
} >"$tmp"

# ln refuses an existing target, so a file created by someone else in the
# meantime is never replaced.
ln "$tmp" "$out" 2>/dev/null || fail "$out appeared while the secrets were generated. Nothing was changed."

# If a directory or a link to one appeared at $out, ln put the link inside it.
if ! { [ -f "$out" ] && [ ! -L "$out" ] && [ "$out" -ef "$tmp" ]; }; then
    stray="$out/$(basename "$tmp")"
    if [ "$stray" -ef "$tmp" ]; then
        rm -f "$stray"
    fi
    fail "$out is not a regular file created by this script. Nothing was created."
fi

printf 'Created %s with new random values for: %s\n' "$out" "$generated"
printf 'Add optional settings from .env.example if you need them, then run: docker compose up -d\n'
