# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Push updates over SSE**: `GET /flags/stream?project=<name>&version=<n>`
  on the `http` server streams `version` events whenever the project version
  goes above `n` (the current version is sent right away if the client is
  behind). Events carry only the version, clients fetch flags with `/sync`.
  Event ids are versions, so standard SSE clients resume via `Last-Event-ID`.
  Streams are served with `sse-starlette` and end cleanly when the server
  shuts down, so clients reconnect to another instance right away.
  Saves send a Postgres `NOTIFY` in the same transaction as the version bump,
  so it's delivered only on commit; every `http` instance `LISTEN`s and
  re-reads watched versions after reconnecting and every
  `http.sse_reconcile_interval` seconds (default 30), so no update is lost
  while the listener is down. In `readonly` mode versions are polled every
  `http.sse_replica_poll_interval` seconds (default 1) instead. Open streams
  are exposed as the `sse_streams` gauge, listener reconnects as
  `project_version_listener_reconnects_total`.
- **Optimistic locking**: `saveFlag` / `saveValue` accept the `version` the
  client loaded and reject the save if the flag/value changed since.
- **Load test** (`loadtest/`): locust users holding SSE streams like the SDK
  does, with commit-to-client latency reports.
- `postgres.pool_maxsize` (default 10) sets the database connections per
  process.

### Changed
- `saveFlag`, `resetFlag`, `deleteFlag` and their value counterparts apply
  all their writes (rules, version bump, changelog) in one transaction.
- Requires FastAPI >= 0.143 and Starlette 1.x (`sse-starlette` needs
  Starlette >= 0.49), which also brings Pydantic 2.14 and Uvicorn 0.54.
- `http` server shutdown waits at most `http.shutdown_timeout` seconds
  (default 5) for in-flight requests.
- `/flags/load` and `/flags/sync` build a project's flags once per version
  (`async-lru`, concurrent requests share the build): after a change every
  connected client syncs to the same version at the same moment.
- Project versions come from one sequence (`project_version_seq`, migration
  `d7e2a9b4c1f6`), so they only go up: a project deleted and created again
  continues above its old versions, which SDKs, their snapshots and the SSE
  hub would otherwise ignore. New projects start above 0.
- `/flags/load` registers variables, flags and values with one query per
  kind and refreshes "last reported" at most once a minute per row. It ran
  two queries per flag, every one updating the same rows, so clients
  starting together queued on row locks and then on the pool: 1000 clients
  starting at 50/s saw 89% of their loads fail; now none do (0.48s mean).
- New streams take the project version from memory while the listener is
  up, instead of each querying the database (with 10k streams connecting,
  the 10-connection pool timed out and most streams never got an event).

### Fixed
- Mutations in one GraphQL request no longer share change tracking: a
  rejected save was audited by the next save in the request, and a second
  save re-checked the first one's flags and was rejected as a conflict.
- `deleteProject` runs in one transaction; a failure halfway no longer
  leaves the project without its flags.
- New flags and values get their creation time from the insert, not from
  when the server started (`default=datetime.utcnow()` ran at import).
- The SSE hub's reconnect backoff for its `LISTEN` connection resets after
  the connection stayed up for 60s. It only grew before, so after a few
  outages a later one waited the full 30s before reconnecting.
- Deleting a flag or value bumps the project version, so clients stop
  evaluating it.
- **UI**: a signed-out visit rendered a blank page (React error #300):
  `Base` returned the sign-in redirect before some of its hooks.
- The index page works on Starlette 1.x (`TemplateResponse` takes the
  request first).
- The Docker image builds again: the base moved from bullseye (end of life,
  its security archive 404s on libpq-dev) to bookworm, and hishel is pinned
  below 1 for pdm 2.26.
- `lets` works in clones without release tags (`git describe --always`).
- A save based on a stale view no longer silently drops conditions while
  reporting success.

## [2.0.0] - 2026-07-31

### Added
- **Production change confirmation**: new `ui.confirmation_required` config option. When
  enabled, applying, resetting, or deleting a flag/value in the UI asks for confirmation via
  an antd dialog before the change is sent.
- **Slack notifications**: flag and value changes are posted to Slack via
  incoming webhooks. Notification channels (name + webhook URL) are managed
  on a new global settings page (gear icon in the sidebar) and selected
  per-project in project settings. Delivery is best-effort and async;
  sends are counted in the `slack_notifications_total` Prometheus metric and
  failures in `slack_notification_errors_total` (both labelled by `channel`).
- **Readonly replica mode**: new `readonly` config option. When enabled, the
  `http` server skips all client-triggered DB writes (flag registration,
  report timestamps) so the server can run against a readonly Postgres
  replica in a second datacenter. Unknown projects are served as an empty
  state with `version: 0` instead of failing.

### Fixed
- **UI**: Redirect to the root page right after a project is deleted ([#61](https://github.com/evo-company/featureflags/issues/61)). The projects list is refreshed via Apollo cache instead of a delayed full page reload, and the deleted project is cleared from the selection state.

### Removed
- **gRPC Server**: Removed the gRPC server, protobuf definitions and related dependencies; the gRPC API is superseded by the HTTP API
  - Removed `featureflags/rpc/` (server, servicer, container, db sync, metrics) and the `rpc` CLI command
  - Removed `featureflags/protobuf/` (`.proto` files and generated modules) and `graph/proto_adapter.py`
  - Removed `grpclib`, `grpcio`, `protobuf` and `types-protobuf` dependencies
  - Removed the `rpc` service from docker-compose, `grpc_health_probe` from the Dockerfile, rpc config sections and gRPC docs

### Internal
- Release workflow now creates a GitHub Release for each pushed `v*` tag, with notes taken from the matching `CHANGELOG.md` section (auto-generated when missing) and pre-release marking for non-final versions

## [1.26.0rc3] - 2026-06-10

### Added
- **Flag State Metric**: New Prometheus gauge `flag_state` with `flag` and `project` labels, set to 1/0 whenever a flag is enabled or disabled

## [1.26.0rc2] - 2026-05-21

### Added
- **Env Var Substitution in Config**: Support resolving config values from environment variables
  - OIDC `client_secret` can be resolved from an env var via `$VAR` indirection
  - Support for `${VAR}` and inline env-var substitution; invalid env var names pass through unchanged

## [1.26.0rc1] - 2026-05-21

### Added
- **OIDC Authentication**: New OpenID Connect authentication support
  - New `featureflags/services/oidc_auth.py` service and `/oidc` web API endpoints
  - `oidc_subject` column added to auth user (database migration included)
  - UI login flow updated to support OIDC

## [1.25.0] - 2026-01-09

### Added
- **Project Label in Metrics**: Added `project` label to gRPC and HTTP handler metrics to distinguish between projects
- **Prometheus Metrics Documentation**: New documentation page describing exposed Prometheus metrics

### Internal
- Restructured and updated documentation
- Updated pdm and lock file

## [1.24.4] - 2025-10-10

### Fixed
- **Value Condition Override Input**: Made value condition override input single per condition
  - Fixed bug with editing an existing condition (order of `disable_condition` and `add_condition` operations)
  - Fixed bug with the position of the first condition in the list
  - Improved inputs layout with Flex

## [1.24.3] - 2025-10-10

### Internal
- Updated README

## [1.24.2] - 2025-10-10

### Changed
- **UI Improvements**: Improved conditions ordering in the UI and made the layout wider

### Fixed
- Fixed GraphiQL

### Internal
- Updated getting started docs and added code comments

## [1.24.1] - 2025-08-11

### Internal
- **Documentation**: Added RST documentation with Read the Docs config and UI image

## [1.24.0] - 2025-07-29

### Added
- **Version Display Feature**: Added version display in the UI footer
  - Server version from `featureflags.__version__`
  - Build version from `featureflags.__build_version__` (which is taken from `BUILD_VERSION` environment variable)
- **GraphQL Version API**: New GraphQL field `version` for version information
  - `version` query returns `Version` node with `serverVersion` and `buildVersion` fields

### Internal
- **Cross-platform Release Script**: Fixed release script compatibility
  - Added macOS (BSD sed) and Linux (GNU sed) compatibility
  - Fixed `sed -i` command for cross-platform deployment

## [1.22.0] - Previous Release

Initial release with core feature flags functionality.
