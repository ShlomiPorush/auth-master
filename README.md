# Auth Service

A self-hosted token authentication and authorization service. Manage API tokens, define permission zones, and validate access — all through a modern web dashboard or REST API.

## Features

- **Token Management** — Create, edit, revoke, and reveal API tokens with granular per-zone permissions
- **Zone-Based Authorization** — Define zones (areas) and assign `read` / `write` / `delete` / `all` access levels
- **API Key Management** — Create scoped API keys for programmatic access (e.g. n8n, CI/CD)
- **MFA-Protected** — Admin dashboard requires TOTP two-factor authentication
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

   The script writes `ADMIN_API_KEY`, `SESSION_SECRET`, `APP_ENCRYPTION_KEY` and `REDIS_PASSWORD`, makes the file readable by you only, and never replaces an existing `.env`. Keep the file private and out of version control. Optional settings are listed in [.env.example](.env.example).
2. Start the containers: `docker compose up -d`
3. Open `http://localhost:8080` — you'll be redirected to the setup wizard
4. Create your admin account and set up MFA (TOTP)
5. Log in and start managing tokens and zones

The service refuses to start while a required secret is missing, empty, or has a value that was ever published in this repository, and its log names each setting and how to fix it.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///data/auth.db` | Database connection string. Use `postgres://…` for PostgreSQL. A password in it must not be a published value, and a URL with more than one `@` before the host is refused (write `@` as `%40`) |
| `REDIS_URL` | _(required)_ | Redis connection string with a password (required for admin sessions). The bundled compose file builds it from `REDIS_PASSWORD` |
| `REDIS_PASSWORD` | _(required with the bundled Redis)_ | Password of the bundled Redis server |
| `ADMIN_API_KEY` | _(required)_ | Master API key with full access to all scopes |
| `SESSION_SECRET` | _(required)_ | Together with `APP_ENCRYPTION_KEY`, derives the key that encrypts stored MFA secrets and revealable token and API key values |
| `APP_ENCRYPTION_KEY` | _(required)_ | See `SESSION_SECRET` |
| `PGPASSWORD` | _(empty)_ | Optional PostgreSQL password when `DATABASE_URL` has none; checked like the others |
| `ALLOWED_AREAS` | `orders,billing,webhooks` | Fallback zone list when the zones table is empty |
| `BOOTSTRAP_TOKEN` | _(empty)_ | Optional token for bootstrapping the first admin account via API |
| `TOTP_ISSUER` | `AuthService` | Issuer name shown in authenticator apps |
| `ENVIRONMENT` | `production` | Application environment (`production` or `development`) |
| `ROOT_PATH` | _(empty)_ | URL path prefix for sub-directory deployments (e.g. `/auth`) |
| `COOKIE_SECURE` | `false` | Set to `true` when running behind HTTPS |
| `TZ` | `UTC` | Timezone for dates in the UI (e.g. `Asia/Jerusalem`, `America/New_York`) |
| `RATE_LIMIT_MAX_REQ` | `120` | Max validation requests allowed per IP. Also editable on the Settings page; when this variable is set, the page shows the field locked. See [Rate limit settings](#rate-limit-settings) |
| `RATE_LIMIT_WINDOW_MS` | `60000` | Time window for rate limiting in milliseconds (default 1 minute). Also editable on the Settings page, like `RATE_LIMIT_MAX_REQ` |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Read by uvicorn, not by the app: the addresses of reverse proxies whose `X-Forwarded-For` header is trusted. See [Behind a reverse proxy](#behind-a-reverse-proxy) |

### Behind a reverse proxy

The rate limit counts requests per client address, as uvicorn reports it. Behind a
reverse proxy such as Traefik or nginx in another container, every request arrives
from the proxy's address, so all clients share one limit, and the access log shows
the proxy's address for every request.

To count real client addresses, set `FORWARDED_ALLOW_IPS` to the proxy's address,
for example `FORWARDED_ALLOW_IPS=172.18.0.50`. uvicorn then takes the client address
from the `X-Forwarded-For` header, but only on connections from that address. Give
the proxy a fixed address in its compose file: if its address changes, the header
is ignored again and all clients share one limit.

Trust only the proxy. Do not use a whole Docker network such as `172.18.0.0/16` or
`*`: any other container on that network could then send a false
`X-Forwarded-For` header to choose the address it is counted under.

Requests over the limit are answered `429 Too many requests` with a `Retry-After`
header (the whole seconds until the caller's window ends), and are not written to the
access log. The Access Logs page
shows how many there were and from which addresses, and each server process writes
a `Rate limit:` line to its log at most once per window.

### Rate limit settings

The Settings page (`/admin/settings`) sets the rate limit and the addresses it never
applies to:

- **Requests per window** and **window length** (whole seconds). When
  `RATE_LIMIT_MAX_REQ` or `RATE_LIMIT_WINDOW_MS` is set in the environment or `.env`,
  that value wins and the field is locked on the page. Remove the variable to manage
  the value on the page.
- **Exempt addresses**: up to 100 single IPv4 or IPv6 addresses, one per line.
  Requests from them are never limited or counted. They are still validated and
  logged. An exempt address is not limited at all: behind a reverse proxy without
  `FORWARDED_ALLOW_IPS`, every caller arrives from the proxy's address, and exempting
  it removes the limit for everyone. The page asks for a confirmation before it
  exempts the address your own request came from.

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
### Secrets

- Generate every secret with `tools/generate-env.sh` or `tools/generate-env.ps1`, or with `openssl rand -hex 32`. The service rejects every value that was ever published in this repository, in any of these settings.
- **External PostgreSQL or Redis:** take the password from your secret store and put it in `.env` (`DATABASE_URL`, `PGPASSWORD`, or `EXTERNAL_REDIS_URL` below), or let your orchestrator inject it. For an external Redis, keep the generated `REDIS_PASSWORD` in `.env` (Compose checks `docker-compose.yml` before it applies an override, so it is still required) and add a `docker-compose.override.yml` next to `docker-compose.yml` (Docker Compose 2.24 or later):

  ```yaml
  services:
    redis: !reset null
    auth-service:
      environment:
        REDIS_URL: ${EXTERNAL_REDIS_URL:?EXTERNAL_REDIS_URL is not set}
      depends_on: !reset {}
  ```
- The service does not read PostgreSQL password files (`.pgpass`, `PGPASSFILE` or a `passfile` URL parameter), so a published password stored in one is not detected.

### Changing secrets

| Secret | What changing it affects |
|---|---|
| `ADMIN_API_KEY` | Update every integration that uses the master key. Nothing stored is affected. |
| `REDIS_PASSWORD` / Redis password | Restart Redis and the service together. Admins may have to sign in again. |
| `SESSION_SECRET`, `APP_ENCRYPTION_KEY` | Stored MFA secrets and revealable token and API key values become unreadable: admins can no longer pass MFA, and existing tokens and API keys can no longer be revealed. Token validation and API key authentication keep working. There is no conversion tool, so change these only on a new installation. |

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
- **API Keys** (`/admin/api-keys`) — Create scoped API keys for external integrations, edit permissions, copy keys (MFA-protected)
- **Activity Log** (`/admin/logs/activity`) — Audit trail of admin actions
- **Access Logs** (`/admin/logs/access`) — History of token validation requests, and the requests the rate limit rejected
- **Settings** (`/admin/settings`) — The rate limit and the addresses exempt from it

Opening a page while signed out redirects to the login flow and returns to the same page after signing in.

## API Documentation

See [API.md](API.md) for the full REST API reference.

## Security

- Admin passwords are hashed with **bcrypt**, which uses at most 72 bytes of a password, so new admin passwords are limited to 72 bytes
- Dashboard access requires **TOTP MFA** (two-factor authentication)
- Tokens and API keys are encrypted at rest with **AES-256-GCM**
- Token hashes use **SHA-256** for lookups
- Session cookies are signed and stored in Redis with configurable TTL
- CSRF protection on all state-changing admin operations
- Rate limiting on the `/validate` endpoint

## License

MIT
