# Auth Service

A self-hosted token authentication and authorization service. Manage API tokens, define permission zones, and validate access — all through a modern web dashboard or REST API.

## Features

- **Token Management** — Create, edit, revoke, and reveal API tokens with granular per-zone permissions
- **Zone-Based Authorization** — Define zones (areas) and assign `read` / `write` / `delete` / `all` access levels
- **API Key Management** — Create scoped API keys for programmatic access (e.g. n8n, CI/CD)
- **MFA-Protected** — Admin dashboard requires TOTP two-factor authentication
- **Passkeys** — Sign in to the dashboard with a passkey (fingerprint, face or screen lock) instead of the password and authenticator code, and confirm token and API key copies with it. See [Passkeys](#passkeys)
- **Fast Validation** — Token validation with rate limiting; revocations and other token changes apply to the next request
- **Dual Database Support** — PostgreSQL for production, SQLite for lightweight/dev setups
- **Modern Dashboard** — Admin UI with light and dark themes and phone layouts for managing tokens, zones, and API keys
- **n8n Integration** — Community nodes for workflow automation: action node + webhook trigger with built-in token validation ([n8n-nodes-auth-service](https://www.npmjs.com/package/n8n-nodes-auth-service))

## Quick Start

### Docker Compose

See [docker-compose.yml](docker-compose.yml)

### First Run

1. Create `.env` with new random secrets, in the directory that holds `docker-compose.yml`:
   - Linux or macOS: `sh tools/generate-env.sh`
   - Windows: `pwsh tools/generate-env.ps1`

   The script writes `SESSION_SECRET`, `APP_ENCRYPTION_KEY` and `REDIS_PASSWORD`, makes the file readable by you only, and never replaces an existing `.env`. Keep the file private and out of version control. Optional settings are listed in [.env.example](.env.example).
2. Start the containers: `docker compose up -d`
3. Open `http://localhost:8080` — you'll be redirected to the setup wizard
4. Create your admin account and set up MFA (TOTP). The service starts with no accounts, and the first person who completes this setup becomes the admin, so complete it before the service can be reached from other machines
5. Log in and start managing tokens and zones. The time zone, log retention, the Secure cookie, the rate limit and the Redis connection are on the Settings page.

The service refuses to start while a required secret is missing, empty, or has a value that was ever published in this repository, and its log names each setting and how to fix it.

## Environment Variables

You manage most settings on the dashboard's Settings page. The environment holds only what
the service needs before it can read its own settings, and values that override a Settings
page field.

### Environment only

| Variable | Default | Description | Why it is not on the Settings page |
|---|---|---|---|
| `DATABASE_URL` | `sqlite:///data/auth.db` | Database connection string. Use `postgres://…` for PostgreSQL. A password in it must not be a published value, and a URL with more than one `@` before the host is refused (write `@` as `%40`) | The settings are stored in this database |
| `PGPASSWORD` | _(empty)_ | Optional PostgreSQL password when `DATABASE_URL` has none; checked like the others | Part of the database connection |
| `SESSION_SECRET` | _(required)_ | Together with `APP_ENCRYPTION_KEY`, derives the key that encrypts stored MFA secrets, revealable token and API key values, and the saved Redis URL. Alone, it also protects admin sessions and unfinished MFA setups in Redis. Use a different value for every installation | The key that encrypts stored secrets |
| `APP_ENCRYPTION_KEY` | _(required)_ | See `SESSION_SECRET` | The key that encrypts stored secrets |
| `REDIS_PASSWORD` | _(required with the bundled Redis)_ | Password of the bundled Redis server. `docker-compose.yml` builds `REDIS_DEFAULT_URL` from it | Set on the Redis container by the deployment |
| `REDIS_DEFAULT_URL` | _(empty)_ | Redis URL used while no Redis URL is saved on the Settings page and `REDIS_URL` is not set. `docker-compose.yml` sets it to the bundled Redis | Deployment wiring for the bundled stack |
| `ADMIN_API_KEY` | _(empty)_ | Optional master API key with every scope. Empty means no master key. The API Keys page shows an active master key as a locked row | Kept for integrations that already use it. Keys created on the API Keys page replace it |
| `ROOT_PATH` | _(empty)_ | URL path prefix when a reverse proxy serves the portal under a path, such as `/auth`. The Settings page shows it read-only | It must match the reverse proxy's route, and a wrong saved value would make the Settings page unreachable |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` | Read by uvicorn, not by the app: the addresses of reverse proxies whose `X-Forwarded-For` header is trusted. The Settings page shows it read-only. See [Behind a reverse proxy](#behind-a-reverse-proxy). The bundled compose file passes it to the container | uvicorn reads it when the server starts, before the app runs |
| `ALLOWED_AREAS` | `orders,billing,webhooks` | Deprecated. On the first start after the upgrade to this version, an empty zones table gets these zones once. The service does not read it after that | Zones are managed on the Zones page |

### Overrides of Settings page fields

Each variable below overrides a field on the Settings page. When the variable is set in the
environment or in `.env`, the page shows the field locked and names the variable. Remove
the variable to manage the value on the page.

| Variable | Default | Settings page field |
|---|---|---|
| `TZ` | `UTC` | **Time zone** for dates on every page and in the Usage report, such as `Asia/Jerusalem`. An empty value or a name that is not a time zone keeps dates in UTC, and the page says so |
| `COOKIE_SECURE` | `false` | **Secure session cookie**. Set it to `true` when admins use the portal over HTTPS. See [Remote access and HTTPS](#remote-access-and-https). An empty value counts as not set |
| `ACTIVITY_LOG_RETENTION_DAYS` | `30` | **Activity log retention**, from 1 to 3650 days. A value outside that range stops the start |
| `ACCESS_LOG_RETENTION_DAYS` | `30` | **Access log retention**, from 1 to 3650 days; also the longest period the Usage page can count. A value outside that range stops the start |
| `RATE_LIMIT_MAX_REQ` | `120` | **Requests per window**. See [Rate limit settings](#rate-limit-settings) |
| `RATE_LIMIT_WINDOW_MS` | `60000` | **Window**, in milliseconds |
| `REDIS_URL` | _(empty)_ | **Redis connection**. See [Redis connection](#redis-connection) |
| `TOTP_ISSUER` | `AuthService` | **Name in authenticator apps**, on the MFA step of the first setup. The name is used only there |
| `PASSKEY_ORIGIN` | _(empty)_ | **Passkey address**, such as `https://auth.example.com`. Not copied to the page: when you remove it, the saved address applies. An empty value counts as not set. See [Passkeys](#passkeys) |

At each start, the service copies `TZ`, `COOKIE_SECURE` and the two retention values from
the environment to the Settings page when they differ from the saved values. Each copy is a
new version, saved by `environment` and shown in the Activity Log. So when you remove one of
these variables later, the field keeps its last value instead of going back to the default.
The service does not copy a `TZ` that is not a time zone. `REDIS_URL` is copied only after a
check against it passes (see [Redis connection](#redis-connection)). The rate limit variables
are not copied.

### General settings

The **General** panel of the Settings page (`/admin/settings`) holds the time zone, the
retention of both logs and the Secure session cookie. It works like the rate limit panel:
each save is a new version in the table `app_settings`, a save applies at once in the server
process that saved it and within about 15 seconds in any other, and two admins who save from
the same version are told about the conflict.

- **Time zone**: the tab that saves the new zone shows it at once. Other open tabs show
  dates in the previous zone until they are reloaded. The Usage report always names its hours
  and days in the zone it was counted in.
- **Log retention**: a lower value deletes older entries within about a minute. The page
  first shows how many entries are older than the new limit and asks you to confirm. A server
  process whose own environment value differs from the saved value does not delete by its own
  value. The page shows that cleanup as skipped, with the reason.
- **Secure session cookie**: browsers then send the session cookie only over HTTPS. If you
  turn it on from a page opened over plain HTTP, the page asks you to confirm, because signing
  in over HTTP stops working. Sessions that are already open keep working until they end. To
  undo it without the page, set `COOKIE_SECURE=false`.

When a retention value is raised, the Usage page still marks the days that the earlier,
shorter retention already deleted, instead of showing them as zero.

### Remote access and HTTPS

The service speaks plain HTTP on port 8080, and the bundled compose file publishes
that port on every interface of the host. Over plain HTTP, the admin password, the
session cookie, the MFA setup secret, tokens and API keys cross the network
unencrypted, and anyone on the path can read them. The Secure session cookie setting
does not change that.

- To reach the service from other machines, browsers and integrations such as n8n
  alike, put an HTTPS reverse proxy such as Traefik, Caddy or nginx in front of it,
  and use the proxy's `https://` address. Callers on the same Docker network can use
  `http://auth-service:8080`.
- Do not expose plain HTTP port 8080 to networks you do not trust. Allow it only from
  the proxy, with a host firewall, or remove the published port when the proxy runs
  on the same Docker network: in an override file, `ports: !reset []` removes it
  (`ports: []` does not, because Compose merges port lists).
- Behind HTTPS, turn on **Secure session cookie** on the Settings page (or set
  `COOKIE_SECURE=true`, which locks the field), so browsers send the session cookie
  over HTTPS only, and set `FORWARDED_ALLOW_IPS` to the proxy's address or network,
  as described below.

The bundled compose file passes `COOKIE_SECURE` and `FORWARDED_ALLOW_IPS` to the
container, so they work when set in `.env` or as stack variables in a tool such as
Portainer. Docker Compose takes them from the shell or stack variables first, then
from the `.env` file of the project directory, or from the files given with
`--env-file` instead of it, and the result wins over `env_file: .env`. If you start
the stack with `--env-file` and keep these two values only in `.env`, move them to
your env file, or add `--env-file .env` before your other `--env-file` options: a
later file wins, so your own file keeps its values, secrets included. When
`FORWARDED_ALLOW_IPS` is not set at all, the compose file passes uvicorn's default
`127.0.0.1,::1`; an empty value is passed as it is. When `COOKIE_SECURE` is not set,
the compose file passes it empty, which the service reads as not set: the field on the
Settings page is not locked and its saved value applies.

### Behind a reverse proxy

The rate limit counts requests per client address, as uvicorn reports it. An IPv6
address counts with its whole /64 network, so all addresses in one /64 share one
limit: a client cannot get more requests by changing addresses inside its /64, but
a client that holds a larger block still gets one limit per /64 it uses (256 for a
/56, 65,536 for a /48). An IPv4-mapped IPv6 address (`::ffff:192.0.2.1`) counts as
its IPv4 address, and every client whose reported address is not an IP address
shares one limit. Behind a
reverse proxy such as Traefik or nginx in another container, every request arrives
from the proxy's address, so all clients share one limit, and the access log shows
the proxy's address for every request.

To count real client addresses, set `FORWARDED_ALLOW_IPS` to the proxy's address or
network, for example `FORWARDED_ALLOW_IPS=172.18.0.50` or
`FORWARDED_ALLOW_IPS=172.18.0.0/16`. uvicorn then takes the client address from the
`X-Forwarded-For` header, but only on connections from those addresses. Without it,
uvicorn trusts the header only from `127.0.0.1` and `::1`. The Settings page shows
the value.

A network trusts every container on it, not only the proxy: any of them can send its
own `X-Forwarded-For` header and choose the address it is counted under. A single
address trusts only the proxy, but needs the proxy to keep that address.

Requests over the limit are answered `429 Too many requests` with a `Retry-After`
header (the whole seconds until the caller's window ends), and are not written to the
access log. The Access Logs page
shows how many there were and from which addresses, and each server process writes
a `Rate limit:` line to its log at most once per window.

Dashboard sign-in has its own limit, also per client address: 10 attempts in 15
minutes (IPv6 per `/64` network), then `429` with `Retry-After` until the window ends.
It has no exempt list and cannot be changed. The same address rules apply: behind a
proxy that `FORWARDED_ALLOW_IPS` does not trust, all clients share one count of 10,
so 10 wrong passwords from anyone block sign-in for everyone for 15 minutes; and a
trusted network lets its containers choose the address they are counted under.

### Rate limit settings

The Settings page (`/admin/settings`) sets the rate limit and the addresses it never
applies to:

- **Requests per window** and **window length** (whole seconds). When
  `RATE_LIMIT_MAX_REQ` or `RATE_LIMIT_WINDOW_MS` is set in the environment or `.env`,
  that value wins and the field is locked on the page. Remove the variable to manage
  the value on the page.
- **Exempt addresses and networks**: up to 100 entries, one per line: IPv4 or IPv6
  addresses (`203.0.113.10`) or networks (`172.18.0.0/16`). Requests from them are
  never limited or counted. They are still validated and logged. An exempt address is
  not limited at all: behind a reverse proxy that `FORWARDED_ALLOW_IPS` does not
  cover, every caller arrives from the proxy's address, and exempting it removes the
  limit for everyone. The page asks for a confirmation before a change exempts the
  address your own request came from.
- **Reverse proxy addresses**: the value of `FORWARDED_ALLOW_IPS`, shown locked. It is
  read by uvicorn when the server starts, so it is set in the environment only.

Each save is kept as a new version in the table `rate_limit_settings`, with who saved
it, when and from which address; older versions are never changed or deleted, and
log retention does not remove them. The Activity Log shows each change as well. If
two admins save from the same version, the second one is told that the settings
changed and can compare before saving again. A change applies at once in the server
process that saved it and within about 15 seconds in any other.

#### Repairing stored rate limit settings

If the newest stored version cannot be read, for example after a manual edit, the
service refuses to start and names the version. To repair it:

1. Back up the database: copy the SQLite file while the service is stopped, or take a
   PostgreSQL dump.
2. Look at the newest rows:
   `SELECT version, actor, created_at, value FROM rate_limit_settings ORDER BY version DESC LIMIT 5;`
3. Add a new version with the defaults. This keeps the history, removes every
   exemption and takes the limits from the environment or the defaults:

   ```sql
   INSERT INTO rate_limit_settings (version, value, actor)
   SELECT COALESCE(MAX(version), 0) + 1,
          '{"exempt": [], "format": 1, "maxReq": null, "windowMs": null}',
          'manual repair'
   FROM rate_limit_settings;
   ```
4. Start the service and set the values again on the Settings page.

Do not delete rows: a running process only moves to a higher version.

The General settings (table `app_settings`) are repaired the same way. This version
uses the environment values or the defaults for all four fields:

```sql
INSERT INTO app_settings (version, value, actor)
SELECT COALESCE(MAX(version), 0) + 1,
       '{"accessLogRetentionDays": null, "activityLogRetentionDays": null, "cookieSecure": null, "format": 1, "timeZone": null}',
       'manual repair'
FROM app_settings;
```

A saved Redis URL (table `redis_settings`) that cannot be read does not need a manual
repair: set `REDIS_URL` in the environment, which overrides it, start the service, and
save the URL again on the Settings page.

### Redis connection

Admin sessions and the first-admin setup live in Redis. The **Redis connection** panel of the
Settings page shows the URL this server process uses, with the password replaced by `****`,
and takes a new URL. The page never shows a password, and the field is write-only.

The service uses the first of these that is set:

1. `REDIS_URL` in the environment. The field is then locked.
2. The URL saved on the Settings page.
3. `REDIS_DEFAULT_URL`, which `docker-compose.yml` sets to the bundled Redis.

Before it saves a URL, the server connects with it and runs, on test keys under each key prefix
the service uses, the commands the service sends there: session records, sign-in and MFA
attempt counters, and used MFA codes. If that check fails, nothing is saved, and the page
says whether the server was unreachable, refused the user name or password, or did not allow
the writes. The saved URL is encrypted with the key that `SESSION_SECRET` and
`APP_ENCRYPTION_KEY` derive.

A saved URL takes effect only when the service restarts. Then every admin signs in again,
and a first-admin setup in progress starts over. Each server process reads the URL once, when
it starts, so restart every instance of the service together. Nothing in `.env` changed, so a
restart is enough:

```sh
docker compose restart auth-service
```

With several replicas, stop them all, then start them. Otherwise processes that started at
different times use different Redis servers, and sign-in fails some of the time.

When `REDIS_URL` is set and differs from the saved URL, the service saves it on the Settings
page once a check against it passes. Until then, the page says: "The environment value is not
saved yet; keep REDIS_URL set until this note goes away." The service tries again every 15
seconds. Remove `REDIS_URL` only after that note is gone.

#### Use an external Redis

Do one of the following:

- **On the Settings page:** while the bundled Redis still runs, save the external URL on the
  Settings page and restart the service. Then remove the bundled Redis with a
  `docker-compose.override.yml` next to `docker-compose.yml` (Docker Compose 2.24 or later),
  and apply it with `docker compose up -d --remove-orphans`:

  ```yaml
  services:
    redis: !reset null
    auth-service:
      depends_on: !reset {}
  ```
- **In the environment:** set `REDIS_URL` in the override file. The field on the Settings page
  is then locked:

  ```yaml
  services:
    redis: !reset null
    auth-service:
      environment:
        REDIS_URL: ${EXTERNAL_REDIS_URL:?EXTERNAL_REDIS_URL is not set}
      depends_on: !reset {}
  ```

In both cases, keep the generated `REDIS_PASSWORD` in `.env`. Compose checks
`docker-compose.yml` before it applies an override, so the variable is still required.

If the external Redis uses access control lists, the app's user needs these commands on keys
starting with `auth:`: `+get +setex +del +ping +set +incrby +ttl +expire +multi +exec ~auth:*`.
If the URL selects a database other than 0, also allow `+select`. Without them, sign-in can
fail with `500`; the limit is never skipped. The check before a save runs these commands, so
the page refuses a URL whose user lacks one of them.

### Passkeys

An admin can sign in with a passkey instead of the password and the authenticator
code, and can use it in place of the code to copy a token or an API key. The password
and the authenticator code keep working: they are the way back when a passkey is
lost.

- **HTTPS is required.** Browsers offer passkeys only on `https://` pages and on
  `http://localhost`. Over `http://<ip>:8080` the dashboard says so and offers the
  password and code only. See [Remote access and HTTPS](#remote-access-and-https).
- **The passkey address.** Every passkey works at one address: the host name of
  the address admins open the service at, such as `https://auth.example.com`. The
  first passkey an admin adds sets it to the address of that page; the Settings
  page shows it and can change it. `PASSKEY_ORIGIN` sets it from the environment
  and locks the field. The address must be a host name (not an IP address), over
  HTTPS or on `localhost`, without a path.
- **Changing the address.** Passkeys made for another host name stop working until
  the address is set back: sign in with the password and code, and add new
  passkeys on the Account page. Changing only the port keeps the host name, so
  passkeys keep working. Removing the address turns passkey sign-in off.
- **Adding a passkey** (Account page) needs the authenticator code, or one of your
  passkeys. Deleting one needs neither; also remove it from your device or password
  manager.
- The Activity Log records passkeys added, renamed and deleted, failed passkey
  checks, sign-ins with the passkey used, and whether a copy was confirmed with a
  passkey or a code.

### Secrets

- Generate every secret with `tools/generate-env.sh` or `tools/generate-env.ps1`, or with `openssl rand -hex 32`. The service rejects every value that was ever published in this repository, in any of these settings.
- **External PostgreSQL or Redis:** take the password from your secret store and put it in `.env` (`DATABASE_URL`, `PGPASSWORD`, or `EXTERNAL_REDIS_URL` above), let your orchestrator inject it, or, for Redis, save the URL on the Settings page.
- The service does not read PostgreSQL password files (`.pgpass`, `PGPASSFILE` or a `passfile` URL parameter), so a published password stored in one is not detected.

### Changing secrets

After you change `.env` or an override file, apply the change with `docker compose up -d`. It
creates the containers whose settings changed again. `docker compose restart` keeps the
environment the containers were created with, so a changed or removed variable has no effect.

| Secret | What changing it affects |
|---|---|
| `ADMIN_API_KEY` | Update every integration that uses the master key. Nothing stored is affected. To remove the master key, remove the variable and run `docker compose up -d`. |
| `REDIS_PASSWORD` of the bundled Redis | Once a Redis URL is saved on the Settings page, it wins over `REDIS_DEFAULT_URL`, so changing `REDIS_PASSWORD` alone breaks the connection. Set the new `REDIS_PASSWORD` and also `REDIS_URL=redis://:<new password>@redis:6379` in `.env`, then run `docker compose up -d`, which creates Redis and the service again with the new values. The service saves the new URL on the Settings page once its check passes. When the page no longer says that the environment value is not saved yet, you can remove `REDIS_URL`. Admins sign in again. |
| `SESSION_SECRET`, `APP_ENCRYPTION_KEY` | Stored MFA secrets and revealable token and API key values become unreadable: admins can no longer pass MFA, and existing tokens and API keys can no longer be revealed. Token validation and API key authentication keep working. There is no conversion tool, so change these only on a new installation. The saved Redis URL also becomes unreadable, and the service refuses to start until `REDIS_URL` is set in the environment. Set it, start the service, and save the URL on the Settings page again. `REDIS_URL` does not make the MFA secrets readable again. Changing `SESSION_SECRET` also signs every admin out and cancels an unfinished first-admin setup. |

### Upgrade from 2.1.x

- **Compose file:** an existing `docker-compose.yml` still sets `TZ`, `ALLOWED_AREAS` and
  `REDIS_URL`, requires `ADMIN_API_KEY`, and sets `COOKIE_SECURE` to `false` when it is not
  set. While it sets `TZ` and `COOKIE_SECURE`, those fields stay locked. Take the new
  `docker-compose.yml`, or make these changes to your copy:
  - Remove the `TZ: ${TZ:-UTC}` and `ALLOWED_AREAS: ...` lines.
  - Replace the `COOKIE_SECURE: ${COOKIE_SECURE-false}` line with
    `COOKIE_SECURE: ${COOKIE_SECURE-}`. An empty `COOKIE_SECURE` counts as not set.
  - Replace the `REDIS_URL: ...` line with
    `REDIS_DEFAULT_URL: redis://:${REDIS_PASSWORD:?REDIS_PASSWORD is not set. Run tools/generate-env.sh or tools/generate-env.ps1}@redis:6379`.
  - Replace the `ADMIN_API_KEY: ...` line with `ADMIN_API_KEY: ${ADMIN_API_KEY:-}`.
- **Values from the environment:** on the first start, `TZ`, `COOKIE_SECURE` and the two
  retention values set in the environment are saved on the Settings page, and `REDIS_URL` is
  saved once its check passes.
- **Zones:** on the first start, an empty zones table gets the zones that `ALLOWED_AREAS` gave
  (`orders,billing,webhooks` when it was not set), once. Entries that differ only in case
  become one zone, with the first spelling. The Zones page then shows them, and
  tokens can be granted only zones from the zones table. `ALLOWED_AREAS` is deprecated.
- **Retention values:** an `ACTIVITY_LOG_RETENTION_DAYS` or `ACCESS_LOG_RETENTION_DAYS` below 1
  or above 3650 now stops the start. Before, `0` or a negative value deleted every log entry.
- **`ENVIRONMENT`:** removed. The service never read it.

### Roll back to 2.1.x

Version 2.1.x does not read the values saved on the General and Redis connection panels.

1. Put the values you set on the Settings page into `.env`: `ACTIVITY_LOG_RETENTION_DAYS` and
   `ACCESS_LOG_RETENTION_DAYS` above all, and also `TZ` and `COOKIE_SECURE`. Without the
   retention values, 2.1.x uses 30 days and deletes older log entries within a minute.
2. Use the `docker-compose.yml` of 2.1.x. It sets `REDIS_URL`, which 2.1.x does not start
   without, and passes `COOKIE_SECURE` as `false` when it is not set. This version's compose
   file passes an unset `COOKIE_SECURE` empty, and 2.1.x refuses an empty value. With your
   own compose file, set `REDIS_URL` and `COOKIE_SECURE` in `.env`.
3. Set `ADMIN_API_KEY` in `.env`, because 2.1.x requires it.

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Client /   │     │              │     │   Redis      │
│   n8n Node   │────▶│ Auth Service │────▶│  (admin      │
│              │     │  (FastAPI)   │     │   sessions)  │
└──────────────┘     └──────┬───────┘     └──────────────┘
                            │
                     ┌──────▼───────┐
                     │  PostgreSQL  │
                     │  or SQLite   │
                     └──────────────┘
```

## Dashboard

Every dashboard section is a page with its own URL, so it can be bookmarked, shared, and opened directly:

- **Tokens** (`/admin/tokens`) — Create, edit, reveal (MFA-protected), and delete tokens
- **Zones** (`/admin/zones`) — Create and delete authorization zones
- **API Keys** (`/admin/api-keys`) — Create scoped API keys for external integrations, edit permissions, copy keys (MFA-protected). A master key set by `ADMIN_API_KEY` shows as a locked row
- **Activity Log** (`/admin/logs/activity`) — Audit trail of admin actions
- **Access Logs** (`/admin/logs/access`) — History of token validation requests, and the requests the rate limit rejected. `?token=<id>` lists one token's requests
- **Usage** (`/admin/usage`) — Calls per token over the last 24 hours, 7 days or 30 days: allowed and denied calls per hour or day, the zones each token calls with their levels, and the addresses the calls come from. Figures come from the access log, so they cover at most the access log retention and leave out requests the log does not record (rate-limited requests and requests with an invalid level)
- **Settings** (`/admin/settings`) — The time zone, log retention and the Secure session cookie, the rate limit and the addresses exempt from it, the Redis connection, and the passkey address
- **Account** (`/admin/account`) — Your passkeys: add, rename and delete them

Opening a page while signed out redirects to the login flow and returns to the same page after signing in.

## API Documentation

See [API.md](API.md) for the full REST API reference.

## Security

- Admin passwords are hashed with **bcrypt**, which uses at most 72 bytes of a password, so new admin passwords are limited to 72 bytes
- Dashboard access requires **TOTP MFA** (two-factor authentication)
- A **passkey** replaces the password and the authenticator code at sign-in. Passkeys are created and used with user verification required (the device checks the fingerprint, face, PIN or screen lock), each sign-in or confirmation request can be used once and for five minutes, and adding a passkey needs the authenticator code or another passkey
- Each authenticator code is accepted once, and an admin gets 5 tries in 15 minutes, shared by sign-in, token reveal and API key copy; after that the service answers `429` with `Retry-After`
- Tokens and API keys are encrypted at rest with **AES-256-GCM**
- Token hashes use **SHA-256** for lookups
- The session cookie holds only a random session ID. Admin sessions and unfinished MFA setups are kept in Redis under names derived from `SESSION_SECRET`, encrypted and authenticated with a key derived from it, and expire after 12 hours (setup: 15 minutes). Another installation or another Redis client without that secret cannot create, read, change, or move them between sessions
- CSRF protection on all state-changing admin operations
- Rate limiting on the `/validate` endpoint
- Dashboard sign-in is limited to 10 attempts per client address in 15 minutes, and
  runs its password checks off the request loop, so sign-in attempts cannot slow
  down `/validate`

## License

MIT
