# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Zero-downtime updates** with docker rollout: each image carries the labels
  `auth-master.compose-sha256` and `auth-master.rollout-from`, which say whether a release can
  run beside the running one. `docs/deploy.md` describes what the host's Compose file and `.env`
  need, what happens while two containers run, and how to update by hand.

### Changed

- A version tag publishes its image only after every check of the Tests workflow passed, and
  never pushes a version that GHCR already has, so `X.Y.Z` always names the same image. An empty
  CHANGELOG section now stops the release before anything is pushed, and a tag that is not
  `vX.Y.Z` with plain numbers is refused.

### Fixed

- On PostgreSQL, a starting service no longer takes table locks for columns and indexes that
  already exist. A second instance that started while the first one ran a long Usage report or
  while a VACUUM ran could make `/validate` wait until the report or the VACUUM ended.

## [2.5.0] - 2026-11-16

### Added

- **Move to PostgreSQL** in the Database section of the Settings page: an installation that
  uses the SQLite file chosen in setup moves to an empty PostgreSQL database while the service
  runs. The check names every row that cannot be moved; the move copies everything in one
  PostgreSQL transaction and compares the copy, and at the switch requests wait a few seconds.
  It needs the authenticator code or a passkey, and each move is an Activity Log entry
  (`database.migrated`, or `database.migration_failed`). New endpoints
  `POST /admin/api/settings/database/move/check` and `POST /admin/api/settings/database/move`.
  See "Move from SQLite to PostgreSQL" in README.md.

### Changed

- After a move, the old SQLite file stays in the data directory, locked against writes: its
  `installation` row is `pending` with the move's id, and triggers refuse every write. Start-up
  never serves it, with or without `data/database.json`, and with `DATABASE_URL` the service
  does not start on it. `data/database.json` gains `migrationId` and `movedFromSqlite`, which
  2.3 and 2.4 ignore.
- With `DATABASE_URL`, the service no longer starts on a database whose `installation` row is
  not active (an unfinished copy from a move).
- The access and activity logs replace the NUL character with U+FFFD on both backends. Before,
  a `/validate` request with NUL in `area` failed with 500 on PostgreSQL.

### Fixed

- On SQLite, requests could fail with 500 "database is locked" under concurrent load, for
  example `/validate` calls while an admin changes tokens: a write sent while another request's
  read was still open on the shared connection failed at once instead of waiting for the write
  lock. Each call on the shared connection now finishes before the next one starts.
- In the Docker image, every PostgreSQL connection failed (setup, the Settings page, the
  recovery page and `DATABASE_URL`), reported as "The database server cannot be reached":
  the service ran as uid 10001 with `HOME=/root`, and the PostgreSQL driver stopped with a
  permission error while it looked for client certificates in `~/.postgresql`. The service
  now runs with the service user's own home, `/nonexistent`. Affects 2.3.0 and 2.4.0.

### Upgrade notes

- After a move to PostgreSQL, do not roll back below 2.3: older images open the old SQLite
  file, whose lock refuses their first write at start-up, so they stop (checked with 2.2.0).
  Do not roll back to the 2.3.0 or 2.4.0 images either: they cannot connect to PostgreSQL
  (see Fixed).

## [2.4.0] - 2026-11-16

### Added

- The **Date format** and **Time format** of every date on the dashboard are server settings
  in Settings > General, sent to every page by `GET /admin/api/me` (`dateFormat`,
  `timeFormat`). `DATE_FORMAT` and `TIME_FORMAT` set them and lock the fields. They are
  stored in the table `app_state` (key `display_settings`), so the database schema does not
  change. The two fields leave **Preferences**, and the choice each browser kept there is no
  longer read.
- Each Settings section has its own address, such as `/admin/settings/database`.

### Changed

- The dashboard follows the v3 design throughout:
  - A top bar on wide screens holds the page title, its help button, a search of the whole portal (press `/`) and the page's actions. The sidebar folds into an icon rail, and the browser remembers that choice.
  - Every page has a help button beside its title.
  - Settings is split into sections with one save bar for all of them. Each change is marked, and a field can be reset to its default. Stored secrets show as stored, with Replace. Questions about risky changes come up in a dialog before saving.
  - The logs load more rows as you scroll and offer new entries at the top.
  - The Usage chart zooms, and an overview strip shows the whole period.
  - A list that could not be updated says so and keeps its rows.
- Every word the dashboard shows comes from one text catalog, `services/static/public/js/text.js`, and the wording was rewritten throughout.

### Fixed

- A database connection change whose answer was lost no longer reads as "nothing running"
  while it still waits for its second-factor check: `GET /admin/api/settings/database` reports
  it as `{"state": "pending"}` until its operation starts or the request ends.

### Upgrade notes

- The Date format and Time format each browser kept in **Preferences** in 2.3.x are no longer
  read. Every page uses the server setting instead: Browser order and 24-hour until they are
  set in Settings > General, or by `DATE_FORMAT` and `TIME_FORMAT`.
- Rolling back to 2.3.x needs nothing: 2.3.x ignores the `display_settings` key in
  `app_state`, and each browser's earlier Preferences choice applies again.

## [2.3.0] - 2026-11-16

### Added

- First-time setup starts with the database: the SQLite file `data/auth.db`, or a
  PostgreSQL server (host, port, database, user, password, TLS mode). **Check connection**
  shows what the database holds before anything changes. A PostgreSQL database that already
  holds an installation of this service is used as it is and leads to sign-in; a database
  with other tables is refused. The choice is saved in `data/database.json` (mode 0600, the
  password encrypted). The service now starts without a database and waits for setup.
- The Settings page has a **Database** panel. It shows the connection, and for a PostgreSQL
  database chosen in setup it changes the host, port, database name, user, password or TLS
  mode of the same installation. The server checks the new connection first (primary server,
  write access, table privileges and ownership, no row-level security, CREATE on the schema,
  and that it reaches the same live database), then switches while the service runs:
  requests wait a few seconds and continue. Saving needs the authenticator code or a
  passkey. A restored copy of the database can be chosen after a confirmation. Each change is
  an entry in the Activity Log, without the password.
- Recovery mode: when the saved database cannot be used at start, the service does not start
  setup again. `/health` and the API answer 503 with a code, and the dashboard opens the
  recovery page `/recovery`, which needs the master key (`ADMIN_API_KEY`) and takes a
  corrected connection. An unreachable server is tried again every 10 seconds.
- TLS for PostgreSQL connections saved in the UI: `prefer` (the default), `disable`,
  `require`, or `verify-full` with an optional CA certificate.
- A database holding this service's tables from an older version or a backup can be adopted
  in setup and on the recovery page after a confirmation.

### Changed

- `DATABASE_URL` is optional and has no default. When set, it overrides the database chosen
  in setup, as before, and the Settings page shows the connection locked.
- An existing `data/auth.db` is used as before: the first start marks it with an
  installation id and writes `data/database.json`.
- Every database gets the table `installation`, which holds the installation id. At each
  start the id must match `data/database.json`, so a replaced or emptied database is never
  used by mistake.
- `/health` adds a `status` field while setup waits for a database (`setup_required`, status
  200) and in recovery mode (`database_unavailable`, status 503).
- The service runs as the user `app` (uid and gid 10001) instead of root. The container starts
  as root only to give that user the data directory `/app/data` and the files in it, checks
  that the user can read and write them, and then starts the service as that user. When the
  check fails, the container stops and its log lists the paths. See "Data directory and file
  ownership" in README.md.
- When `DATABASE_URL` names a SQLite file outside `/app/data`, the container also gives the
  service user the directory of that file and the database files in it, when the directory
  holds nothing else (or already belongs to the service user). A directory with other files,
  `/` and system directories are never changed: the container stops and names the reason.

### Fixed

- A start that fails because the SQLite file cannot be written now ends the process. Before,
  the service logged "Application startup failed" and kept running without serving.

### Upgrade notes

- Rolling back to 2.2.x after PostgreSQL was chosen in setup needs `DATABASE_URL` in `.env`:
  2.2.x ignores `data/database.json` and would otherwise create a new, empty `data/auth.db`.
  See "Roll back to 2.2.x" in README.md.
- Changing the database connection needs a single service process for the data directory.
- The data directory (`data/`) must be writable, also with `DATABASE_URL`: every process
  registers itself there at start, and one that cannot does not start.
- On the first start, the owner of `./data` and the files in it changes to uid 10001 on the
  host. Scripts that read or copy `./data` as another user need root or that uid.
- A SQLite file outside `/app/data` (a `DATABASE_URL` such as `sqlite:////db/auth.db`) must be in
  a directory that holds only the database; its directory then belongs to uid 10001.
- Files in `./data` with more than one hard link, and symlink targets, are not changed. Replace
  them with regular copies if the service cannot read and write them.
- The data directory must be mounted writable. On an NFS export with `root_squash`, give it to
  uid 10001 on the server and start the container with `user: "10001:10001"`.
- `docker compose exec` still runs as root. Use `--user 10001:10001` for anything that writes
  to `/app/data`.
- Rolling back to 2.2.0 works with the same files, because that version runs as root.

## [2.2.0] - 2026-11-15

### Added

- Passkeys for the admin dashboard. An admin can sign in with a passkey
  (fingerprint, face, PIN or screen lock) instead of the password and the
  authenticator code, and can confirm "Copy token" and "Copy key" with a passkey
  instead of the code. Passkeys are added, renamed and deleted on the new Account
  page (`/admin/account`); adding one needs the authenticator code or another
  passkey. The password and the code keep working and are the way back when a
  passkey is lost. Passkeys need HTTPS, or `http://localhost`.
- The Settings page has a **Passkeys** panel for the passkey address, and the
  `PASSKEY_ORIGIN` setting overrides it and locks the field. The first passkey sets it
  to the address the dashboard is open at. Passkeys made for another host name stop
  working when the address changes. Each change is a new version in the table
  `passkey_settings` and an entry in the Activity Log. See "Passkeys" in README.md.
- New Python dependency: `webauthn` 3.0.1 (py_webauthn), which builds and checks
  the passkey requests, with `cbor2`, `pyOpenSSL`, `pyasn1` and `pyasn1-modules`.
- The Settings page has a **General** panel for the time zone, the activity log and
  access log retention, and the Secure session cookie. Each save is a new version in
  the table `app_settings` and an entry in the Activity Log. A lower retention first
  shows how many entries it will delete and asks you to confirm. Turning on the Secure
  cookie from a page opened over plain HTTP asks you to confirm too. The panel also shows
  `ROOT_PATH` read-only.
- The Settings page has a **Redis connection** panel. A new URL is saved only after the
  server connects with it and runs the commands the service uses on test keys under each of
  its key prefixes; it takes effect when every instance of the service restarts. The page never shows the password. The saved
  URL is stored encrypted in the table `redis_settings`.
- The MFA step of the first setup has a **Name in authenticator apps** field
  (`TOTP_ISSUER`, default `AuthService`). A new name updates the QR code.
- The API Keys page shows a master key set by `ADMIN_API_KEY` as a locked row.
- The Usage report names its hours and days in the time zone it was counted in. When an
  earlier, shorter retention deleted part of the period, the page says so.

### Changed

- "Copy token" and "Copy key" (`POST /admin/api/tokens/{id}/reveal` and
  `POST /admin/api/api-keys/{id}/reveal`) accept `{"passkey", "ceremony"}` in
  place of `{"code"}`, and their Activity Log entries say which one was used.
- The Usage page reads a summary of the access log instead of every row, so it
  answers in a fraction of a second also with millions of log rows (before, the
  30-day view could take longer than the page waits). The figures are the same and
  stay up to date: the service summarises each 15-minute period once it has ended,
  and reads the calls that are not summarised yet from the access log itself. The
  first start of this version summarises the rows already in the log in the
  background, newest first; until that ends, which takes a few minutes for a few
  million rows, the 30-day view can be slower than before.
- `TZ`, `COOKIE_SECURE`, `ACTIVITY_LOG_RETENTION_DAYS`, `ACCESS_LOG_RETENTION_DAYS`,
  `REDIS_URL` and `TOTP_ISSUER` now only override a field on the Settings page or the setup
  page, which then shows it locked. At each start, `TZ`, `COOKIE_SECURE` and the retention
  values set in the environment are saved on the Settings page, so removing them later keeps
  their last value. `REDIS_URL` is saved once a check against it passes.
- `ADMIN_API_KEY` is optional. Empty or unset means no master key. `docker-compose.yml` no
  longer requires it, and `tools/generate-env.sh` and `tools/generate-env.ps1` no longer
  create it. A set value keeps working, and a publicly known value is still refused.
- `docker-compose.yml` no longer sets `TZ` or `ALLOWED_AREAS`, and it sets
  `REDIS_DEFAULT_URL` to the bundled Redis instead of `REDIS_URL`. A value in `.env` still
  reaches the service. When `COOKIE_SECURE` is not set, it passes it empty instead of
  `false`, so the Secure session cookie field is not locked. An empty `COOKIE_SECURE` counts
  as not set; `true` or `false` still locks the field.
- `ALLOWED_AREAS` is deprecated. On the first start, an empty zones table gets its zones
  once (`orders,billing,webhooks` when it was not set); entries that differ only in case
  become one zone, with the first spelling. Zones then come only from the zones table, and
  the Zones page shows them. After the last zone is deleted, `ALLOWED_AREAS` no longer
  applies: a grant needs a zone.
- An `ACTIVITY_LOG_RETENTION_DAYS` or `ACCESS_LOG_RETENTION_DAYS` below 1 or above 3650 now
  stops the start. Before, `0` or a negative value deleted every log entry.
- A server process deletes old log entries only when its retention equals the saved one.
  Otherwise it skips the cleanup, and the Settings page shows why.

### Removed

- `ENVIRONMENT`. The service never read it.
- The `BOOTSTRAP_TOKEN` setting and the `X-Bootstrap-Token` header. The first
  person who completes the setup on `/setup`, including MFA, becomes the admin,
  and the setup page no longer has a bootstrap token field. A `BOOTSTRAP_TOKEN`
  that is still set is ignored. Complete the setup before the service can be
  reached from other machines.

### Upgrade notes

- An existing `docker-compose.yml` still sets `TZ` and `COOKIE_SECURE`, so those fields
  stay locked until you change it. Remove the `TZ` and `ALLOWED_AREAS` lines, replace the
  `COOKIE_SECURE` line with `COOKIE_SECURE: ${COOKIE_SECURE-}`, replace the `REDIS_URL` line
  with `REDIS_DEFAULT_URL: redis://:${REDIS_PASSWORD:?...}@redis:6379`, and replace the
  `ADMIN_API_KEY` line with `ADMIN_API_KEY: ${ADMIN_API_KEY:-}`. README.md has the exact
  lines.
- After a Redis URL is saved on the Settings page, changing `REDIS_PASSWORD` alone breaks the
  connection. README.md, "Changing secrets", has the steps.
- Changing `SESSION_SECRET` or `APP_ENCRYPTION_KEY` also makes the saved Redis URL
  unreadable. Set `REDIS_URL` in the environment, start, and save the URL again.
- To roll back to 2.1.x, first put the retention values, `TZ` and `COOKIE_SECURE` from the
  Settings page into `.env`, use the `docker-compose.yml` of 2.1.x (or set `REDIS_URL` and
  `COOKIE_SECURE` in `.env`, because 2.1.x does not start without `REDIS_URL` and refuses
  an empty `COOKIE_SECURE`), and set `ADMIN_API_KEY`. Without the retention values, 2.1.x
  uses 30 days and deletes older log entries within a minute. README.md, "Roll back to
  2.1.x", has the steps.
- Version 2.1.x has no passkeys. After a roll back, admins sign in with the password and
  the authenticator code; the passkeys stay in the database and work again after an upgrade.

## [2.1.0] - 2026-11-15

### Security

- Admin sessions and unfinished MFA setups in Redis are bound to this
  installation's `SESSION_SECRET`: each record is stored under a name derived
  with that secret from the session ID (or, for a setup, the admin ID), and
  encrypted and authenticated with a key derived from it. Before, the session ID
  itself was the key and the session and the new MFA secret were stored as plain
  text, so two installations sharing one Redis database accepted each other's
  admin sign-ins, and any client that could write to that Redis database could
  create an admin session or plant the MFA secret of an admin being set up.
  README.md no longer says the session cookie is signed: it holds only a random
  session ID.
- **Upgrade note:** every admin signs in again once after the upgrade, and a
  first-admin setup that is in progress during the upgrade must start again.
  From now on, changing `SESSION_SECRET` also signs every admin out. Give every
  installation its own `SESSION_SECRET`.
- `/validate` (POST and GET) takes an `area` of at most 255 characters, counted
  before surrounding spaces are trimmed. A longer `area` gets `422` and is not
  written to the access log. Before, a single anonymous call could store an area
  of any size. The access log also keeps at most 255 characters of any area.
  Zone names are at most 63 characters, so no zone is affected. An `ALLOWED_AREAS`
  entry longer than 255 characters can no longer be validated, and neither can the
  grants already stored for it, even after zones were added to the table.
- `/validate` answers an `area` that is not valid Unicode text, such as a lone
  `\ud800` escape in a JSON body, with `422` instead of `{"result": false}`.
- The request log in the container output no longer shows the token of a
  `GET /validate?token=...` call: the query string of `/validate` is shown as
  `[redacted]`. API.md now recommends `POST /validate`, which sends the token in the
  request body. Logs written before this version can still hold tokens sent with
  `GET`: rotate those tokens if other people can read those logs.
- The `/validate` rate limit counts an IPv6 client by its /64 network, so changing
  addresses inside one /64 no longer gets more requests. All clients in one /64
  share one limit. A client that holds a larger block still gets one limit per /64
  it uses: 256 for a /56, 65,536 for a /48. `::ffff:192.0.2.1` counts as
  `192.0.2.1`, and clients whose reported address is not an IP address share one
  limit. The Access Logs page and the `Rate limit:` log line name the /64 network.
- The rate limit forgets an address once its window has ended, instead of keeping
  every address it has seen until the server restarts. When the window length is
  changed, a window that has already ended stays ended; a window in progress is
  measured against the new length, as before.
- A token `expiresAt` without a UTC offset, such as `2026-06-01T00:00:00`, is
  read as UTC on every database. On PostgreSQL it was read in the server time
  zone (`TZ`), so with a zone west of UTC a token kept passing `/validate` for
  up to the zone offset after the time it was given to expire. This applies to
  creating and updating tokens through `/tokens` and the admin interface. Tokens
  already stored this way on PostgreSQL, on a server whose `TZ` is not UTC, keep
  their shifted expiry: set their expiry again.
- The service sends `X-Frame-Options: DENY` and `Content-Security-Policy:
  frame-ancestors 'none'` itself, so browsers no longer show the dashboard, the
  sign-in and setup pages or the API docs inside a frame on another site. Before, a
  page on a sibling subdomain could overlay the signed-in dashboard and steer an
  admin's clicks. The only response without the headers is the plain-text 500 for an
  unexpected server error.
- Dashboard sign-in no longer holds up the rest of the service. The password check
  (bcrypt) ran on the server's only event loop, so a few concurrent sign-in attempts,
  which need no account, delayed every other request, `/validate` included, by close
  to a second. It now runs in worker threads, at most two at a time. When both are
  busy for more than 10 seconds, sign-in and first-run registration answer
  `503` with `Retry-After`.
- Sign-in is limited to 10 attempts per client address in 15 minutes, counted from
  the first attempt; IPv6 addresses count per `/64` network. Further attempts answer
  `429` with `Retry-After` and a message that says when to try again. Every attempt
  counts, a correct password does not reset the count, and the limit never locks a
  username: an attacker elsewhere cannot lock the admin out. Behind a reverse proxy
  that `FORWARDED_ALLOW_IPS` does not trust, every client has the proxy's address and
  shares one count of 10, so set it as described in README.md.
- Sign-in with an unknown username now takes as long as a wrong password, so the
  answer time no longer tells which usernames exist. (A password hash with a bcrypt
  cost other than 12, which only a hand-edited database can hold, still works but
  takes a different time.)
- **Upgrade note:** the sign-in limit keeps its counts in Redis under `auth:login:*`.
  The bundled Redis needs no change. An external Redis with access control lists
  must allow the app's user `SET`, `INCRBY`, `TTL`, `EXPIRE`, `MULTI` and `EXEC` on
  these keys, next to the `GET`, `SETEX`, `DEL` and `PING` it already needs, for
  example `+get +setex +del +ping +set +incrby +ttl +expire +multi +exec ~auth:*`.
  If `REDIS_URL` selects a database other than 0, `+select` is needed too. Without
  them, sign-in can fail with `500`; the limit is never skipped.
- Request bodies are capped at 64 KiB. Before, the service read and parsed a body of
  any size before its own checks, so an anonymous caller could make it buffer and
  parse large bodies on `/validate`, sign-in and setup. A larger body now gets
  `413 Content Too Large`, and the dashboard says the item is too large to save.
  A request that takes no body (such as `DELETE`) but declares a body over the cap
  is still handled, and its answer is replaced by `413`, so the `413` does not tell
  whether it succeeded; see "Request size" in API.md.
- Authenticator codes are limited to 5 tries per admin in 15 minutes, counted
  together for sign-in, token reveal and API key copy. After the fifth wrong code the
  service answers `429` with `Retry-After` until the 15 minutes from the first try
  have passed, also for a correct code; at sign-in the started sign-in ends and the
  password is needed again. Each wrong code is written to the activity log as
  `mfa_failed`, and the lockout as `mfa_locked`. The counts and the used codes are
  kept in Redis under `auth:mfa-fail:*` and `auth:totp-used:*`, with the same commands
  as the sign-in limit, so a Redis access control list set up for the sign-in limit
  needs no change.
- Each authenticator code is accepted only once. A reveal or copy right after
  signing in, or two in a row, needs the next code, which can take up to 30 seconds.
- After the password, the authenticator code must be entered within 5 minutes
  (was 12 hours).
- First-admin setup completes only once. Before, a setup session whose account a
  later registration had replaced could still finish MFA enrollment after another
  admin completed setup, and it got a full admin session that could, for example,
  create API keys. Two setups confirmed at the same moment could also both
  succeed. Now confirming MFA succeeds only for an account that still exists and
  has not enrolled yet; any other attempt is refused with 403.
- An admin session is accepted only while its admin account exists and has MFA
  enabled. Other sessions are refused and deleted.
- The service allows at most one admin with MFA enabled, and this is now enforced
  by the database (index `admin_users_one_enabled`). Only first-admin setup creates
  admins, so this matches how the service already works; the rule stays until the
  service gets admin management. Upgrade: if the database already has two admins
  with MFA enabled, which only the flaw above could cause, the service does not
  start and says so. Delete the admin row that should not exist, and any API keys
  it created, then start the service again.
- Zone names are now unique regardless of case. Creating or renaming a zone to a
  name that differs from another zone's only in case (`billing` next to `BILLING`)
  is refused with 409, on the Zones page and on `POST /tokens/zones`. Before, such
  zones could both exist, and because `/validate` compares areas regardless of case,
  a token for one of them passed for the other.
- Renaming or deleting a zone now also updates the grants whose area differs from
  the zone name only in case or surrounding spaces, the way `/validate` compares
  them. Before, such a grant stayed behind after the zone was deleted and kept
  passing `/validate`.

### Added

- A Usage page (`/admin/usage`) shows, for each token, how many `/validate` calls it
  made in the last 24 hours, 7 days or 30 days, how many were denied, which zones it
  called at which levels, and from which addresses, with a chart of allowed and
  denied calls per hour or day in the server's time zone. Calls with a token the
  service does not know, and calls of deleted tokens, are shown as their own rows.
  The figures come from the access log: days older than `ACCESS_LOG_RETENTION_DAYS`
  are marked as deleted, not as zero. The Tokens page row menu opens a token's usage.
- Access Logs takes `?token=<id>` and shows it as a filter that can be removed.
- README.md has a "Remote access and HTTPS" section: the service speaks plain HTTP, so
  reach it from other machines only through an HTTPS reverse proxy, do not expose port
  8080 to networks you do not trust, and set `COOKIE_SECURE=true` behind HTTPS.
  API.md gives the proxy's `https://` address as the base URL.

### Changed

- Token grants accept an area in any case and store it with the zone's exact name.
  Naming one zone twice in a request, in different cases, is refused with 400.
- A zone rename is refused with 409 when a token holds the zone in more than one
  spelling (possible with grants saved from an `ALLOWED_AREAS` list that names the
  zone twice): after the rename the token would hold the new name twice, and
  `/validate` reads only the first. The message names the tokens; edit them on the
  Tokens page, where the zone's row shows a notice and the permissions of every
  spelling, save, and rename again.
- If zones that differ only in case already exist, the service still starts, logs a
  warning and shows it on the Zones page. Until all but one of each group are renamed
  or deleted, a token for one of them passes `/validate` for the others, and the
  database index that keeps names unique regardless of case is created at the first
  start after that. The protection is complete once every running instance is
  updated.
- On PostgreSQL, deleting a zone no longer removes entries without an area from the
  grants of the tokens that held the zone.

### Fixed

- `FORWARDED_ALLOW_IPS` and `COOKIE_SECURE` set as stack variables, for example in
  Portainer, now reach the container: the bundled compose file passes them. When a
  variable is not set at all it passes `127.0.0.1,::1` (uvicorn's default) and
  `false`. Docker Compose now takes these two values from the shell or stack
  variables, then from the project's `.env` or the files given with `--env-file`, and
  they win over `env_file: .env`. If you start the stack with `--env-file` and keep
  them only in `.env`, move them to your env file, or add `--env-file .env` before
  your other `--env-file` options, so that your own file keeps winning.
- The Settings page keeps the reverse proxy hint and the warning about exempt
  addresses when `FORWARDED_ALLOW_IPS` lists only loopback addresses, as the
  compose default does, since no proxy at another address is trusted then.
- A token that expires at the last moment of year 9999 in UTC no longer breaks
  the token lists and `/validate/grants` on a server whose time zone is east of
  UTC: its expiry is shown in UTC.
- Deleting or renaming a zone and changing a token's grants no longer undo or bypass
  each other. On SQLite, a zone delete or rename could restore a grant that was
  removed while it ran. On both databases, a token could keep a grant on a zone that
  was deleted or renamed while the grant was being saved, and `/validate` kept
  allowing it. A grant change now waits for a zone change in progress, and the other
  way round. A grant on a zone that no longer exists is refused with
  `Area not allowed`, or, when the last zone was deleted and `ALLOWED_AREAS` is
  empty, with `Define at least one zone`.
- On SQLite, a zone delete or rename that fails halfway no longer leaves the
  tokens' grants on the old zone name. The zone and the grants change together or
  not at all, so the change can be retried.
- On PostgreSQL, creating a token with a value that another token already uses,
  and renaming a zone to a name that is taken, answer 409 instead of 500.
- On PostgreSQL, creating a zone with a name that is taken answers 409 instead of 500.
- Not changed: grants saved while the zones table was empty (the `ALLOWED_AREAS`
  fallback) keep working after zones are created, and after the last zone is
  deleted, an area named in `ALLOWED_AREAS` can be granted again. Grants that the
  earlier behaviour left on deleted zones are not removed automatically: edit those
  tokens on the Tokens page.

## [2.0.1] - 2026-11-13

### Added

- The exempt list on the Settings page also takes networks, such as
  `172.18.0.0/16`, next to single addresses. A network with address bits beyond its
  size (`172.18.0.5/16`) is refused with the form to write instead. After a network
  is saved, version 2.0.0 refuses to start, because it reads only single addresses:
  remove the network before going back, or use the repair in README.md.
- The Settings page shows `FORWARDED_ALLOW_IPS`, uvicorn's list of trusted reverse
  proxies, as a locked field. The warning about callers sharing the proxy's address
  appears only when it is not set.

### Changed

- The own-address confirmation on the Settings page is asked only when a change
  exempts the admin's own address for the first time, also through a network.

## [2.0.0] - 2026-11-13

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

- **Breaking for `/validate` callers:** a request over the rate limit is answered
  `429` with `{"detail": "Too many requests"}` and a `Retry-After` header (whole
  seconds until the caller's window ends), instead of `200` with
  `{"result": false}`. Before, a valid token over the limit got the same answer as
  a real denial. Callers that read only the body must now handle `429`: retry after
  the given seconds, and do not treat it as a denied token. `/validate/grants` keeps
  its `429` and now sends `Retry-After` too.
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
  still not written to the access log (their new `429` answer is described under
  Changed). Each
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

[Unreleased]: https://github.com/ShlomiPorush/auth-master/compare/v2.5.0...HEAD
[2.5.0]: https://github.com/ShlomiPorush/auth-master/compare/v2.4.0...v2.5.0
[2.4.0]: https://github.com/ShlomiPorush/auth-master/compare/v2.3.0...v2.4.0
[2.3.0]: https://github.com/ShlomiPorush/auth-master/compare/v2.2.0...v2.3.0
[2.2.0]: https://github.com/ShlomiPorush/auth-master/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/ShlomiPorush/auth-master/compare/v2.0.1...v2.1.0
[2.0.1]: https://github.com/ShlomiPorush/auth-master/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/ShlomiPorush/auth-master/compare/v1.2.0...v2.0.0
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
