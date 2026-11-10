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
`410 Gone` and repeated `DELETE` returning `204 No Content`. These HTTP statuses
will be enforced when the routes are implemented.
## Operational storage

`JsonlEntryStore` (`timetracker.storage`) implements the `EntryStore` protocol (`get`, `list`, `create`, `update`).

- **Layout:** `<data_dir>/entries/YYYY/MM.jsonl`, one complete `EntryRecord` per line, including its embedded history. The month comes from the UTC `created_at` and an entry never moves, even when it is later updated or deleted.
- **Reads** are served from an in-memory index rebuilt from these files at startup. Neither reads nor rebuilds touch the journal.
- **Writes** replace the whole month file: write a temporary file in the same directory, fsync it, `os.replace` it over the original, then fsync the directories. A failed write leaves the original file and in-memory state untouched, and stale temporary files are removed at startup.
- **Updates and deletion** both go through `update(record)` with the record produced by `apply_entry_update` / `delete_entry`. There is no separate `delete`; logical deletion is just a record with `deleted=true`.
- **Corruption** (malformed line, duplicate id, record in the wrong month file) raises `StorageCorruptionError` naming the file and line. Nothing is silently skipped.
- **`list`** excludes deleted entries unless `include_deleted=True`, orders by `(created_at, id)`, and filters by exact `task` and exact attribute values (`true` is not `1`, `1` is not `1.0`). Date filtering is deferred until the entry-time semantics are decided.
- A single process is assumed to own the data directory; a lock serializes access within it.

Attribute values are compared type-strictly everywhere (domain no-op detection and storage filters): `true`, `1` and `1.0` are all different values, so changing between them is a real change.

## Journal recovery policy (spec decision #7)

The journal is the recovery authority; operational records are the read model. Verification is read-only and runs before any repair. Repair writes only what verification flagged, and replaced or truncated bytes are copied to a `quarantine/` folder first. Nothing is deleted.

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
- A corrupt or mismatching operational file is moved to `*.corrupt-<time>` and regenerated from the journal. If the damage is not confined to one file, the whole store is rebuilt into a temporary directory, verified, and swapped in.

**Detection:** startup verification, an on-demand `verify` command, and a runtime guard in the coordinator. If a record's `last_event_sequence` is not the expected predecessor, the request returns 503 and the store is flagged for repair.

## Journal storage

`JsonlJournal` (`timetracker.storage`) implements `JournalStore` (`last_sequence`, `append`, `read`).

- **Layout:** `<data_dir>/journal/YYYY/MM.jsonl`, selected by the UTC commit time (`recorded_at`). Appends only; no line is ever rewritten.
- **Line format:** one event as canonical JSON (sorted keys, compact) plus a `checksum` field, `sha256:` over the canonical JSON of the event without that field. The checksum is computed from the parsed event, so it does not depend on whitespace.
- **Append:** requires `sequence == last_sequence + 1`, an unused `event_id`, and a commit month no earlier than the latest. It returns only after the line is fsynced (new files and directories are fsynced first). If a write or fsync fails, the partial bytes are truncated away and the event is not acknowledged. If the rollback itself fails, the journal refuses further appends until restart.
- **Open:** scans the whole journal, enforcing the integrity rules of the recovery policy above. Configurable verification toggles arrive with the configuration loader.
- **Replay:** `timetracker.domain.replay.replay` rebuilds records from events using the same transitions as live updates. An event that cannot apply cleanly (missing or deleted target, double create, no-op) is a `ReplayError`.
