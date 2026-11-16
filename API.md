# Auth Service — API Reference

Base URL: the `https://` address of the reverse proxy in front of the service. Plain `http://<host>:8080` sends API keys and tokens unencrypted: use it only on the Docker host itself or on a network you trust. See "Remote access and HTTPS" in the README.

---

## Authentication

### API Key (Bearer Token)

All `/tokens/*` endpoints require a valid API key in the `Authorization` header:

```
Authorization: Bearer <api-key>
```

API keys can be:
- The master `ADMIN_API_KEY` environment variable (full access). It is optional: when it is not set, there is no master key, and every key is a key from the dashboard that can be revoked there
- A scoped key created from the admin dashboard

### Scopes

| Scope | Description |
|---|---|
| `validate` | Not required by any endpoint. `/validate` is public and needs no API key. |
| `tokens:read` | List tokens and zones |
| `tokens:write` | Create, update, delete tokens |
| `zones:read` | List zones |
| `zones:write` | Create zones |

---

## Public Endpoints

### Health Check

```
GET /health
```

**Response:**
```json
{
  "ok": true,
  "db": true,
  "redis": true
}
```

The status is `200` when the database and Redis both respond. When either does not respond within 2 seconds, the status is `503` and the matching field is `false`.

Before first-time setup has chosen a database, the status is `200` (while Redis responds) and the
body is `{"ok": false, "status": "setup_required", "db": false, "redis": true}`. When the saved
database cannot be used, the service is in recovery mode: the status is `503` and the body is
`{"ok": false, "status": "database_unavailable", "code": "<reason>", "db": false, "redis": true}`.
Every other endpoint then answers `503` with `{"detail": "...", "code": "<reason>"}`, where
`code` is `setup_required` or the recovery reason, such as `auth_failed` or `unreachable`. See
"Database connection" in README.md.

While an admin switches the database connection, requests wait up to 10 seconds and then
continue. A request that would wait longer gets `503` with `"code": "database_switching"` and a
`Retry-After` header.

---

### Validate Token

Validates whether a token has a specific permission level for a given zone. This endpoint is **public** — no API key required.

`area` is compared with the token's grants regardless of case and surrounding spaces: `Orders` and ` orders ` match a grant on `orders`.

Use `POST`: it sends the token in the request body.

#### POST

```
POST /validate
Content-Type: application/json
```

**Body:**
```json
{
  "token": "tok_abc123...",
  "area": "orders",
  "level": "read"
}
```

#### GET (not recommended)

```
GET /validate?token=tok_abc123...&area=orders&level=read
```

The same check, with the token in the URL. Reverse proxies, load balancers and HTTP clients often record URLs in their own logs, so the token can end up there. The service hides the query string of `/validate` in its own request log (the container output) at the default log level. uvicorn's `trace` log level still prints it.

**Response:**
```json
{
  "result": true
}
```

**Permission Levels:** `read`, `write`, `delete`, `all`

`area` takes at most 255 characters, spaces included (characters, not bytes). A longer `area`, or one that is not valid Unicode text (such as a lone `\ud800` escape in a JSON body), gets the standard `422` validation error and is not written to the access log.

> Rate-limited per client address, as the server sees it, and per server process. All IPv6 addresses in one /64 network share one limit. Over the limit it answers `429` with `{"detail": "Too many requests"}` and a `Retry-After` header: the whole seconds until the caller's window ends. These requests are not written to the access log; the Access Logs page of the dashboard shows how many there were. Addresses and networks listed on the dashboard's Settings page are exempt and never limited. Behind a reverse proxy, see "Behind a reverse proxy" in the README.

---

### Token Grants

Returns the grants of a token to whoever holds it. This endpoint is **public**: the token itself is the credential, and no API key is required.

```
POST /validate/grants
Content-Type: application/json
```

**Body:**
```json
{
  "token": "tok_abc123..."
}
```

**Response** for an active token:
```json
{
  "active": true,
  "grants": [
    { "area": "orders", "level": "read" },
    { "area": "webhooks", "level": "read,write,delete" }
  ],
  "expiresAt": "2025-12-31T23:59:59+00:00"
}
```

`level` lists the individual permission flags (`read`, `write`, `delete`). `expiresAt` is `null` when the token never expires.

**Response** for an unknown, disabled or expired token (the same in every case):
```json
{
  "active": false
}
```

A token that is not valid UTF-8 text gets `422` with `{"detail": "Token must be valid UTF-8 text"}`.

The answer is read from the database on every call, so a disabled, deleted or rotated token stops reporting grants immediately. Each call is recorded in the access log with area `*` and level `grants`. It does not update the token's last-used time.

> Shares the per-address rate limit (per /64 network for IPv6) with `/validate`: lookups and validations draw from the same budget. When the limit is exceeded, the response is `429` with `{"detail": "Too many requests"}` and a `Retry-After` header. The limit is kept per server process.

---

## Token Management

All endpoints under `/tokens` require an API key with the appropriate scope.

---

### Ping (API Key Test)

```
GET /tokens/ping
Authorization: Bearer <api-key>
```

Validates the API key without requiring any specific scope. Used for connectivity testing (e.g. n8n credential test).

**Response:**
```json
{ "ok": true }
```

---

### My Scopes

```
GET /tokens/me
Authorization: Bearer <api-key>
```

Returns the scopes of the API key that makes the call. No specific scope is required. The master `ADMIN_API_KEY` returns every scope.

**Response:**
```json
{ "scopes": ["tokens:read", "validate", "zones:read"] }
```

---

### List Zones

```
GET /tokens/zones
Authorization: Bearer <api-key>
```

**Required scope:** `zones:read`

**Response:**
```json
[
  { "name": "orders" },
  { "name": "billing" },
  { "name": "webhooks" }
]
```

---

### Create Zone

```
POST /tokens/zones
Authorization: Bearer <api-key>
Content-Type: application/json
```

**Required scope:** `zones:write`

**Body:**
```json
{
  "name": "payments",
  "description": "Payment processing area"
}
```

**Response** `201`:
```json
{
  "id": "uuid",
  "name": "payments",
  "description": "Payment processing area"
}
```

**Zone name rules:** Alphanumeric, dots, underscores, colons, hyphens. Max 63 characters. Names are unique regardless of case: creating `BILLING` when `billing` exists answers `409`.

---

### List Tokens

```
GET /tokens
Authorization: Bearer <api-key>
```

**Required scope:** `tokens:read`

**Response:**
```json
[
  {
    "id": "uuid",
    "name": "My Token",
    "grants": [
      { "area": "orders", "level": "read" },
      { "area": "billing", "level": "write" }
    ],
    "expiresAt": "2025-12-31T23:59:59+00:00",
    "isActive": true,
    "createdAt": "2025-01-15T10:30:00",
    "lastUsedAt": "2025-04-20T14:22:00"
  }
]
```

---

### Create Token

```
POST /tokens
Authorization: Bearer <api-key>
Content-Type: application/json
```

**Required scope:** `tokens:write`

**Body:**
```json
{
  "name": "Webhook Reader",
  "grants": [
    { "area": "orders", "level": "read" },
    { "area": "webhooks", "level": "all" }
  ],
  "expiresAt": "2025-12-31T23:59:59Z",
  "token": "my-custom-token-value"
}
```

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Label for the token |
| `grants` | Yes | Array of `{area, level}` permissions. An area matches its zone regardless of case and is stored with the zone's name; naming one zone twice in different cases answers `400` |
| `expiresAt` | No | ISO 8601 expiration date (null = never expires). A value without an offset, such as `2025-12-31T23:59:59` or `2025-12-31`, is read as UTC |
| `token` | No | Custom token value (auto-generated if omitted) |

**Response** `201`:
```json
{
  "id": "uuid",
  "token": "tok_abc123...",
  "name": "Webhook Reader",
  "grants": [
    { "area": "orders", "level": "read" },
    { "area": "webhooks", "level": "all" }
  ],
  "expiresAt": "2025-12-31T23:59:59+00:00"
}
```

> ⚠️ The `token` value is returned **only once** upon creation. Store it securely.

---

### Update Token

```
PATCH /tokens/{token_id}
Authorization: Bearer <api-key>
Content-Type: application/json
```

**Required scope:** `tokens:write`

**Body** (all fields optional):
```json
{
  "name": "Updated Name",
  "grants": [
    { "area": "orders", "level": "write" }
  ],
  "isActive": false,
  "expiresAt": "2026-06-01T00:00:00Z",
  "token": "new-custom-token-value"
}
```

`expiresAt` takes the same form as when creating a token.

**Response:**
```json
{ "ok": true }
```

---

### Delete Token

```
DELETE /tokens/{token_id}
Authorization: Bearer <api-key>
```

**Required scope:** `tokens:write`

**Response:**
```json
{ "ok": true }
```

---

## Dashboard endpoints

The admin pages call endpoints under `/admin/api/`. They need a signed-in admin session and,
for every change, the `X-CSRF-Token` header. They are not meant for integrations, but two of
them carry values that other tools may read:

- `GET /admin/api/me` returns, besides the user, `timeZone`, `dateFormat` and `timeFormat`:
  the time zone and the formats every date on the pages is written in. `dateFormat` is one
  of `browser`, `DD/MM/YYYY`, `MM/DD/YYYY`, `DD.MM.YYYY`, `YYYY-MM-DD`; `timeFormat` is one of
  `24h`, `12h`, `browser`. `browser` leaves the order of the date, or the clock, to the
  browser's language.
- `GET /admin/api/settings/display` and `PUT /admin/api/settings/display` read and save those
  two formats. The body of a save is `{"baseVersion": <version read>, "dateFormat": ...,
  "timeFormat": ...}` with only the fields being changed. A save from an older version, or
  one another admin made first, answers `409` with `"code": "conflict"` and the current
  values in `current`. A field that `DATE_FORMAT` or `TIME_FORMAT` sets answers `409` with
  `"code": "locked"`. A write the database refuses answers `503` with `"code": "busy"` or
  `"code": "write_refused"`, and nothing is saved.
- `GET /admin/api/settings/database` reports in `job` a database operation that runs
  (`{"state": "running", ...}`), or `{"state": "pending"}` while a connection change has been
  received but has not started its operation yet. A second change meanwhile answers `409`
  with `"code": "busy"`. For a SQLite database chosen in setup, `movable` is `true`.
- `POST /admin/api/settings/database/move/check` with `{"postgres": {...}}` (the connection
  fields of a change) checks a move to that PostgreSQL database and changes nothing. It answers
  `{"ok": true, "target": "empty" | "earlier_copy", "problems": [...], "counts": {...}}`, or
  `{"ok": false, "code": ..., "detail": ...}`.
- `POST /admin/api/settings/database/move` with the same body and `code` or `passkey` starts the
  move and answers `202` with its `job`, followed at `GET /admin/api/settings/database/jobs/{id}`.
  A finished move's `result` holds `rows` per table, `textReplaced` (log rows whose NUL or
  invalid text was replaced) and `pauseMs`.

---

## Error Responses

All error responses follow a consistent format:

```json
{
  "detail": "Error description"
}
```

| Status Code | Description |
|---|---|
| `400` | Bad request (invalid input, missing fields) |
| `401` | Unauthorized (missing or invalid API key) |
| `403` | Forbidden (missing required scope) |
| `404` | Resource not found |
| `409` | Conflict (duplicate token or zone) |
| `413` | The request body is larger than 64 KiB. See "Request size" below. |
| `429` | Too many requests from one client address (one /64 network for IPv6) on `/validate` or `/validate/grants`, or more than 10 dashboard sign-in attempts (`/admin/api/login`) from one address (one /64 for IPv6) in 15 minutes. The `Retry-After` header gives the whole seconds until the caller's window ends. |
| `503` | Dashboard sign-in or first-run registration waited more than 10 seconds for a password check. Retry after the seconds in `Retry-After`. |

### Request size

Request bodies are limited to 64 KiB (65,536 bytes). A larger body is not processed: the answer is `413 Content Too Large` and the request is not carried out. The answer is plain text `Content Too Large` when the `Content-Length` header gives the size, or `{"detail": "Content Too Large"}` when the body is sent without one (chunked) and passes the limit while it arrives.

Send no body on requests that take none (`GET`, `DELETE`). The service does not read such a body. If its `Content-Length` is over 64 KiB, the request is still handled, but its answer, whatever it was (success, `401`, `403`, `404`), is replaced by `413`. A `413` on such a request therefore does not tell whether it succeeded: a `DELETE` may already have deleted the item. Repeat the request without a body to learn the outcome.

---

## Examples

### cURL — Validate a Token

```bash
curl -X POST http://localhost:8080/validate \
  -H "Content-Type: application/json" \
  -d '{"token": "tok_abc123", "area": "orders", "level": "read"}'
```

### cURL — Create a Zone

```bash
curl -X POST http://localhost:8080/tokens/zones \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"name": "payments", "description": "Payment processing"}'
```

### cURL — Create a Token

```bash
curl -X POST http://localhost:8080/tokens \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Payment Service",
    "grants": [{"area": "payments", "level": "write"}]
  }'
```

### cURL — List All Tokens

```bash
curl http://localhost:8080/tokens \
  -H "Authorization: Bearer your-api-key"
```
