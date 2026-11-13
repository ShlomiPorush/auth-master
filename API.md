# Auth Service — API Reference

Base URL: `http://<host>:8080`

---

## Authentication

### API Key (Bearer Token)

All `/tokens/*` endpoints require a valid API key in the `Authorization` header:

```
Authorization: Bearer <api-key>
```

API keys can be:
- The master `ADMIN_API_KEY` environment variable (full access)
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

---

### Validate Token

Validates whether a token has a specific permission level for a given zone. This endpoint is **public** — no API key required.

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

#### GET

```
GET /validate?token=tok_abc123...&area=orders&level=read
```

**Response:**
```json
{
  "result": true
}
```

**Permission Levels:** `read`, `write`, `delete`, `all`

> Rate-limited per IP address. Returns `{"result": false}` when rate limit is exceeded.

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

> Shares the per-IP rate limit with `/validate`: lookups and validations draw from the same budget. When the limit is exceeded, the response is `429` with `{"detail": "Too many requests"}`. The limit is kept per server process.

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

**Zone name rules:** Alphanumeric, dots, underscores, colons, hyphens. Max 63 characters.

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
| `grants` | Yes | Array of `{area, level}` permissions |
| `expiresAt` | No | ISO 8601 expiration date (null = never expires) |
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
| `429` | Too many requests from one IP address. `/validate` never returns this code: over the limit it answers `200` with `{"result": false}`. |

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
