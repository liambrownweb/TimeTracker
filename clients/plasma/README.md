# Plasma 5 time-capture widget

This widget targets Plasma 5.27 and Qt 5.15 on Linux. The Qt/C++ capture
backend is separate from the Plasma-facing QML; it is not a Plasma 6 package.

## Build and test

On Ubuntu/Pop!_OS, the development dependencies are `cmake`, `qtbase5-dev`,
`qtdeclarative5-dev`, `qtdeclarative5-dev-tools`, `qml-module-qttest`, and
`plasma-sdk`. Build from this directory:

```sh
cmake -S . -B build
cmake --build build
ctest --test-dir build --output-on-failure
```

The tests use temporary storage, a controllable clock, and a simulated HTTP
server. An additional live-server test is skipped unless an isolated test
server is supplied. **It creates an entry on that server**:

```sh
TIMETRACKER_TEST_SERVER=http://127.0.0.1:8000 build/capture_tests liveServerIntegration
```

To try the built package without installing it:

```sh
plasmoidviewer --applet "$PWD/build/package"
```

Qt 5 logs a module-identifier warning when loading the bundled native plugin
through its package-relative QML import. Qt consequently cannot protect that
local module name from other registrations in the same process. The relative
import avoids installing a shared QML module system-wide or changing Plasma's
environment; this warning remains a known packaging limitation.

To install it for the current user, use `kpackagetool5 -t Plasma/Applet -i
"$PWD/build/package"`, then add **Time Tracker** through Plasma's widget
picker. For an already-installed copy use `-u` instead of `-i`. Rebuild before
upgrading. The package includes a compiled Qt plugin and must be built for
the target machine; do not install the unbuilt `package/` source directory.

If Plasma reports `plugin "timetrackerplugin" not found`, first verify that
`~/.local/share/plasma/plasmoids/org.timetracker.capture/contents/ui/backend/libtimetrackerplugin.so`
exists. If it is missing, rebuild and upgrade using `build/package`, not
`package/`. If it is present but a previous load failed, log out and back in
to restart Plasma's QML engine. Qt 5 can cache the failed lookup for the
life of the running shell even after the plugin is installed; removing and
re-adding the widget alone may not clear it. A native plugin upgrade can
likewise require a Plasma restart. `plasmoidviewer` runs in a separate process,
so successful loading there does not clear a cached failure in the desktop.

## Using the widget

Set the service base URL in the widget. Add a draft, enter a task and,
optionally, metadata as a JSON object such as `{"location":"home"}`. Task
suggestions come from the server; a new task needs no registration. Paused
durations can be edited in `HH:MM:SS`. A draft that has never been started
is a retrospective entry with no invented start timestamp.

Draft edits are saved after a short pause or when **Save**, **Start**, or
**Submit** is pressed. The widget shows **Unsaved edits** until a valid edit is
saved; invalid JSON or duration text must be corrected before saving. Selecting
another draft also saves valid changes first. Starting a draft pauses any other
running draft in that widget. Pause before editing or submitting.

Submission first moves the draft into a durable local queue. Pending requests
show their original destination and any error. Transient errors are retried
with bounded backoff while the widget is loaded; 409, 422, and other permanent
HTTP errors require action. **Retry now** explicitly retries the same request.
**Copy to draft** leaves the original request in the queue and creates an
editable draft; submitting that draft creates a new UUID and may create a
second entry. Discarding either a draft or pending request requires confirmation.
Changing the service URL never redirects existing pending requests.

State is stored per Plasma widget instance under
`$XDG_DATA_HOME/timetracker/plasma/<instance>/state.json` (normally
`~/.local/share/timetracker/plasma/<instance>/state.json`). The state file is
owner-readable and writable only. Keep this directory when migrating or
backing up a widget with drafts or pending requests; removing the widget or
package does not automatically delete the data. A second process cannot open
the same instance simultaneously. Storage errors are displayed and prevent
the affected action from being reported as saved.

Elapsed time uses Linux `CLOCK_BOOTTIME` within a boot, including suspend, and
a UTC wall-clock reference across reboots. A detectable wall-clock jump or
negative reconstructed interval pauses the timer and requires duration review.
A clock change while the machine is off may be impossible to detect. The
widget must be loaded to deliver queued requests; it does not install a
background service. The v0 server has no authentication: use a trusted network
and do not expose it directly to the public internet.
