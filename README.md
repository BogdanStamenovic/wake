# wake

Schedule and fire wake-up tasks across a server/device pair. One machine —
usually the always-on one — runs `wake serve` and holds the authoritative
task database. Any other machine installs `wake` in device mode: local bots
and agents write tasks with `wake add`, and the device syncs them up to the
server automatically.

Each task carries its own **backend**, dispatched at fire time: run a shell
command, send a Wake-on-LAN packet, arm the local machine's hardware RTC
alarm, leave a note in [hotline-ios](https://github.com/BogdanStamenovic/hotline-ios),
or ring the phone for real.

Requires Python 3.11+. No third-party dependencies.

## Install

### ownbox

```
ownbox install wake
```

This runs `deploy/install.sh`, so it is a complete install rather than just a
CLI: it asks whether the machine is the **server** or a **device**, asks a
device for the server's address and the shared key, builds the venv, writes
`~/.config/wake/wake.env` at mode 0600, and enables and starts the systemd user
unit for that role.

It can ask because Ownbox runs setup commands with its own stdio inherited, so
the script gets the terminal you typed the command into. Where there is no
terminal — a script, a cron job, a CI runner — nothing blocks: each prompt
falls through to its documented default and says which it took and why. That
matters more than it sounds, because Ownbox's command timeout is 1800 seconds,
so a prompt with nobody to answer it would hang for half an hour before failing.

To answer in advance and skip the prompts entirely:

| Variable | Values | Default |
|---|---|---|
| `WAKE_INSTALL_ROLE` | `server`, `device` | `device` |
| `WAKE_INSTALL_UNIT` | `server`, `agent`, `timer`, `none` | follows the role |
| `WAKE_INSTALL_SERVER_URL` | host, `host:port` or a full URL | blank |
| `WAKE_INSTALL_API_KEY` | the shared secret | blank |
| `WAKE_INSTALL_NO_PROMPT` | `1` to never prompt, terminal or not | `0` |
| `WAKE_INSTALL_TAKEOVER` | `1` to repoint another checkout's unit | `0` |

`ownbox uninstall wake` runs `deploy/uninstall.sh`, which stops, disables and
deletes the units it installed and removes the venv. It does **not** pass
`--purge`: the task database is your data, not a build artifact, so it is
listed as left behind rather than deleted.

### Manual

```
git clone https://github.com/BogdanStamenovic/wake
cd wake
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

### As a service

`deploy/install.sh` is the same script Ownbox drives, and takes flags for
everything it would otherwise ask:

```
deploy/install.sh                            # asks role, then server, then key
deploy/install.sh --role server              # wake-server.service
deploy/install.sh --role device --server-url 100.72.2.62
deploy/install.sh --role device --unit timer # wake-sync.timer instead
deploy/install.sh --role device --unit none  # no unit; run it by hand
deploy/install.sh --no-prompt                # defaults, never asks
```

A bare host is normalised: `100.72.2.62` becomes `http://100.72.2.62:8788`. The
address is then probed with an unauthenticated `GET /health`, and an
unreachable server is **reported, not enforced** — a device is legitimately
installed before its server exists, and `wake agent` treats an unreachable
server as retryable rather than fatal, so refusing here would be stricter than
the thing being installed.

The unit is enabled *and started*. A start that fails is reported and does not
fail the install, for the same reason: a device whose server is not up yet is a
normal state.

Re-running is safe. An existing `wake.env` is never rewritten, and its `ROLE`
is what the install follows — so a second run on a deployed machine does not
quietly change what that machine is, and does not re-ask questions it can read
the answer to.

#### Two checkouts on one machine

Ownbox keeps its own checkout under `~/.local/share/ownbox/tools/wake`, which
may sit beside a working clone somewhere else. They share one systemd user
manager and one config file, so both scripts check ownership by `ExecStart`:

- **install** refuses to overwrite a unit whose `ExecStart` points into a
  different checkout, because doing so would silently move a running daemon —
  and its scheduled power tasks — onto a checkout nobody meant to deploy.
  `--takeover` does it on purpose.
- **uninstall** only removes units whose `ExecStart` is under the repository it
  is run from, prints what it skipped, and takes `--all` to override.

The unit files carry a `@WAKE_ROOT@` placeholder that `install.sh` substitutes
with the checkout it is installing from, so **do not copy them into place by
hand**. They previously hardcoded `%h/data/wake`, which was correct for one
clone on one machine and produced a bare `203/EXEC` anywhere else.

`deploy/uninstall.sh` also runs `systemctl --user reset-failed` and clears any
timer stamp files, because neither is removed by disabling and deleting a
unit — a unit that had failed keeps a `not-found failed` runtime entry, and a
stamp file outlives its timer entirely.

`wake-sync.timer` is the alternative to `wake-agent.service` for a laptop:
each run is a fresh short-lived process, so nothing holds a socket or a
database handle across a suspend and there is no daemon to wedge. It only
syncs — a device that must also fire its own tasks on time wants the agent,
since a two-minute timer cannot honour an alarm to the second.

## Usage

```
wake add --at <when> --task <cmd> [--backend shell|wol|rtcwake|notify|call]
                                 [--target T] [--on HOST] [--id ID]
wake list [--all] [--json]
wake cancel <id>
wake sync
wake agent [--once]
wake serve [--port 8788] [--bind 0.0.0.0]
wake fire <id>
```

| Option | Meaning |
| --- | --- |
| `--at` | Epoch seconds, ISO 8601, or a relative offset like `+30m`, `+2h`, `+1d` |
| `--task` | For `shell`/`rtcwake`: a command; for `wol`: unused (see `--target`); for `notify`/`call`: the text |
| `--backend` | `shell` (default), `wol`, `rtcwake`, `notify`, or `call` |
| `--target` | Backend-specific: a MAC for `wol`, a hotline agent name for `notify`/`call` |
| `--on` | Which machine fires it, by `ORIGIN` name. Default: the server |
| `--id` | Explicit task id instead of a generated one. Re-adding an id that exists **re-arms** it — see below |

Every subcommand also accepts `-v/--verbose`, `-q/--quiet`, `--version`, and
`--config <path>` (a `KEY=value` env file — see Configuration below).

stdout is kept parseable: `add` prints the task id and nothing else, `list
--json` prints the JSON array and nothing else. Progress, warnings and errors
all go to stderr. Exit codes are 0 for success, 1 for a failed operation, 2 for
a usage or configuration error.

### Examples

```
# Run a shell command in 30 minutes, on the server
wake add --at +30m --task "systemctl restart myservice"

# ... and the same command, but on this laptop
wake add --at +30m --task "systemctl --user restart myservice" --on laptop

# Wake a sleeping desktop by MAC address at 07:00 tomorrow
wake add --at 2026-09-04T07:00:00 --backend wol --target a8:a1:59:fd:4d:13 --task wol

# Arm this laptop's own RTC alarm to resume from suspend in 6 hours
wake add --at +6h --backend rtcwake --task rtcwake

# Leave a note in a hotline channel when it fires
wake add --at +1h --backend notify --target hotline-80 --task "check the deploy"

# Actually ring the phone at 07:00 — an alarm clock
wake add --at 2026-09-04T07:00:00 --backend call --task "time to get up"

wake list --json
wake cancel 3f9a2c1b
```

## Backends

- **`shell`** — runs `task` as `sh -c <task>` on whichever machine owns it,
  with a five-minute timeout.
- **`wol`** — sends a Wake-on-LAN magic packet to `--target`. The packet is
  built and sent in-process, so there is no dependency on the `wakeonlan`
  binary, which is not installed everywhere and would be a system-wide package
  for 102 bytes. `--target` takes a MAC in any usual spelling, optionally
  suffixed with `@<address>` to reach another subnet by directed broadcast
  (`a8:a1:59:fd:4d:13@10.0.0.255`).
- **`rtcwake`** — **armed locally at `add` time**, not fired later by the
  server. A suspended machine cannot run a scheduler loop to wake itself, so
  `wake add --backend rtcwake` calls `rtcwake -m no -t <epoch>` immediately on
  whichever machine you ran it on, and the task is stored as `armed` so no
  scheduler anywhere mistakes it for work. Requires `rtcwake` and root on that
  machine.
- **`notify`** — POSTs to hotline-ios's `/api/v1/say`, leaving a note in a
  channel. `--target` becomes the `agent` field. Does not ring anything.
- **`call`** — POSTs to hotline-ios's `/api/v1/call` with `wait: false`, which
  rings the phone and returns immediately. The `wait` matters: that endpoint
  otherwise blocks for up to fifteen minutes waiting for a typed reply, which
  is right for `hotline-call` and would be fatal here — the scheduler would sit
  on one alarm while every later task went unfired.

## Server vs. device

A **server** install runs `wake serve`: the HTTP API, plus a loop that fires
every due task it owns. It is the single source of truth, and `wake sync` on a
server is a no-op.

A **device** install works against its own local copy of the database and
reconciles with the server:

- `wake add` / `wake cancel` best-effort sync immediately after writing. A
  device that is offline warns on stderr and still exits 0 — the row is queued,
  not lost.
- `wake sync` does one push-then-pull pass.
- `wake agent` is the daemon form: it syncs every `SYNC_SECONDS` and fires this
  machine's own tasks every `POLL_SECONDS`. Two periods rather than one because
  firing wants to be prompt and reconciling does not want to hammer the server.

### Who fires a task

One rule: **a task is fired by the machine named in `--on`, and by the server
if `--on` was not given.** That is the entire scheduling protocol. No leases,
no claims, nothing to time out — two machines cannot both pick up the same row
because they are querying for different owners. `wake add --on laptop` runs on
the laptop even though the row lives on the server too; `wake add` with no
`--on` runs on the server even though the laptop has a copy.

### Re-arming a recurring timer

`wake` has no recurrence syntax, so a caller that wants one owns it: it re-adds
the next occurrence itself after observing a task fire. Passing the same
`--id` every time is what makes that safe. Re-adding an existing id points that
row at the new time, resets it to `pending`, and clears any previous `fired_at`
or `error` — one row, re-armed, rather than a second row racing the first. An
interrupted run therefore cannot leave two timers for the same thing.

This is deliberately *not* the same code path as sync's conflict resolution,
though the two look alike. `merge` settles a disagreement between two peers
that each wrote, and may discard the incoming row for being older. A re-add is
an instruction — "the timer is now at T" — and is never discarded. Routing it
through last-write-wins would mean two adds landing on the same float timestamp
silently drop the second, leaving the timer at its old time with nothing on
stderr to say so.

The same rule applies over HTTP, keyed on whether the body carries
`updated_at`: a sync push sends a full task and so has one, and merges; a thin
`{task, at, id}` from a Shortcut or a bot has none, and re-arms.

A task whose own command re-arms it — the normal shape for a recurring job,
whose command is something like `track run abc` — gets one more guarantee. The
command rewrites the row while wake is still holding it open, so wake's
post-run bookkeeping is a compare-and-set against the revision it read before
starting the command, and gives way if the row moved. Without that, `fired`
lands over the `pending` the re-arm just wrote: only `status` and `fired_at`
are touched, so the task keeps its correct future `at` and looks perfectly
healthy while never firing again. The log says `re-armed itself; leaving it
scheduled` when this happens. `wake cancel` is never conditional — an
operator cancelling beats whatever the task just did to itself.

### How sync works

Every write bumps a monotonic revision counter and stamps the row with it, so
"everything after revision N" is an exact, gap-free query with no clock
involved — the same shape hotline-ios's event feed uses its global `seq` for.

The two directions use different cursors, on purpose:

- **Pull** uses a single watermark into the server's revision sequence. That is
  correct because the sequence belongs to the server and the device only reads
  it.
- **Push** uses a per-row marker (`pushed_rev`), not a watermark. The local
  revision sequence is written by *both* halves of sync — a pull merges rows
  and those writes take fresh local revisions — so any single watermark over it
  is wrong in one of two ways. Advance it past the merged rows and the device
  re-pushes everything the server just sent it, forever. Advance it only to
  where the push got to and a task created while the pull was running is
  skipped and never sent at all. Per row, the invariant is just `pushed_rev <
  rev` means "the server has not seen this content", which survives any
  interleaving. Both failure modes have a test.

Conflicting edits to the same task id — rare, since normally only the owning
side edits a task — are resolved last-write-wins by `updated_at`, once, inside
`WakeDB.merge`. Nothing else in the system compares timestamps.

## Configuration

Read from `~/.config/wake/wake.env` (or `--config <path>` / `$WAKE_CONFIG`),
then overridden by `WAKE_*` environment variables. `KEY=value` lines, `#`
comments, quoting optional — same format as `profiler`'s env file.
`deploy/wake.env.example` is a commented starting point.

| Key | Meaning | Default |
| --- | --- | --- |
| `ROLE` | `device` or `server` | `device` |
| `DB_PATH` | Where the SQLite database lives | `~/.local/state/wake/wake.db` |
| `ORIGIN` | This machine's name, recorded on every task it creates | `hostname` |
| `SERVER_URL` | The server's base URL, e.g. `http://100.x.x.x:8788` | (unset) |
| `API_KEY` | Shared secret for `X-Wake-Key`, checked by the server | (unset — no auth) |
| `BIND` | Server listen address | `0.0.0.0` |
| `PORT` | Server listen port | `8788` |
| `POLL_SECONDS` | How often a firing loop looks for due tasks | `5` |
| `SYNC_SECONDS` | How often `wake agent` reconciles with the server | `60` |
| `HOTLINE_IOS_URL` | Base URL for the `notify` and `call` backends | (unset) |
| `HOTLINE_IOS_KEY` | `X-Hotline-Key` for those backends | (unset) |
| `WOL_BROADCAST` | Default broadcast address for `wol` | `255.255.255.255` |

## HTTP API

All POST, all JSON, all gated by `X-Wake-Key` when `API_KEY` is set.

| Route | Body | Returns |
| --- | --- | --- |
| `GET /health` | — | `{ok, revision, role}` |
| `POST /api/v1/tasks` | a task (`task` and `at` required) | the stored task |
| `POST /api/v1/tasks/list` | `{since}` | `{tasks, revision}` |
| `POST /api/v1/tasks/cancel` | `{id}` | the cancelled task |

Posting a task with an `id` that already exists merges rather than duplicates,
so a caller can safely re-send.

## Integrating with hotline-ios

`wake` does not modify hotline-ios; it is a client of the API hotline-ios
already has. Two independent directions:

- **wake → phone**: the `notify` and `call` backends POST to hotline-ios's
  existing `/api/v1/say` and `/api/v1/call`. Both carry a `client_token` of
  `wake:<task id>`, so a retried task lands as the same note rather than two.
- **phone → wake**: anything on the tailnet can schedule a task by POSTing to
  `/api/v1/tasks` — an iOS Shortcut, a hotline agent, a cron job. No `wake`
  install is needed on the caller's side, and no new endpoint on hotline-ios's.

## Development

```
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src/wake tests
shellcheck deploy/*.sh scripts/*.sh
```

The shell scripts are checked at shellcheck's default severity, and the test
suite runs it too when it is installed. `--enable=all` adds nothing here: its
whole output is SC2310/SC2312 complaining that predicate functions appear in
`if` conditions, which is what they are for.

`scripts/mutate.sh` is a mutation audit: it flips one constant or comparison
at a time and reports whether the suite noticed. It exists because a test that
compares a value against the constant that produced it passes whatever that
constant becomes — `assert config.port == DEFAULT_PORT` is happy with any
port. Seven of wake's checks were that shape until it said so, among them the
default bind address, both loop periods, and the last-write-wins tie-break.
All seventeen mutations are caught now; run it after changing a tuning value
or a comparison.

Two of them came from a different question, worth asking separately: *which
docstrings explain why something matters, and assert nothing?* Both of this
module's sync docstrings turned out to be that — the argument for pushing
before pulling, and the claim that a push dying halfway resumes rather than
restarts. Reversing the order passed the entire suite. A docstring is where
the thing you understood too well to test tends to end up.

It clears `__pycache__` between runs, and that is load-bearing: Python keys
bytecode on mtime-seconds plus size, so flipping a one-character constant back
and re-running within the same second can execute the *old* code and report a
failure against source that is byte-identical to git.

The sync tests run a real `WakeServer` on a loopback port and the backend tests
open real sockets, rather than stubbing `urllib`. The thing most likely to be
wrong in this codebase is the wire contract between two halves that were
written together, and a stub is exactly the layer that would agree with itself
while the real pair disagreed.

## Limitations

- `rtcwake` tasks are armed once, immediately, on whichever machine ran `wake
  add`. There is no remote arming, and no re-arming if the machine reboots
  before the alarm fires.
- Sync is last-write-wins by timestamp with no merge UI — fine for
  single-owner task rows, not built for two devices racing to edit the same
  task id.
- The HTTP API has no TLS; it is meant for a private tailnet, gated by the
  optional `API_KEY` header check. `wake serve` warns if it is listening on a
  non-loopback address with no key set.
- Devices poll. The server has no way to push a new task down to a device, so a
  task added on the server for a device fires no earlier than that device's
  next sync.
- `wake` is one-shot per task row: there is no recurrence syntax. A caller that
  wants a recurring wakeup re-adds the next occurrence itself after observing a
  task fire.
- The deploy scripts are Linux-only: every unit is a systemd **user** unit
  and there are no launchd equivalents, so `ownbox.yaml` lists `linux`
  alone. On macOS the package still imports and the CLI still runs, but
  nothing installs it as a service.
- `install.sh` writes the unit under `$HOME` while `systemctl --user` talks
  to a manager that fixed its own home at login. Where those disagree —
  `sudo -u`, a container on the host bus, a redirected `$HOME` — the
  install refuses to enable anything rather than act on the real user's
  daemon. Use `--unit none` there.
- Nothing prunes the task table. Fired and cancelled rows accumulate; `list`
  hides them, but they are still there and still travel on a first sync.
