# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- The service no longer has default values for its secrets, and it refuses to
  start when one is missing, empty, or set to a value that was ever published in
  this repository. Earlier versions fell back to these published values when a
  setting was not overridden, so the master API key, the key that encrypts stored
  MFA secrets and token values, and the Redis password were publicly known. Every
  value below is public. Replace it wherever it is still in use:
  - `ADMIN_API_KEY`: `change-me-in-production`, `generate-a-long-random-string`
  - `SESSION_SECRET`: `min-32-chars-secret-change-me-for-production-use`,
    `min-32-chars-secret-change-me!!`, `at-least-32-characters-long-secret-key`
  - `APP_ENCRYPTION_KEY`: `0123456789abcdef0123456789abcdef`
  - Redis password: `changeme`
  - PostgreSQL password in the example `DATABASE_URL`: `authpass`

  The same check rejects the placeholders `<password>`, `<api-key>` and
  `your-api-key` from the documentation. Passwords inside `REDIS_URL` and
  `DATABASE_URL`, and the `REDIS_PASSWORD` and `PGPASSWORD` variables, are
  checked too. Error messages name the setting and the fix and never show its
  value.

- Revoking, deleting, rotating or narrowing a token, and renaming or deleting a
  zone, now take effect for the very next validation request. Validation results
  were cached in Redis for up to `VALIDATE_CACHE_TTL_SEC` seconds (300 by default),
  and two paths could keep serving the old result until that time ran out: a
  validation request that read the token just before the change and stored it in
  the cache just after, and a cache delete that failed after the database change
  was saved. `/validate` now reads the token from the database on every request.
  This holds once no instance of an earlier version is serving requests, because an
  earlier version still reads and fills the cache. Rolling back to an earlier
  version can bring back results cached before the upgrade: wait at least
  `VALIDATE_CACHE_TTL_SEC` seconds after the upgrade, or delete the Redis keys that
  start with `validate:`, before an earlier version serves traffic again.

### Changed

- **Breaking:** `ADMIN_API_KEY`, `SESSION_SECRET`, `APP_ENCRYPTION_KEY` and
  `REDIS_URL` are required, and `REDIS_URL` must contain a password.
  `docker-compose.yml` requires `REDIS_PASSWORD`, `ADMIN_API_KEY`,
  `SESSION_SECRET` and `APP_ENCRYPTION_KEY` and stops with a message when one is
  missing. A PostgreSQL `DATABASE_URL` with more than one `@` before the host is
  refused; write `@` in the user name or password as `%40`.
- **Breaking for installations that ran on the published `SESSION_SECRET` or
  `APP_ENCRYPTION_KEY`:** these two values derive the key that encrypts stored MFA
  secrets and revealable token and API key values. After setting new ones, admins
  cannot pass MFA and existing values cannot be revealed. Token validation and
  API key authentication keep working. No conversion tool is provided.
- New `tools/generate-env.sh` (Linux, macOS) and `tools/generate-env.ps1`
  (Windows) create `.env` once with new random secrets, readable by its owner
  only. They never replace an existing file. `.env.example` no longer contains
  values.
- The admin dashboard and the sign-in and setup pages have a new design with
  light and dark themes (following the system by default), a sidebar that
  becomes an icon rail on narrow screens and a tab bar on phones, and dialogs
  that open as bottom sheets on phones. Pages update by themselves, so the
  Refresh button is gone. Filters are kept in the page address. Dates show in
  the server's time zone (`TZ`), in the date and time format chosen under
  Preferences.
- Enabling or disabling a token takes one click and no longer asks for
  confirmation. Deleting still asks.
- `GET /health` answers `503` with `{"ok": false, ...}` when the database or
  Redis does not respond, instead of failing with `500`. The Docker health check
  still fails during such an outage.
- The log endpoints list entries with the same timestamp in a fixed order
  (newest first, then by ID).

### Added

- A Needs attention bell on every dashboard page shows a database or Redis
  outage and the tokens that expire within 7 days.
- The admin log endpoints accept `before` and `since` cursors and return
  `nextCursor` and `headCursor`, so a list can be paged without gaps or repeats
  while new entries arrive. With `since`, `limit` can be up to 500.
- `GET /admin/api/me` returns the server's time zone as `timeZone`.
- A CI workflow runs the Python and JavaScript tests and builds the Docker image
  on every pull request and push to `main`.

- Token holders can read the grants of their token with `POST /validate/grants`.
  The call is public, returns the zones and permission levels together with the
  expiry time, and answers `{"active": false}` for an unknown, disabled or
  expired token. It shares the per-IP rate limit with `/validate`, answers `429`
  when the limit is exceeded, and appears in the access log.
- API key holders can read the scopes of their key with `GET /tokens/me`.
- Requests rejected by the `/validate` rate limit are now reported. They are
  still not written to the access log, and their answers do not change. Each
  server process writes a `Rate limit:` line to its log at most once per rate
  limit window, with the number of rejected requests and the addresses that
  caused most of them. The Access Logs page shows how many requests were
  rejected, when the last one was, and from which addresses, read from the new
  `GET /admin/api/rate-limit`. Counts are kept in memory per server process and
  reset on restart.
- A Settings page (`/admin/settings`) sets the `/validate` rate limit (requests per
  window and window length) and up to 100 exempt IP addresses, which are never
  limited or counted. `RATE_LIMIT_MAX_REQ` and `RATE_LIMIT_WINDOW_MS` still work and,
  when set, lock their field on the page. Every save is kept as a new version in the
  new table `rate_limit_settings` with who saved it, when and from where, and is also
  shown on the Activity Log. Saving from an outdated page is refused with the current
  values, and the page asks for a confirmation before exempting the address the
  admin's own request came from. The service refuses to start when the newest stored
  version is invalid; README.md explains the repair. Going back to an earlier version
  of the service ignores the table, so its limits and exemptions stop applying.
- In-memory SQLite (`DATABASE_URL=sqlite:///:memory:`) is refused at startup: the
  rate limit settings are read on a second connection to the same database file.
### Removed

- The `VALIDATE_CACHE_TTL_SEC` setting. It has no effect and can be removed from
  environment files; leaving it in place is harmless. Validation entries already
  in Redis are no longer read and expire on their own. Redis is still required
  for admin sessions.

### Fixed

- An API key name containing a quote no longer breaks the key's actions or
  inserts script into the page, and activity log details no longer insert unescaped text
  into the page.
- After signing in, the `next` address can no longer lead to another site.
- After an upgrade, browsers no longer keep using the previous stylesheet for up
  to a day. Stylesheets and scripts are now checked with the server on every
  page load.
- The access log page no longer fails on PostgreSQL deployments. The listing
  matched each log entry to its token by comparing the entry's token ID, stored as
  text, with the token's UUID, and PostgreSQL rejects a comparison between those
  two types, so every request for the page failed. The token ID is now compared as
  text. SQLite deployments were not affected.
- `/validate` no longer fails with `500` when the token or the zone is text that
  cannot be encoded as UTF-8, such as a lone `\ud800` escape in a JSON body. It
  now answers `{"result": false}`, the same answer as for any other rejected
  input, and does not write an access log entry for the request.
- A request that fails input validation now answers `422` when the rejected
  input contains text that cannot be encoded as UTF-8. Building the error
  response failed for such text, so the request answered `500`. Non-ASCII characters
  in `422` response bodies are now sent as JSON `\uXXXX` escapes; the parsed
  content is unchanged.
- Admin registration now refuses a password longer than 72 bytes with a clear
  message, both in the setup page and in the API (`400`). bcrypt uses only the
  first 72 bytes of a password. Earlier versions dropped the rest without saying
  so; with bcrypt 5 such a password would fail with `500`. Letters outside English,
  such as Hebrew, and emoji take 2 to 4 bytes each. Signing in is not limited:
  an existing password longer than 72 bytes keeps working, and as before only
  its first 72 bytes are checked.
- Admin registration answers `400` and sign-in answers `401` when the password
  is text that cannot be encoded as UTF-8, such as a lone `\ud800` escape in a
  JSON body. Both answered `500`.

## [1.2.0] - 2026-09-22

### Added

- Every dashboard section is now a page with its own URL: `/admin/tokens`,
  `/admin/zones`, `/admin/api-keys`, `/admin/logs/activity` and
  `/admin/logs/access`. Pages can be bookmarked, shared and opened in a new tab;
  refreshing stays on the same page and the browser back and forward buttons move
  between pages. Opening a page while signed out goes through the login flow and
  returns to that page afterwards. `/` and the legacy `/admin` paths redirect to
  `/admin/tokens`.

### Fixed

- Searching the activity and access logs no longer fails on SQLite deployments.
  The SQLite driver rewrote every `$N` placeholder to a positional `?` without
  duplicating the bound value, so any query that reuses a placeholder, as both
  log search filters do, was rejected by SQLite. PostgreSQL deployments were not
  affected.

## [1.1.5] - 2026-09-10

### Fixed

- A URL prefix containing a quote character no longer breaks the dashboard. The
  `ROOT_PATH` value is embedded in the served JavaScript and in the `<base href>`
  tag of every page, and it was inserted without encoding, so a quote ended the
  surrounding text and the browser read the rest as instructions. The value is now
  encoded for the context it lands in. Deployments using an ordinary prefix such as
  `/auth`, or no prefix, are served exactly the same bytes as before.

## [1.1.4] - 2026-08-25

### Changed

- Multi-architecture image builds are considerably faster. The CSS build stage now
  runs on the build machine's own architecture instead of being emulated once per
  target platform.

## [1.1.3] - 2026-08-25

### Security

- Updated `cryptography` from 48.0.1 to 50.0.0.

## [1.1.2] - 2026-06-21

### Security

- Updated `cryptography` from 46.0.7 to 48.0.1.

## [1.1.1] - 2026-06-18

### Fixed

- Access log entries no longer show an empty token name for tokens that were
  renamed or recorded before the name was stored alongside the entry. The name is
  now resolved from the token when the entry does not carry one.
- Validation results cached before 1.1.0 are discarded instead of being served.
  They lack the token name and produced incomplete access log entries until the
  cache expired.

## [1.1.0] - 2026-06-18

### Added

- Activity and access logging, with a Logs tab in the dashboard. Activity logs
  record administrative actions; access logs record token validation attempts with
  their result and calling IP address.
- `ACTIVITY_LOG_RETENTION_DAYS` and `ACCESS_LOG_RETENTION_DAYS`, both defaulting to
  30 days. Older entries are purged automatically.

## [1.0.10] - 2026-06-14

### Added

- The API documentation pages, their fonts and the QR code library are served from
  the container instead of from public CDNs, so the dashboard and the docs work in
  installations without outbound internet access.

## [1.0.9] - 2026-06-14

### Changed

- Release tooling only. Updated the GitHub Actions used to build and publish the
  images. No change to the service itself.

## [1.0.8] - 2026-06-12

### Security

- Updated `cryptography` from 44.0.0 to 46.0.7.

## [1.0.7] - 2026-06-12

### Changed

- Release tooling only. No change to the service itself.

## [1.0.6] - 2026-06-12

### Added

- Expired tokens are deleted automatically. A background job runs at startup and
  then every minute, removing expired tokens and dropping their cached validation
  entries.

### Fixed

- Expired tokens are now rejected consistently by `/validate`, including tokens
  whose expiry timestamp was stored in a format the previous comparison did not
  handle.

## [1.0.5] - 2026-04-29

### Added

- `RATE_LIMIT_MAX_REQ` and `RATE_LIMIT_WINDOW_MS` to tune the per-IP rate limit on
  `/validate`. The previous fixed limit of 120 requests per minute is now the
  default.

## [1.0.4] - 2026-04-28

### Added

- `TZ` to control the timezone used for dates shown in the dashboard. Defaults to
  UTC.

## [1.0.3] - 2026-04-26

### Fixed

- Stylesheets, scripts and images failed to load when the service was hosted under
  a URL prefix.

## [1.0.2] - 2026-04-26

### Added

- `ROOT_PATH` for hosting the service under a URL prefix such as `/auth`, behind a
  reverse proxy.

### Changed

- `docker-compose.yml` now pulls the published image from GHCR instead of expecting
  a locally built one.

## [1.0.0] - 2026-04-26

Initial release. Self-hosted token authentication and authorization service with an
admin dashboard, TOTP-protected sessions, scoped API keys, authorization zones and a
token validation endpoint.

[Unreleased]: https://github.com/ShlomiPorush/auth-master/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/ShlomiPorush/auth-master/compare/v1.1.5...v1.2.0
[1.1.5]: https://github.com/ShlomiPorush/auth-master/compare/v1.1.4...v1.1.5
[1.1.4]: https://github.com/ShlomiPorush/auth-master/compare/v1.1.3...v1.1.4
[1.1.3]: https://github.com/ShlomiPorush/auth-master/compare/v1.1.2...v1.1.3
[1.1.2]: https://github.com/ShlomiPorush/auth-master/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/ShlomiPorush/auth-master/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/ShlomiPorush/auth-master/compare/v1.0.10...v1.1.0
[1.0.10]: https://github.com/ShlomiPorush/auth-master/compare/v1.0.9...v1.0.10
[1.0.9]: https://github.com/ShlomiPorush/auth-master/compare/v1.0.8...v1.0.9
[1.0.8]: https://github.com/ShlomiPorush/auth-master/compare/v1.0.7...v1.0.8
[1.0.7]: https://github.com/ShlomiPorush/auth-master/compare/v1.0.6...v1.0.7
[1.0.6]: https://github.com/ShlomiPorush/auth-master/compare/v1.0.5...v1.0.6
[1.0.5]: https://github.com/ShlomiPorush/auth-master/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/ShlomiPorush/auth-master/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/ShlomiPorush/auth-master/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/ShlomiPorush/auth-master/compare/v1.0.0...v1.0.2
[1.0.0]: https://github.com/ShlomiPorush/auth-master/releases/tag/v1.0.0
