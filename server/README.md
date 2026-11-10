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
