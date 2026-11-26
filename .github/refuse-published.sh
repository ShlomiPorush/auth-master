#!/usr/bin/env bash
set -euo pipefail

image=${1:?usage: refuse-published.sh REGISTRY/OWNER/NAME VERSION}
version=${2:?usage: refuse-published.sh REGISTRY/OWNER/NAME VERSION}
registry=${image%%/*}
repository=${image#*/}
manifest_types='application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'

cannot_check() {
    echo "Could not check whether $image:$version is already published ($1). Nothing was pushed." >&2
    exit 1
}

token_url="https://$registry/token?scope=repository:$repository:pull&service=$registry"
if [ -n "${REGISTRY_PASSWORD:-}" ]; then
    response=$(curl -fsS --max-time 20 -K <(printf 'user = "%s:%s"\n' "${REGISTRY_USER:-token}" "$REGISTRY_PASSWORD") "$token_url") ||
        cannot_check "the registry gave no token"
else
    response=$(curl -fsS --max-time 20 "$token_url") || cannot_check "the registry gave no token"
fi
token=$(jq -r '.token // empty' <<<"$response")
[ -n "$token" ] || cannot_check "the registry gave no token"

manifest_status() {
    curl -sS -o /dev/null -w '%{http_code}' --max-time 20 --head \
        -H @<(printf 'Authorization: Bearer %s\n' "$token") -H "Accept: $manifest_types" \
        "https://$registry/v2/$repository/manifests/$1"
}

latest=$(manifest_status latest) || cannot_check "the registry did not answer"
[ "$latest" = 200 ] || cannot_check "the registry answered $latest for latest"

case "$(manifest_status "$version")" in
    404) echo "$image:$version is not published yet." ;;
    200)
        echo "$image:$version is already published, and a version tag never moves. Publish a new version." >&2
        exit 1
        ;;
    *) cannot_check "unexpected answer for $version" ;;
esac
