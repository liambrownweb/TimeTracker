# Personal Time Tracker — v0 Implementation Specification

Status: Prototype specification, 2026-11-06

## 00. Scope, architecture, and working rules

Build a personal, domestic-first time tracker with an HTTP JSON API, central server, KDE Plasma client, and Android client. The service can run on a LAN host or independent VPS; no cloud-vendor dependency. The central service alone retains acknowledged historical time records. Each client keeps unsent entries and in-progress timer drafts locally until acknowledged or completed, respectively.

Principles:
- Capture without prior setup: new task names, categories, locations, and other metadata values become available for future completion.
- API resources and persistence models are separate contracts.
- Prefer readable, portable text files and simple, testable components.
- API endpoints live under `/v0`.
- Use meaningful HTTP methods; POST creates, PATCH modifies, DELETE logically deletes. No PUT required in v0; PATCH and PUT must never create a missing entry.
- Persist timestamps in UTC, ISO 8601 with `Z` notation. Never use filesystem timestamps as data semantics.
- No application authentication/authorization in the v0 prototype. Do not expose an unauthenticated server directly to the public internet.
- Do not add cross-device timer coordination in v0.

Implementation recommendations (not irrevocable requirements): Python 3, FastAPI, Pydantic, pytest, JSONL, and DuckDB for reporting. Favor standard library facilities when adequate.

Repository layout (suggested):
```
README.md
pyproject.toml
docs/{architecture.md,api.md,data-model.md,decisions/}
src/timetracker/{api/v0/,domain/,services/,storage/}
tests/{domain/,storage/,services/,api/,integration/}
data/{entries/,journal/,reports/}
clients/{plasma/,android/}
```

### Server configuration

Server settings live in a TOML file parsed with the standard library (`tomllib`). Locations are resolved in this order; the first that applies wins:

| | Config file | Data directory |
|---|---|---|
| Explicit | `--config PATH` / `TIMETRACKER_CONFIG` | `data_dir` setting / `TIMETRACKER_DATA_DIR` |
| Under systemd (`ConfigurationDirectory=` / `StateDirectory=`) | `$CONFIGURATION_DIRECTORY/config.toml` | `$STATE_DIRECTORY` |
| Otherwise (CLI or ad hoc) | `$XDG_CONFIG_HOME/timetracker/config.toml` (default `~/.config/timetracker/`) | `$XDG_DATA_HOME/timetracker` (default `~/.local/share/timetracker`) |

For a system-level unit with `ConfigurationDirectory=timetracker` and `StateDirectory=timetracker`, systemd supplies `/etc/timetracker/config.toml` and `/var/lib/timetracker` (FHS locations for configuration and persistent variable state; program code is installed separately, e.g. under `/opt/timetracker`). Everything local to a user, whether run from the CLI or a user-level unit, lives under `~/.local/share/timetracker` for data and `~/.config/timetracker` for configuration. User-level units therefore declare `ConfigurationDirectory=timetracker` but not `StateDirectory=` (which would resolve to `~/.local/state`), so the default data path applies. The server needs no separate "system mode" switch; it only honors those variables.

Precedence for individual settings, highest first: command-line options, `TIMETRACKER_*` environment variables, the config file, built-in defaults. A missing config file is handled by context: in the CLI/ad hoc case the server writes a file with every setting at its default and short comments, and never overwrites an existing file. When systemd supplied the configuration directory, the server does not write to it (system config is often read-only); it runs on built-in defaults, logs that no file was found, and `timetracker init-config [--config PATH]` writes the defaults file explicitly. Unknown keys and invalid values are startup errors that name the key; nothing is silently ignored. Settings include at least: `bind_address`, `port`, `data_dir`, and the recovery/verification options of section 03/04. Configuration never changes API contracts or on-disk record formats.

Implementation-agent instruction: implement only the assigned section and its minimal prerequisites; do not rewrite adjacent modules or silently change established contracts. Add focused tests and document any unresolved design decision.

## 01. Domain model and public schemas

An **entry** represents one completed recorded activity. Its stable `id` is a client-generated UUID. Minimum public fields:
- `id`: UUID
- `task`: nonempty string
- `duration_seconds`: nonnegative integer (whole-second resolution for v0)
- `started_at`: optional UTC timestamp (useful for stopwatch captures; manual retrospective entries need not know a start time)
- `attributes`: dictionary of extensible, typed metadata (e.g., `category`, `project`, `location`, `billable`)

Server-managed fields:
- `created_at`, `updated_at`: UTC recording timestamps
- `revision`: integer, starting at 1
- `deleted`: boolean, initially false
- `last_event_sequence`: latest journal event applied
- `history`: array, initially empty
- `schema_version`: operational record format version

An operational entry holds its **latest state in outer fields** and an embedded `history` array:
- On creation, `history=[]`.
- On the first meaningful change, add an `initial` snapshot of the pre-change state, followed by an `update` containing only changed fields.
- Later changes append only deltas; each change carries its UTC timestamp and journal `event_id`.
- Do not record a no-op as a meaningful update.
- Attribute changes need unambiguous patch semantics, including removal; choose and document a representation that distinguishes removal from setting a nullable value.
- Deletion is a recorded state change, not physical erasure.

Journal event minimum: `schema_version`, `event_id` (UUID), `sequence` (server-wide monotonically increasing integer), `event_type`, `entry_id`, `recorded_at` (UTC), and sufficient payload to rebuild the operational state. Define event types for create/update/delete. Events are immutable.

Do not expose journal internals as mandatory client fields. Maintain independent versions for HTTP API, operational records, and journal events.

Acceptance: model validation tests cover valid manual/stopwatch entries, unknown attribute keys, wrong types, missing IDs, malformed UTC timestamps, no-op changes, and deletion semantics.

## 02. Operational storage

Operational records are the normal read/reporting source. Organize by resource domain and the **UTC month of the entry's original `started_at`** (falling back to the UTC month in which the create was committed when no start time was supplied), e.g. `data/entries/2026/11.jsonl`. Filing by activity time keeps entries recorded after a gap, such as a vacation, in the month when they happened. Each JSONL line is one complete current entry including its embedded history. An entry stays in its original monthly file after later changes.

The operational file is mutable. Update an entry by safely replacing its containing file: construct a complete replacement, write a temporary file in the same filesystem, flush and synchronize it, and atomically rename. Serialize writes with a process-level write lock in v0. Keep filesystem access behind a storage interface, not in API handlers.

Operational records are reconstructible from the journal, but they are the authoritative **current read model** during normal operation. Provide lookup by UUID, list/filter, create, update, and logical deletion through the storage adapter. A startup-built in-memory index is acceptable, with deterministic rebuild after restart.

Acceptance: reads do not scan the journal; updates preserve all unrelated records; modified entries remain in original month; interrupted replacement does not leave half-written JSONL; history is readable without other files.

## 03. Immutable transaction journal

Store one transaction event per JSONL line under `data/journal/YYYY/MM.jsonl`, selected by the **UTC journal commit time** (not the activity's start time or original entry month). Journal writes only append. Event sequence numbers are globally ordered across months; do not derive ordering from OS timestamps.

Each committed event must contain enough information to replay its effect. Include a create snapshot and subsequent deltas/deletion events. An event ID links journal events to embedded operational history. Provide an append operation that only returns success after durable persistence; detect incomplete trailing records during startup and recover or quarantine them without silently discarding valid committed history.

Acceptance: a fresh operational store can be rebuilt by ordered journal replay; event IDs and sequence numbers remain unique; month transitions preserve ordering; interrupted trailing writes are handled predictably.

## 04. Transaction coordinator and recovery

All mutations follow this order:
1. Acquire write lock.
2. Validate request against current operational state, including existence and revision where applicable.
3. Compute the meaningful delta and corresponding journal event.
4. Append and durably commit the journal event.
5. Apply the event to the operational record, including embedded history and revision.
6. Durably persist operational state.
7. Acknowledge the request and release lock.

`POST` alone creates entries. PATCH/DELETE on unknown IDs return HTTP 404 (`ENTRY_NOT_FOUND`). Conflicting creation of an existing UUID returns 409. Identical retried POST submissions must not create duplicate journal events; compare the immutable original submission, not a later-edited current state.

Recovery: the journal is the durable recovery authority; if a crash happens after journal commit but before operational update, reconcile missing committed events before serving requests. Use event IDs/sequences to make replay idempotent. A failed response after journal commit does not imply the transaction was rolled back; retries must resolve to the committed result without duplication.

Acceptance: inject failures before journal append, after journal commit, and during operational replacement; restart produces one correct logical mutation and no duplicate event. Concurrent requests cannot lose updates.

## 04.5. Configuration and verification controls

Implement the configuration rules in §00 using `tomllib` and the standard library. Settings are `bind_address`, `port`, `data_dir`, `verify_journal_on_startup`, `verify_cross_check_on_startup`, `deep_check` (`startup`, `interval`, or `manual`), and the optional positive integer thresholds `deep_check_days` and `deep_check_events`. Interval mode requires at least one threshold; the first startup without a completed check runs one. Defaults enable both startup checks and use `deep_check = "startup"`. Configuration, environment, and CLI values must be validated, with errors identifying the setting.

Persist the UTC completion time and journal sequence only after a successful deep comparison, using an atomic, durable state-file replacement. Interval mode runs a check when either configured threshold is reached; event counts are journal events. Invalid state and a stored sequence ahead of the journal are errors, not reasons to silently skip a check.

Provide `timetracker init-config` and `timetracker verify [--deep]`. Verification checks journal integrity and operational correspondence without repairing or changing journal or entry data; a successful deep verification updates the schedule state. Report findings and return a nonzero exit status on corruption or mismatch. Never operate on a data directory concurrently with a running managed server. Startup always handles torn tails and checks the journal tail; the other checks follow their settings, and committed missing updates are reconciled before serving. A failed deep comparison halts startup and does not advance the schedule.

Acceptance: cover configuration locations, overrides and validation, default-file creation without overwrites, systemd behavior, both interval thresholds and restart persistence, failed/interrupted checks, disabled checks, read-only on-demand verification, and startup recovery.

## 05. HTTP API — entries

All endpoints under `/v0`, JSON request/response bodies, conventional status codes, and machine-readable error codes.

- `POST /v0/entries`: create from a client-supplied UUID; 201 and `Location: /v0/entries/{id}` on new creation; 200 for identical retried original submission; 409 for conflicting UUID/content; 422 for invalid content.
- `GET /v0/entries`: list/filter current nondeleted entries (pagination and filter contract to be finalized).
- `GET /v0/entries/{id}`: retrieve current entry; 404 if nonexistent.
- `PATCH /v0/entries/{id}`: update only existing entry; 200 on successful meaningful modification; 404 if nonexistent; 409 for conflicting revision if optimistic concurrency is used; 422 on invalid patch.
- `DELETE /v0/entries/{id}`: logical deletion of existing entry; 404 if nonexistent. Define response behavior for already-deleted entries consistently.
- `GET /v0/health`: simple service health response.

Error example:
```json
{"error":{"code":"ENTRY_NOT_FOUND","message":"No entry exists with the specified ID."}}
```

Document deletion visibility: deleted entries are excluded from normal list/report responses, but preserved in operational state and journal. Whether direct GET of a deleted entry yields 404 or 410 is an explicit open choice; do not silently vary behavior.

Acceptance: generate OpenAPI; verify methods/status codes, UUID retries, non-upsert PATCH, error shapes, and that HTTP schemas do not expose file paths.

## 06. Completion and field discovery

- `GET /v0/fields`: discover metadata field names and basic types from operational entries.
- `GET /v0/completions?field=task&q=vac`: return matching prior values for a given field; support `task` and keys in `attributes`.

New values are learned automatically from successful submissions, with no registration step. Suggestions derive from the central operational store, optionally through a rebuildable index/cache. Define predictable case normalization, matching, sorting, and limits. Do not turn historical user input into a mandatory fixed taxonomy.

Acceptance: a newly submitted task/location appears in completion; no predefined task list is required; suggestions remain correct after updates/deletions and service restart.

## 07. Reporting API

Expose reports through `/v0`, initially using current operational entry state only. Minimum capability: filter by date range and arbitrary attributes, group by task or an attribute, aggregate total duration and entry count. Deleted entries are excluded by default. Date ranges use clearly documented inclusive/exclusive boundaries and UTC semantics.

Suggested endpoints:
- `GET /v0/reports`: discover report definitions.
- `GET /v0/reports/time?group_by=task&from=...&to=...`: basic aggregate report.
- `GET /v0/reports/{id}`: retrieve a saved definition when saved reports are implemented.

DuckDB is an optional implementation mechanism, not an API dependency. Reporting must not need journal replay on every query. A later extension may add historical/as-of reports from the journal.

Acceptance: totals agree with current entries, corrections change totals, deleted entries disappear, arbitrary metadata grouping works, empty result sets are well-defined.

## 08. Shared client capture and submission contract

Each client is independently configured with the central service base URL. Each widget instance permits **at most one running timer**, but may retain multiple in-progress task drafts. Starting a different task pauses the currently running task automatically; accumulated durations remain editable when paused. Stopping freezes the displayed duration field, ready for manual correction and submission. Manual retrospective entry is supported without running a timer.

Persist drafts locally across app/widget restarts. Store a running timer's start reference and accumulated duration so the UI can reconstruct elapsed time without depending on continuous UI updates. A completed entry is queued locally with a UUID until the central service acknowledges durable storage. Retry with the same UUID; do not duplicate entries. Once acknowledged, remove the pending local copy. Clients need not retain a permanent local history database.

Cross-device timer coordination is **out of scope for v0**. Multiple widgets/devices can run independently; the user manages that choice.

Acceptance: switching tasks pauses the previous timer; manual duration edits persist; restart preserves drafts; offline submissions queue; lost acknowledgments retry safely; successful acknowledgment removes queued data.

## 09. KDE Plasma client

Build a compact native KDE Plasma widget (QML/Plasmoid, with platform-appropriate HTTP integration) using the shared behavior contract in §08. UI includes task autocomplete, optional metadata fields, editable HH:MM:SS duration, Start/Pause, switching among drafts, and Submit. Include a setting for the service URL. Keep server storage and reporting logic out of the widget.

Acceptance: usable from the desktop panel or widget surface; no predefined tasks required; multiple drafts and single active timer; reliable queue behavior and API completion.

## 10. Android client

Build a native Android app and home-screen widget using Kotlin and Jetpack Glance or another documented native approach. Follow §08. Android widget constraints may require opening an activity for text entry or richer editing; document those UX choices rather than assuming unrestricted inline editing.

Acceptance: quick start/pause/switch, capture/manual submission, persistent drafts, local pending queue, configurable server URL, completion fetched from API.

## 11. Deployment, documentation, and integration

Support running the service on a LAN host or generic VPS, without cloud-specific services. Offer a simple systemd or Docker Compose deployment path. Provide configuration for bind address, port, and data directory through the server configuration described in section 00. Keep the data directory outside version control. No application-level security in v0; document that publicly exposing an unauthenticated write API is unsafe and leave security design for a later phase.

Documentation deliverables: README quickstart, architecture overview, API/OpenAPI contract, data format documentation, and short ADRs for append-only journal + mutable operational read model, client-generated UUIDs, and POST-only creation.

Integration acceptance: create, retry, patch, delete, autocomplete, report, restart/recover, and verify on-disk JSONL using a temporary data directory. Verify no API contract depends on storage file layout.

## Open decisions to resolve before or during the relevant section

1. Whether operational records remain in monthly JSONL files (the current proposal) or move to individual UUID files now that they are mutable.
2. Exact PATCH semantics for nested attributes and deletion of attribute keys (e.g. JSON Merge Patch vs JSON Patch vs explicit delta format).
3. Whether optimistic concurrency (`revision`/`If-Match`) is mandatory in v0.
4. How to identify identical POST retries after an entry has subsequently been edited (retain a canonical original-submission fingerprint or equivalent). (Resolved; see `server/README.md`.)
5. Whether GET of a logically deleted entry returns 404 or 410, and behavior for repeat DELETE.
6. Entry time semantics for manual records without known `started_at`, and reporting date filtering rules. (Partly resolved: storage placement uses the original `started_at`, else the commit month. Reporting date filtering rules remain open.)
7. Journal commit/recovery strategy for partial writes and a corrupted/non-final line. (Resolved; see the journal recovery policy in `server/README.md`.)
8. Whether saved reports are included in v0 or deferred.

Do not invent decisions silently. For implementation work, make the smallest reversible choice, document it, and request a decision if it affects public behavior or data durability.
