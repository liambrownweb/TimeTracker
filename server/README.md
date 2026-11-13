# TimeTracker server

The server package targets Python 3.12 and uses FastAPI, Pydantic, and pytest.

## Entry model

Client-submitted entry values are represented separately from operational records
and HTTP response schemas. Operational history starts empty. On the first
meaningful update or deletion it records an initial snapshot, followed by the
event delta; subsequent changes append deltas only. Attribute history uses
separate `set` and `remove` operations, so assigning JSON `null` differs from
removing a key.

The public `PATCH` contract uses the same explicit attribute operations and maps
to a separate domain update model. `started_at: null` clears the known start
time; null is invalid for `task`, `duration_seconds`, and `attributes`. Revision
is exposed as audit/restore metadata and is not used for optimistic concurrency.
Public responses omit operational history, journal sequence, and schema version.

Journal event models have an independent schema version and immutable,
discriminated create/update/delete variants. Create events carry the initial
client-submitted entry; update events carry the delta; delete events carry the
entry identity and deletion timestamp.

The v0 API contract for logically deleted entries is direct `GET` returning
`410 Gone` and repeated `DELETE` returning `204 No Content`.
## Operational storage

`JsonlEntryStore` (`timetracker.storage`) implements the `EntryStore` protocol (`get`, `list`, `create`, `update`).

- **Layout:** `<data_dir>/entries/YYYY/MM.jsonl`, one complete `EntryRecord` per line, including its embedded history. The month is the UTC month of the entry's original `started_at`, or of `created_at` (the create's commit time) when no start time was supplied; see `storage_month` in the domain module. Entries are filed by when the activity happened, not when the server heard about it. An entry never moves, even when `started_at` is later edited or cleared (the original value is read from the initial history snapshot), or when it is deleted. An update that would change the placement is rejected.
- **Reads** are served from an in-memory index rebuilt from these files at startup. Neither reads nor rebuilds touch the journal.
- **Writes** replace the whole month file: write a temporary file in the same directory, fsync it, `os.replace` it over the original, then fsync the directories. A failed write leaves the original file and in-memory state untouched, and stale temporary files are removed at startup.
- **Updates and deletion** both go through `update(record)` with the record produced by `apply_entry_update` / `delete_entry`. There is no separate `delete`; logical deletion is just a record with `deleted=true`.
- **Corruption** (malformed line, duplicate id, record in the wrong month file) raises `StorageCorruptionError` naming the file and line. Nothing is silently skipped.
- **`list`** excludes deleted entries unless `include_deleted=True`, orders by `(started_at or created_at, id)`, and filters by exact `task` and exact attribute values (`true` is not `1`, `1` is not `1.0`). Date filtering is deferred until the entry-time semantics are decided.
- A single process is assumed to own the data directory; a lock serializes access within it.

Attribute values are compared type-strictly everywhere (domain no-op detection and storage filters): `true`, `1` and `1.0` are all different values, so changing between them is a real change.

## Journal recovery policy (spec decision #7)

The journal is the recovery authority; operational records are the read model. On-demand verification is read-only. Startup recovery may replace flagged operational records without preserving the old month file: the working copy is derived from the journal. Torn journal tails and unreadable operational trees are still quarantined.

**Journal integrity** (checked first):
- Every event carries a checksum over its canonical content, so silent bit rot is detected even when the line still parses as JSON.
- A torn final line in the newest file is an unacknowledged write. A write is acknowledged only after its line, including the terminating newline, is fsynced, so a final line that lacks its newline is torn however much of it parses. It is copied to `quarantine/`, truncated, and its sequence number is reused. A newline-terminated line that fails parsing or its checksum is never treated as torn; it is corruption and halts startup.
- Any other fault (a bad non-final line, a sequence gap, a duplicate `event_id` or `sequence`, an event outside its commit month) halts startup. The server reports file, line and reason, and is repaired only by explicit manual action. Events are never skipped.
- Sequence numbers must be consecutive from 1 across month files.

**Cross-check, per entry:**
- Every journal `entry_id` has a record and every record has a create event.
- `last_event_sequence` equals the entry's latest journal sequence.
- `revision` equals the entry's event count.
- The embedded history's event IDs equal the entry's journal event IDs.

**Deep comparison:** replay the journal in memory and compare each record field by field. When it runs is configurable (`deep_check` setting):
- `startup`: at every startup (the default).
- `interval`: at startup only if at least `deep_check_days` days have passed, or at least `deep_check_events` journal events have been committed, since the last completed deep check. Either threshold may be unset, and the check runs when any set threshold is met.
- `manual`: never automatically; run only when requested with the `verify --deep` command (for example from cron).

Every automatic verification step can be disabled in the configuration, and all are on by default:
- `verify_journal_on_startup`: full-journal checksum, sequence continuity, duplicate and month-placement checks.
- `verify_cross_check_on_startup`: the per-entry journal-to-record 1:1 cross-check.
- `deep_check`: as above.

Torn-tail handling and the check that the journal tail matches the records' `last_event_sequence` are cheap and always run. A disabled check still runs on demand with `verify`, and `verify --deep` runs everything. "Events" in `deep_check_events` means committed journal events, not distinct entries touched.

The time and journal sequence of the last completed deep check are kept in a small state file, so interval counting survives restarts. A deep check that is interrupted does not count as completed. A failed check always halts the server and reports the mismatch, whichever mode started it.

**Repair:**
- Events missing at an entry's tail are applied in order, idempotently by `event_id` and `sequence`.
- An event missing from the middle of an entry's history means the entry is rebuilt from all of its journal events and its month file is rewritten. A missed event is never applied on top of later state.
- Unreadable operational files, or a record with no create event, cause the whole `entries/` tree to be moved to `quarantine/entries-<time>-<id>/` and regenerated from the journal. (Rewriting only the damaged file is a possible later optimization; a full rebuild is correct in every case and is rare.) After any repair a full deep comparison must come back clean, otherwise startup fails.

**Detection:** startup verification, an on-demand `verify` command, and a runtime guard in the coordinator. If a record's `last_event_sequence` is not the expected predecessor, the request returns 503 and the store is flagged for repair.

## Journal storage

`JsonlJournal` (`timetracker.storage`) implements `JournalStore` (`last_sequence`, `append`, `read`).

- **Layout:** `<data_dir>/journal/YYYY/MM.jsonl`, selected by the UTC commit time (`recorded_at`). Appends only; no line is ever rewritten.
- **Line format:** one event as canonical JSON (sorted keys, compact) plus a `checksum` field, `sha256:` over the canonical JSON of the event without that field. The checksum is computed from the parsed event, so it does not depend on whitespace.
- **Append:** requires `sequence == last_sequence + 1`, an unused `event_id`, and a commit month no earlier than the latest. It returns only after the line is fsynced (new files and directories are fsynced first). If a write or fsync fails, the partial bytes are truncated away and the event is not acknowledged. If the rollback itself fails, the journal refuses further appends until restart.
- **Open:** scans the whole journal, enforcing the integrity rules of the recovery policy above. Configurable verification toggles arrive with the configuration loader.
- **Replay:** `timetracker.domain.replay.replay` rebuilds records from events using the same transitions as live updates. An event that cannot apply cleanly (missing or deleted target, double create) is a `ReplayError`. `replay` returns a `ReplayResult` with the rebuilt `records` and `skipped_no_ops`. An update event is skipped only when an independent check proves it changes nothing: the delta is compared type-strictly against current state without using the live no-op detector, so a flaw there cannot make replay drop a real change. A skipped event is reported for verification, since the live path never journals one. Every other event that does not change state is a `ReplayError`. The per-entry cross-check in the recovery policy must apply the same classification.

## Identical POST retries (spec decision #4)

A retried `POST /v0/entries` for an existing UUID is compared with the entry's original create event in the journal, never with the entry's current (possibly edited) state, and a retry never writes anything.

- **Match:** the server answers 200 with the entry's current state. The client treats this as its acknowledgment.
- **Mismatch:** the server answers 409, since the UUID exists with different content.
- **Identity** is the normalized submission: `id`, `task`, `duration_seconds`, `started_at` and `attributes`, compared with the same type-strict equality used elsewhere (`1`, `1.0` and `true` are different; key order is irrelevant). Server-managed fields (timestamps, revision, history) are not compared.
- **Deleted entries:** a retry of a since-deleted entry still matches its create event and gets 200.
- **Lookup:** the coordinator keeps an in-memory index from entry id to its create event, rebuilt from the journal at startup, so a retry never scans the journal. No fingerprint is stored on the operational record.

## Transaction coordinator

`timetracker.service.EntryCoordinator` is the only write path. API handlers will call it; they never touch the stores.

- **Order:** one lock; validate against current state; compute the meaningful delta (`apply_entry_update` as a dry run); append the journal event (fsynced); build the record with `apply_journal_event`, the same function replay uses, so live and replayed state cannot diverge; persist the record. A request that changes nothing writes nothing.
- **Errors:** `EntryNotFoundError` (404), `EntryDeletedError` (410, for GET and PATCH on a deleted entry), `EntryConflictError` (409), `StoreUnavailableError` (503). Repeated DELETE returns normally (204).
- **POST retries:** compared with the original create event, as in decision #4, via an in-memory index built at startup.
- **After a journal commit the mutation is committed.** If saving the record then fails, the coordinator raises `StoreUnavailableError`, flags the store, and repairs it from the journal (cross-check plus targeted repair) before serving the next request. A retried POST then returns the committed entry; a retried PATCH recomputes its delta against the repaired state, so an idempotent set becomes a no-op. Nothing is duplicated. A restart repairs the same way.
- **Runtime guard:** each entry's `last_event_sequence` must equal the sequence the journal says it should have. A mismatch, or a disagreement about whether an entry exists, returns 503 once, flags the store, and the next request repairs it.
- **Clock:** commit times never go backwards. If the system clock steps back (including across a month boundary) the event is stamped with the previous event's time, so the journal's month ordering cannot be violated.
- **Startup:** `open_entry_store` quarantines unreadable operational files and starts empty; `EntryCoordinator` then runs `verify_and_repair` with `VerificationOptions(cross_check, deep_check)`. The journal is always replayed in memory (the coordinator needs it); the options control whether records are compared. The cheap tail check always runs and escalates to the cross-check on a mismatch, including a store that is ahead of the journal. The cross-check and deep comparison classify provable no-op events exactly as `replay` does; they are reported in `skipped_no_ops`, never treated as damage.
- **Configured startup:** use `timetracker.verification.open_server(settings)` as a context manager. It holds an exclusive data-directory lock, opens the journal and coordinator, reconciles detected recovery gaps, and performs a scheduled deep comparison before yielding. The HTTP server and integration with its lifecycle are still future work.

## Configuration and verification (§04.5)

Run `timetracker init-config` to create the default configuration explicitly. The loader also creates it on first non-systemd use, without overwriting an existing file. If `CONFIGURATION_DIRECTORY` is set and the file is absent, it logs a warning and uses defaults instead. `--config PATH` selects a file, and `TIMETRACKER_CONFIG` does so when no CLI path is given.

Configuration is TOML. `bind_address` defaults to `127.0.0.1`, `port` to `8000`, and `data_dir` to `$STATE_DIRECTORY` if supplied or `$XDG_DATA_HOME/timetracker` (default `~/.local/share/timetracker`). Settings take precedence in this order: command-line options, `TIMETRACKER_<SETTING>` environment variables, file, built-in defaults. Unknown file keys and invalid values are errors. For example:

```toml
verify_journal_on_startup = true
verify_cross_check_on_startup = true
deep_check = "interval"
deep_check_days = 7
deep_check_events = 1000
```

`deep_check` is `startup` (default), `interval`, or `manual`. Interval mode requires at least one positive integer threshold; either threshold makes a check due. The first interval startup checks immediately. A clean completed deep check atomically records its UTC time and latest journal sequence in `verification-state.json` under the data directory. Invalid state, or a state sequence ahead of the journal, stops startup. Disabling checks reduces protection: even when journal integrity verification is disabled, events must still parse and replay; a scheduled deep check always forces full journal integrity verification.

Run `timetracker verify` for full journal integrity and the record cross-check, or `timetracker verify --deep` to additionally compare every field. Both return nonzero on errors or mismatches and never repair entry or journal data; a clean deep check updates the schedule state. A torn journal tail is reported rather than repaired by this command. Verification and a managed server cannot run concurrently against the same directory: the command fails if the directory is locked. Other programs that bypass the managed lock must not access the directory concurrently.

The current coordinator still replays the journal at startup, even when deep comparison is disabled.

## HTTP API (§05)

Run `timetracker serve` to start the server; `--config`, `--data-dir`, `--bind-address`, `--port`, and the verification options override the file and environment. The default bind address is localhost. The server is unauthenticated and **must not be exposed directly to the public internet**. Interactive OpenAPI documentation is available at `/docs` and the schema at `/openapi.json`.

- `POST /v0/entries` creates with a client UUID (201 and `Location`); an identical retry returns the current entry (200), including a deleted one. A different original submission using that UUID returns 409.
- `GET /v0/entries` returns an unpaginated JSON array of live entries in activity-time order (`started_at` or `created_at`, then UUID). `task` is an optional exact match. `attributes` is an optional JSON object encoded as a query parameter, for example `?attributes=%7B%22billable%22%3Atrue%7D`. Each supplied attribute must match exactly, with JSON types distinguished. The filters combine with AND.
- `GET` and `PATCH /v0/entries/{id}` return 404 for unknown IDs and 410 for deleted entries. PATCH uses the explicit `attributes.set`/`attributes.remove` contract; an empty or unchanged patch is a successful no-op. `DELETE` returns 204 for both a new and repeated logical deletion, or 404 for an unknown ID.
- `GET /v0/health` returns `{"status":"ok"}` while the application is serving.

Errors use `{"error":{"code":"...","message":"..."}}`; validation errors return 422 and storage unavailability returns 503. Errors do not expose storage paths. Storage exceptions are logged server-side. The managed server holds the data-directory lock throughout its lifetime; stop it before using `verify`.
