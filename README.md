# Auth Service

A self-hosted token authentication and authorization service. Manage API tokens, define permission zones, and validate access — all through a modern web dashboard or REST API.

## Features

- **Token Management** — Create, edit, revoke, and reveal API tokens with granular per-zone permissions
- **Zone-Based Authorization** — Define zones (areas) and assign `read` / `write` / `delete` / `all` access levels
- **API Key Management** — Create scoped API keys for programmatic access (e.g. n8n, CI/CD)
- **MFA-Protected** — Admin dashboard requires TOTP two-factor authentication
- **Fast Validation** — Redis-cached token validation with rate limiting
- **Dual Database Support** — PostgreSQL for production, SQLite for lightweight/dev setups
- **Modern Dashboard** — Dark-themed admin UI for managing tokens, zones, and API keys
- **n8n Integration** — Community nodes for workflow automation: action node + webhook trigger with built-in token validation ([n8n-nodes-auth-service](https://www.npmjs.com/package/n8n-nodes-auth-service))

## Quick Start

### Docker Compose

```yaml
services:
  redis:
    image: redis:7-alpine
    command: redis-server --requirepass ${REDIS_PASSWORD:-changeme}
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD:-changeme}", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  auth-service:
    image: auth-service:latest
    ports:
      - "8080:8080"
    environment:
      REDIS_URL: redis://:${REDIS_PASSWORD:-changeme}@redis:6379
      ADMIN_API_KEY: ${ADMIN_API_KEY:-change-me-in-production}
      SESSION_SECRET: ${SESSION_SECRET:-min-32-chars-secret-change-me!!}
      APP_ENCRYPTION_KEY: ${APP_ENCRYPTION_KEY:-0123456789abcdef0123456789abcdef}
    volumes:
      - ./data:/app/data
    depends_on:
      redis:
        condition: service_healthy
```

### First Run

1. Start the containers: `docker compose up -d`
2. Open `http://localhost:8080` — you'll be redirected to the setup wizard
3. Create your admin account and set up MFA (TOTP)
4. Log in and start managing tokens and zones

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///data/auth.db` | Database connection string. Use `postgres://…` for PostgreSQL |
| `REDIS_URL` | `redis://:changeme@127.0.0.1:6379` | Redis connection string (required for caching + sessions) |
| `ADMIN_API_KEY` | `change-me-in-production` | Master API key with full access to all scopes |
| `SESSION_SECRET` | _(dev default)_ | Secret for signing session cookies (min 32 chars) |
| `APP_ENCRYPTION_KEY` | _(dev default)_ | Key for encrypting stored tokens (32 hex chars) |
| `ALLOWED_AREAS` | `orders,billing,webhooks` | Fallback zone list when the zones table is empty |
| `BOOTSTRAP_TOKEN` | _(empty)_ | Optional token for bootstrapping the first admin account via API |
| `TOTP_ISSUER` | `AuthService` | Issuer name shown in authenticator apps |
| `ENVIRONMENT` | `production` | Application environment (`production` or `development`) |
| `VALIDATE_CACHE_TTL_SEC` | `300` | How long validated tokens are cached in Redis (seconds) |
| `COOKIE_SECURE` | `false` | Set to `true` when running behind HTTPS |

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Client /   │     │              │     │   Redis      │
│   n8n Node   │────▶│ Auth Service │────▶│  (cache +    │
│              │     │  (FastAPI)   │     │   sessions)  │
└──────────────┘     └──────┬───────┘     └──────────────┘
                            │
                     ┌──────▼───────┐
                     │  PostgreSQL  │
                     │  or SQLite   │
                     └──────────────┘
```

## Dashboard

The admin dashboard provides:

- **Tokens tab** — Create, edit, reveal (MFA-protected), and delete tokens
- **Zones tab** — Create and delete authorization zones
- **API Keys tab** — Create scoped API keys for external integrations, edit permissions, copy keys (MFA-protected)

## API Documentation

See [API.md](API.md) for the full REST API reference.

## Security

- Admin passwords are hashed with **bcrypt**
- Dashboard access requires **TOTP MFA** (two-factor authentication)
- Tokens and API keys are encrypted at rest with **AES-256-GCM**
- Token hashes use **SHA-256** for lookups
- Session cookies are signed and stored in Redis with configurable TTL
- CSRF protection on all state-changing admin operations
- Rate limiting on the `/validate` endpoint

## License

MIT
