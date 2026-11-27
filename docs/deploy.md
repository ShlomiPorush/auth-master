# Deploying with a zero-downtime rollout

A running installation can move to a new release without downtime with
[docker rollout](https://github.com/wowu/docker-rollout): it starts a container of the new release
beside the running one, waits until the new one is healthy, and then removes the old one. This page
describes what each release publishes for that, what the host's own Compose files need, what happens
while the two containers run together, and how to update by hand when a release cannot roll out.

## Release images

A version tag `vX.Y.Z` starts the workflow "Build and Publish Docker Image". It runs every check of
the Tests workflow first, and pushes the image only when all of them pass.

| Image | Tags |
|---|---|
| `ghcr.io/shlomiporush/auth-master` | `X.Y.Z`, `latest` |
| `shlomip/auth-master` (Docker Hub) | `X.Y.Z`, `latest` |

- Every image is built for `linux/amd64` and `linux/arm64`.
- `latest` moves only to the highest version tag. A release of an older line, or a run that ends
  after a newer release was published, leaves it where it is.
- The tag `X.Y.Z` never moves. The workflow refuses to push a version that GHCR already has, also
  when a run is started again. To correct a published release, publish a new version.
- The image has a health check on `/health`, every 10 seconds. It passes only when the database and
  Redis answer. While the service waits for first-time setup, it passes when Redis answers. It fails
  while the file `/tmp/drain` exists (see [The old container's stop](#the-old-containers-stop)).

## Image labels

Each image carries two labels that say whether it can replace the running release with a rollout:

| Label | Value |
|---|---|
| `auth-master.compose-sha256` | The sha256 of `docker-compose.yml` in the repository at that release. |
| `auth-master.rollout-from` | The oldest running release that this image may run beside. |

A newer release can roll out only when its `auth-master.compose-sha256` equals the running image's,
and the running release is not older than its `auth-master.rollout-from`. Going back to an older
release is always an update by hand: the running release may have changed data that the older one
cannot use. Otherwise, update it by hand (see
[Update by hand](#update-by-hand)). docker rollout does not read these labels: compare them before a
rollout, or let a deployment tool do it. The host's own `docker-compose.yml` is never compared with
the label: a changed hash means the release changed the example file, so the host's file may need the
same change.

To compare them, in the directory with `docker-compose.yml`:

```bash
NEW=X.Y.Z
LABELS='{{index .Config.Labels "auth-master.compose-sha256"}} {{index .Config.Labels "auth-master.rollout-from"}}'
docker inspect --format "running: $LABELS" $(docker compose ps -q auth-service)
docker pull "ghcr.io/shlomiporush/auth-master:$NEW" &&
  docker image inspect --format "new: $LABELS" "ghcr.io/shlomiporush/auth-master:$NEW"
```

Releases published before these labels existed have none, and they do not drain before they stop.
The first release with `docker/rollout.txt` therefore has its own version as `auth-master.rollout-from`
and is updated by hand; later releases can roll out from it.

### Releases that cannot roll out

No change to the start-up schema can roll out: start-up requires exactly the tables of its own
release (a new table makes the new container refuse the old database, and an old container that
restarts later refuses the new one), and adding a column or an index takes locks that wait behind the
running release's reports and writes. So a release rolls out only when its start-up schema code is
the same as the running release's, and nothing else it changes is something the running release
cannot run beside.

`docker/rollout.txt` declares this:

- `schema <fingerprint>`: one line, the sha256 of the start-up schema code (`services/app/schema.py`,
  `services/app/installation.py`, and the functions of the zone name index). The test
  `services/tests/test_rollout_declarations.py` fails when that code changed and names the new value
  to write. Any edit there, a comment included, makes the release one that is updated by hand. The
  same test fails when a schema statement appears in another module, except the database setup and
  move operations, which run only while one process uses the data directory.
- `change <name>`: one line for each other change an older release cannot run beside, never removed
  or edited: the files in the data directory (such as `data/database.json` or `data/.proc`), the keys
  or format of what the service keeps in Redis (such as sessions), the meaning of stored values, the
  data steps at start-up (the installation row, the zones copy, the log purge mark, the settings
  sync), and a dashboard file added, removed or renamed (a page from one release would ask the other
  for a file it does not have).

`.github/rollout-from.sh` derives `auth-master.rollout-from` when a version is published. It walks
the release tags `vX.Y.Z` below the version, from the highest down, while each tag has the same
`schema` line and every `change` line of the version; the lowest tag reached is the label, or the
version itself when the highest tag already differs. It refuses to publish when a `change` line of an
earlier tag is missing, when a newer release tag declares a schema or change that the version lacks
(a backport that the newer release's label could cover), and when the tag is not `vX.Y.Z` with plain
numbers.

## What the host needs

The host keeps its own `docker-compose.yml` and `.env`. They are never compared with the repository's
files. For a rollout of the service `auth-service`:

- `name: auth-master` at the top of `docker-compose.yml`.
- `image: ghcr.io/shlomiporush/auth-master:${VERSION}` on `auth-service`.
- `.env` has exactly one `VERSION=X.Y.Z` line. A rollout changes only that line. Do not also set
  `VERSION` in the shell environment: Compose prefers it to `.env`.
- `auth-service` has no `container_name`, no `ports`, no `network_mode: host`, no `build`, and one
  replica, because a rollout runs two containers of it at once.
- No `pull_policy: always` on `auth-service`.
- A reverse proxy reaches the service on a shared network and routes only to healthy containers, for
  example Traefik with its Docker provider. Set `FORWARDED_ALLOW_IPS` to the proxy's address (see
  "Behind a reverse proxy" in the README).
- Both containers use the same `./data` directory and the same `.env`, so they share the database
  configuration and the secrets.
- No `healthcheck` on `auth-service`: one there replaces the image's, which carries the drain. The
  tracked example `docker-compose.yml` defines one; leave it out of the host's file.
- `stop_grace_period: 30s` on `auth-service`, so a request still running when the old container
  stops has time to finish.

An example, with Traefik on an external network named `proxy`:

```yaml
name: auth-master

services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --requirepass ${REDIS_PASSWORD}
    networks:
      - internal
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  auth-service:
    image: ghcr.io/shlomiporush/auth-master:${VERSION}
    restart: unless-stopped
    stop_grace_period: 30s
    env_file:
      - .env
    environment:
      REDIS_DEFAULT_URL: redis://:${REDIS_PASSWORD}@redis:6379
    volumes:
      - ./data:/app/data
    depends_on:
      redis:
        condition: service_healthy
    networks:
      - internal
      - proxy
    labels:
      traefik.enable: "true"
      traefik.docker.network: proxy
      traefik.http.routers.auth-master.rule: Host(`auth.example.com`)
      traefik.http.services.auth-master.loadbalancer.server.port: "8080"

networks:
  internal:
  proxy:
    external: true
```

`.env` holds the secrets that `tools/generate-env.sh` or `tools/generate-env.ps1` creates, the
`VERSION` line, and the proxy's address:

```dotenv
VERSION=X.Y.Z
FORWARDED_ALLOW_IPS=<the proxy's address>
```

## Roll out a release

When the labels allow it, in the directory with `docker-compose.yml` and `.env`:

```bash
NEW=X.Y.Z
docker pull "ghcr.io/shlomiporush/auth-master:$NEW" &&
  sed -i "s/^VERSION=.*/VERSION=$NEW/" .env &&
  docker rollout --timeout 300 --wait-after-healthy 3 auth-service
```

`--wait-after-healthy` keeps both containers serving for a few seconds after the new one is healthy,
so the reverse proxy routes to the new one. If the new container does not become healthy, docker
rollout removes it and the old one keeps serving; set `VERSION` in `.env` back to the running release.

### The old container's stop

The image carries docker rollout's drain, the label
`docker-rollout.pre-stop-hook="touch /tmp/drain && sleep 45"`. Before docker rollout stops the old
container, it runs that hook in it: the health check fails while `/tmp/drain` exists, so the
container turns unhealthy within about 36 seconds, the reverse proxy stops routing to it, and only
then does docker rollout stop it. A rollout takes about 45 seconds longer for that. The container
removes `/tmp/drain` when it starts, so a restarted container is healthy again.

A request still running in the old container when it stops finishes if it needs less than the 30
seconds of `stop_grace_period`; a longer one is cut. Requests that start after the proxy dropped the
old container go to the new one.

docker rollout v0.14 has two limits here. It stops the old container whatever the hook's exit code,
so a hook that cannot run gives no drain. And it does not check the new container again during the
hook. It also stops the old container when the new one exits right after it starts
([wowu/docker-rollout#75](https://github.com/wowu/docker-rollout/issues/75)). After a rollout, check
that `docker compose ps auth-service` shows one healthy container. If it shows none, set `VERSION` in
`.env` back to the release that ran before and start it with `docker compose up -d --wait auth-service`.

### A rollout that was interrupted

Before you start a rollout again, look at what runs, with each container's image:

```bash
docker compose ps auth-service --format '{{.ID}} {{.Image}} {{.Status}}'
```

- Two healthy containers (interrupted before the drain): keep the one on the image that `.env`
  names. Drain the other first, wait until it is unhealthy and 5 seconds more, then remove it:

  ```bash
  OLD=<the ID of the container to remove>
  docker exec "$OLD" touch /tmp/drain
  ```

  When `docker inspect -f '{{.State.Health.Status}}' "$OLD"` says `unhealthy`, wait 5 seconds and run
  `docker stop "$OLD" && docker rm "$OLD"`.
- A healthy new container and an old one that is draining: wait until the old one is unhealthy and
  5 seconds more, then stop and remove it as above.
- One container, unhealthy because `/tmp/drain` exists: remove the file with
  `docker exec <ID> rm -f /tmp/drain` and wait until it is healthy.

Start a rollout again only when exactly one healthy container runs.

## While two containers run

For about a minute, from the new container's start until the proxy drops the old one, the old and
the new container serve together:

- `/validate`, the token API and the dashboard work in both. They use the same database, and admin
  sessions are in the same Redis.
- The `/validate` rate limit is counted in each container, so a client can make up to twice the
  limit during the overlap. The new container starts with empty counts, as after a restart.
- Database changes on the Settings page (first-time setup, a new connection, the move to
  PostgreSQL) need a single process. They are refused while both containers run. Try again after
  the rollout.
- The dashboard's scripts and stylesheets have the same names in every release and are not cached
  without a check, so none is missing at the switch. A browser that loads the dashboard while both
  containers serve can still get the page from one release and a script from the other. Only the
  admin dashboard is affected, not the API; reloading the page fixes it.
- Background work runs in both containers: removing expired tokens and old log rows, and the Usage
  summary. Each step is safe to run twice: the summary and the log purge take a database lock, every
  log row is counted in the same transaction that marks it, and an expired token is removed only if
  its expiry did not change since it was read.

A Redis connection saved on the Settings page takes effect only at the next restart. If the Settings
page says that a restart is needed, restart the service before a rollout, or update by hand.
Otherwise the two containers use different Redis servers during the overlap, and sign-in fails some
of the time.

## Update by hand

Use this for a release that cannot roll out, and for the first release with `docker/rollout.txt`. The service is
down from the stop until the new container is healthy. Read the release's entry in `CHANGELOG.md`
first, and apply any change to `docker-compose.yml` to the host's file. Then, in the directory with
`docker-compose.yml` and `.env`:

```bash
NEW=X.Y.Z
docker pull "ghcr.io/shlomiporush/auth-master:$NEW" &&
  sed -i "s/^VERSION=.*/VERSION=$NEW/" .env &&
  docker compose stop auth-service &&
  docker compose up -d --wait --wait-timeout 300
```

The pull comes first, so a version that cannot be pulled changes nothing.

## A release that stopped half way

A run of "Build and Publish Docker Image" can stop after part of the release is published. Started
again, it refuses once GHCR has `X.Y.Z`. Check each part and complete only what is missing, from the
image already in GHCR. Never build the same version again.

1. Check the version on both registries:

   ```bash
   docker buildx imagetools inspect ghcr.io/shlomiporush/auth-master:X.Y.Z
   docker buildx imagetools inspect shlomip/auth-master:X.Y.Z
   ```

2. If only Docker Hub lacks the version, copy it from GHCR:

   ```bash
   docker buildx imagetools create -t shlomip/auth-master:X.Y.Z ghcr.io/shlomiporush/auth-master:X.Y.Z
   ```

3. If `latest` on a registry does not point at this version, no newer version is published, and no
   publish run is in progress, move it on that registry only, for example on Docker Hub:

   ```bash
   docker buildx imagetools create -t shlomip/auth-master:latest ghcr.io/shlomiporush/auth-master:X.Y.Z
   ```

4. If the GitHub release is missing, re-run the failed `publish-release` job. If that job was
   skipped because the push failed, create the release from the changelog:

   ```bash
   bash .github/release-notes.sh X.Y.Z > notes.md &&
     gh release create vX.Y.Z --title vX.Y.Z --notes-file notes.md --verify-tag
   ```
