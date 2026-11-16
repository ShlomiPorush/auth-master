#!/usr/bin/env bash
# Check how the built image handles the data directory (docker/entrypoint.sh and
# docker/data_dir.py): it takes ownership of /app/data, runs the service as uid
# 10001, refuses to start when that user cannot use the data, and upgrades data
# that an older image wrote as root.
#
#   bash .github/image-check.sh IMAGE
#
# Every secret is generated for this run. Everything the run creates carries a
# label and is removed at the end. Named volumes, so it runs the same on Linux
# and on Docker Desktop.
set -euo pipefail

IMAGE=${1:?usage: image-check.sh IMAGE}
OLD_IMAGE=${OLD_IMAGE:-ghcr.io/shlomiporush/auth-master:2.2.0}
RUN=imgchk-$(openssl rand -hex 4)

SESSION_SECRET=$(openssl rand -hex 32)
APP_ENCRYPTION_KEY=$(openssl rand -hex 32)
REDIS_PASSWORD=$(openssl rand -hex 32)
REDIS_URL="redis://:$REDIS_PASSWORD@redis:6379/0"
export SESSION_SECRET APP_ENCRYPTION_KEY REDIS_PASSWORD REDIS_URL

cleanup() {
    docker ps -aq --filter "label=$RUN" | xargs -r docker rm -f >/dev/null
    docker volume ls -q --filter "label=$RUN" | xargs -r docker volume rm >/dev/null
    docker network rm "$RUN" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

volume() {
    docker volume create --label "$RUN" "$RUN-$1" >/dev/null
    echo "$RUN-$1"
}

# As root, in a throwaway container of the image, with the volume at /d.
on_volume() {
    local vol=$1
    shift
    docker run --rm --label "$RUN" -v "$vol:/d" --entrypoint sh "$IMAGE" -c "$*"
}

# start NAME [docker run options...] IMAGE: the service, detached.
start() {
    local name=$1
    shift
    docker run -d --label "$RUN" --name "$RUN-$name" --network "$RUN" \
        -e SESSION_SECRET -e APP_ENCRYPTION_KEY -e REDIS_URL "$@" >/dev/null
}

healthy() {
    local name=$RUN-$1
    for _ in $(seq 90); do
        if [ "$(docker inspect -f '{{.State.Running}}' "$name")" != true ]; then
            docker logs "$name" >&2 || true
            fail "$1 stopped before /health answered"
        fi
        if docker exec "$name" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    docker logs "$name" >&2 || true
    fail "$1 did not answer /health"
}

# refused NAME TEXT: the container exits with an error and its log has TEXT.
refused() {
    local name=$RUN-$1 code
    code=$(timeout 90 docker wait "$name") || fail "$1 is still running"
    [ "$code" != 0 ] || fail "$1 exited with 0"
    docker logs "$name" 2>&1 | grep -qF -- "$2" || { docker logs "$name" >&2; fail "$1: log lacks '$2'"; }
}

stop_cleanly() {
    local name=$RUN-$1
    docker stop -t 30 "$name" >/dev/null
    [ "$(docker inspect -f '{{.State.ExitCode}}' "$name")" = 0 ] || fail "$1 did not exit with 0 on SIGTERM"
    docker logs "$name" 2>&1 | grep -q "Finished server process" || fail "$1 did not shut down uvicorn"
}

not_service_owned() {
    on_volume "$1" 'find /d \( ! -user 10001 -o ! -group 10001 \) -print'
}

docker network create --label "$RUN" "$RUN" >/dev/null
docker run -d --label "$RUN" --name "$RUN-redis" --network "$RUN" --network-alias redis \
    -e REDIS_PASSWORD redis:7-alpine sh -c 'exec redis-server --requirepass "$REDIS_PASSWORD"' >/dev/null

marker=$(openssl rand -hex 8)
# A row in the service's own key-value table: an extra table would make the database
# look like another application's, which the service refuses to adopt.
put_marker='python -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"INSERT INTO app_state (key, value) VALUES (?, ?)\", (\"ci_marker\", sys.argv[2])); c.commit()"'
get_marker='python -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute(\"SELECT value FROM app_state WHERE key = ?\", (\"ci_marker\",)).fetchone()[0])"'

echo "A. upgrade data that $OLD_IMAGE wrote as root"
a=$(volume a)
start old-a -v "$a:/app/data" "$OLD_IMAGE"
healthy old-a
docker stop "$RUN-old-a" >/dev/null
on_volume "$a" "$put_marker /d/auth.db $marker &&
    echo x > /d/read-only && chmod 0444 /d/read-only &&
    mkdir /d/sub && echo y > /d/sub/f && chmod 0555 /d/sub &&
    touch /d/already && chown 10001:10001 /d/already"
[ -n "$(not_service_owned "$a")" ] || fail "A: the old image did not leave root-owned files"
start a -v "$a:/app/data" "$IMAGE"
healthy a
docker exec "$RUN-a" grep -qP '^Uid:\t10001\t10001\t10001\t10001$' /proc/1/status || fail "A: the service does not run as uid 10001"
docker exec "$RUN-a" grep -qP '^Gid:\t10001\t10001\t10001\t10001$' /proc/1/status || fail "A: the service does not run as gid 10001"
docker exec "$RUN-a" grep -qP '^CapEff:\t0+$' /proc/1/status || fail "A: the service kept capabilities"
leftover=$(not_service_owned "$a")
[ -z "$leftover" ] || fail "A: not owned by 10001: $leftover"
[ "$(on_volume "$a" 'stat -c %a /d/sub /d/read-only' | tr '\n' ' ')" = "755 644 " ] || fail "A: owner bits were not added"
[ "$(on_volume "$a" "$get_marker /d/auth.db")" = "$marker" ] || fail "A: the data changed"
stop_cleanly a
docker start "$RUN-a" >/dev/null
healthy a
stop_cleanly a

echo "B. a root-owned auth.db with two hard links"
b=$(volume b)
on_volume "$b" 'echo x > /d/auth.db && ln /d/auth.db /d/auth.db.copy && chmod 0600 /d/auth.db'
start b -v "$b:/app/data" "$IMAGE"
refused b "/app/data/auth.db: cannot read and write the file"
[ "$(on_volume "$b" 'stat -c %u /d/auth.db')" = 0 ] || fail "B: a hard-linked file was changed"

echo "C. a symlink to a root-owned file outside the data directory"
c=$(volume c)
c_other=$(volume c-other)
on_volume "$c_other" 'echo s > /d/secret && chmod 0600 /d/secret'
on_volume "$c" 'ln -s /other/secret /d/link'
start c -v "$c:/app/data" -v "$c_other:/other" "$IMAGE"
refused c "/app/data/link: cannot read and write the file"
[ "$(on_volume "$c_other" 'stat -c "%u %a" /d/secret')" = "0 600" ] || fail "C: the symlink target was changed"

echo "D. started as another user, on root-owned data"
d=$(volume d)
on_volume "$d" 'echo x > /d/auth.db'
start d --user 12345:12345 -v "$d:/app/data" "$IMAGE"
refused d "The service user (uid 12345) cannot use the data directory"

echo "E. started as another user with access through a supplementary group"
e=$(volume e)
# A file in the volume, so Docker does not copy the image's /app/data (owner 10001) into it.
on_volume "$e" 'touch /d/keep && chgrp -R 20000 /d && chmod 2775 /d && chmod 0664 /d/keep'
start e --user 12345:12345 --group-add 20000 -v "$e:/app/data" "$IMAGE"
healthy e

echo "F. a read-only mount that the service user already owns"
f=$(volume f)
on_volume "$f" 'chown 10001:10001 /d'
start f -v "$f:/app/data:ro" "$IMAGE"
refused f "cannot use the data directory"

echo "G. a SQLite file outside /app/data, written as root by $OLD_IMAGE"
g=$(volume g)
start old-g -v "$g:/db" -e DATABASE_URL=sqlite:////db/auth.db "$OLD_IMAGE"
healthy old-g
docker stop "$RUN-old-g" >/dev/null
on_volume "$g" "$put_marker /d/auth.db $marker"
start g -v "$g:/db" -e DATABASE_URL=sqlite:////db/auth.db "$IMAGE"
code=$(timeout 90 docker wait "$RUN-g") || fail "G: started on a database it cannot write"
[ "$code" != 0 ] || fail "G: exited with 0"
on_volume "$g" 'chown -R 10001:10001 /d'
docker rm "$RUN-g" >/dev/null
start g -v "$g:/db" -e DATABASE_URL=sqlite:////db/auth.db "$IMAGE"
healthy g
[ "$(on_volume "$g" "$get_marker /d/auth.db")" = "$marker" ] || fail "G: the data changed"

echo "H. a file the service user can write but not read"
h=$(volume h)
on_volume "$h" 'echo x > /d/auth.db && ln /d/auth.db /d/auth.db.copy && chown 0:10001 /d/auth.db && chmod 0620 /d/auth.db'
start h -v "$h:/app/data" "$IMAGE"
refused h "/app/data/auth.db: cannot read and write the file"

echo "All image checks passed."
