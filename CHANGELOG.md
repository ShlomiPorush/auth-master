# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/ShlomiPorush/auth-master/compare/v1.1.5...HEAD
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
