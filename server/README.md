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