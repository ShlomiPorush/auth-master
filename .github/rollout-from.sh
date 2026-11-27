#!/usr/bin/env bash
set -euo pipefail

version=${1:?usage: rollout-from.sh VERSION}
file=docker/rollout.txt
semver_core='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'

fail() {
    echo "$*" >&2
    exit 1
}

declared() {
    sed -E 's/[[:space:]]+$//' | grep -E '^(schema|change) ' || true
}

has_file() {
    git cat-file -e "v$1:$file" 2>/dev/null
}

lines_at() {
    git show "v$1:$file" | declared
}

changes_of() {
    grep '^change ' <<<"$1" || true
}

older() {
    [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" = "$1" ]
}

has_all() {
    local line
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        grep -qxF -- "$line" <<<"$2" || return 1
    done <<<"$1"
}

[[ $version =~ $semver_core ]] || fail "The version '$version' is not X.Y.Z (digits, no leading zeros, no prefix or suffix)."
[ -f "$file" ] || fail "$file is missing."

head_lines=$(declared <"$file")
[ "$(grep -c '^schema ' <<<"$head_lines")" = 1 ] || fail "$file needs exactly one schema line."
head_schema=$(grep '^schema ' <<<"$head_lines")
head_changes=$(changes_of "$head_lines")

all_tags=$(git tag -l 'v*')
release_tags=$(sed -En 's/^v((0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))$/\1/p' <<<"$all_tags" | sort -V)
below=()
above=()
while IFS= read -r tag; do
    [ -z "$tag" ] && continue
    if older "$tag" "$version"; then
        below+=("$tag")
    elif older "$version" "$tag"; then
        above+=("$tag")
    fi
done <<<"$release_tags"

for tag in "${below[@]}"; do
    has_file "$tag" || continue
    tag_lines=$(lines_at "$tag")
    if ! has_all "$(changes_of "$tag_lines")" "$head_changes"; then
        fail "v$tag declares a change line that $file no longer has. Change lines are never removed."
    fi
done

for tag in "${above[@]}"; do
    has_file "$tag" || continue
    tag_lines=$(lines_at "$tag")
    tag_changes=$(changes_of "$tag_lines")
    if ! grep -qxF -- "$head_schema" <<<"$tag_lines" || ! has_all "$tag_changes" "$head_changes" || ! has_all "$head_changes" "$tag_changes"; then
        fail "v$tag is newer and its schema or change lines differ from $version's, and its rollout-from could cover $version. A backport must declare exactly what the newer release declares."
    fi
done

rollout_from=$version
for ((i = ${#below[@]} - 1; i >= 0; i--)); do
    tag=${below[i]}
    has_file "$tag" || break
    tag_lines=$(lines_at "$tag")
    grep -qxF -- "$head_schema" <<<"$tag_lines" || break
    has_all "$head_changes" "$tag_lines" || break
    rollout_from=$tag
done
echo "$rollout_from"
