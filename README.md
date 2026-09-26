# ophix-task-systemd

systemd timer Tier 2 client for [Ophix Project](https://ophix.io) task scheduling.

Fetches the task list from an Ophix task server via `ophix-task-client` and writes managed systemd timer and service unit files. The full set of units is reconciled on every sync.

---

## Installation

```bash
pip install ophix-task-systemd
```

`ophix-task-client` is a required dependency and is installed automatically. Bootstrap with `task-client quickstart` before using task-systemd.

---

## How It Works

Each task becomes a pair of unit files in `/etc/systemd/system/`:

```
ophix-nightly-backup.timer
ophix-nightly-backup.service
```

Units are identified as Ophix-managed by the `ophix-` prefix. On every sync:

- New tasks → units written, timer enabled and started
- Changed tasks → units overwritten, reloaded
- Removed or disabled tasks → timer stopped and disabled, units removed

Example unit files for a task named `nightly-backup`:

```ini
# ophix-nightly-backup.timer
[Unit]
Description=Ophix: Nightly backup script

[Timer]
OnCalendar=*-*-* 02:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# ophix-nightly-backup.service
[Unit]
Description=Ophix: Nightly backup script

[Service]
Type=oneshot
User=root
ExecStart=/bin/bash -c '/opt/backup.sh | task-client report 1'
```

---

## Interval Format

The `interval` field on each task must contain a **systemd OnCalendar= expression**. Cron expressions (5-field format) are detected and skipped with a warning — use `ophix-task-crontab` for those hosts.

| Example interval | Format | Result |
| --- | --- | --- |
| `*-*-* 02:00:00` | systemd | `OnCalendar=*-*-* 02:00:00` |
| `daily` | systemd | `OnCalendar=daily` |
| `Mon *-*-* 08:00:00` | systemd | `OnCalendar=Mon *-*-* 08:00:00` |
| `0 2 * * *` | cron | skipped with warning |

One-off tasks (`run_at`) are converted to a pinned `OnCalendar=` spec with `Persistent=false` so they do not re-fire if the system was offline when they were due.

---

## Output Handling

| `stdout_handling` / `stderr_handling` | systemd behaviour |
| --- | --- |
| `inherit` | journald (systemd default) |
| `null` | `StandardOutput=null` / `StandardError=null` |
| `file` | `StandardOutput=append:<log_file>` |
| `merge` | `StandardError=inherit` (stderr → same as stdout) |
| `report` | pipe to `task-client report <id>` in `ExecStart` |

---

## Scope: system vs user units

Like `task-crontab`'s `crond`/`user` format duality, `task-systemd` supports two unit scopes, auto-detected from the effective UID (override with `--scope`):

| Scope | Auto-selected when | Unit dir | systemctl | `User=` directive |
| --- | --- | --- | --- | --- |
| `system` | running as root | `/etc/systemd/system` | `systemctl ...` | written, from `--user` |
| `user` | running as non-root | `~/.config/systemd/user` | `systemctl --user ...` | omitted — unit already runs as the invoking account |

In `user` scope, `--user` is ignored — there's nothing to set it to, since a `--user` unit already runs as whichever account owns the session. `task-systemd` must be **invoked as the target account** (an actual login, or a cron/su invocation running as that user); there's no mechanism here to manage a different user's session bus remotely.

**Lingering.** If the target account is a service account with no persistent login session (no one is ever interactively logged in as it), run `loginctl enable-linger <user>` once. Without it, systemd tears down the account's `--user` manager instance when its last session ends, and its timers silently stop firing until it logs in again — a common source of "it worked when I tested it, then stopped."

```bash
task-systemd sync --scope user                    # explicit, current user's session
task-systemd sync --scope system --user www-data   # explicit, system-wide as www-data
```

---

## Commands

### `sync`

Fetch tasks and apply as systemd unit files.

```bash
task-systemd sync
task-systemd sync --schedule server-maintenance
task-systemd sync --user www-data --unit-dir /etc/systemd/system
task-systemd sync --scope user
```

| Argument | Default | Description |
| --- | --- | --- |
| `--schedule` | (all) | Only fetch tasks from this named Schedule |
| `--user` | `root` | Unix user to run tasks as (system scope only; ignored in user scope) |
| `--unit-dir` | auto | Directory to write unit files (default: `/etc/systemd/system` in system scope, `~/.config/systemd/user` in user scope) |
| `--scope` | auto | `system` or `user`. Default: auto-detect from effective UID. |

Requires write permission to the unit directory and the ability to run `systemctl` (or `systemctl --user`).

### `show`

Print the unit files that would be written, without writing them.

```bash
task-systemd show
task-systemd show --schedule server-maintenance --user www-data
task-systemd show --scope user
```

### `clear`

Stop, disable, and remove ophix-managed task units. Leaves the `install`-created bootstrapping timer in place unless `--with-bootstrap` is given.

```bash
task-systemd clear
task-systemd clear --unit-dir /etc/systemd/system
task-systemd clear --scope user
task-systemd clear --with-bootstrap   # also remove the bootstrapping timer
```

### `install`

Install a self-perpetuating timer that keeps re-running `sync` on its own, then run an immediate sync. This is the systemd equivalent of `task-crontab install`.

```bash
task-systemd install --schedule server-maintenance
task-systemd install --schedule pypiserver-tasks --user pypiserver --interval 10min
task-systemd install --scope user
```

| Argument | Default | Description |
| --- | --- | --- |
| `--schedule` | (all) | Only fetch tasks from this named Schedule |
| `--interval` | `15min` | systemd time span for `OnUnitActiveSec=` on the bootstrapping timer |
| `--user` | `root` | Unix user to run tasks as (system scope only; ignored in user scope) |
| `--unit-dir` | auto | Directory to write unit files |
| `--scope` | auto | `system` or `user`. Default: auto-detect from effective UID. |

Writes a reserved timer/service pair — `ophix-tasks-sync.timer` (or `ophix-tasks-sync-<schedule>.timer` when `--schedule` is given, so multiple schedules can each have their own bootstrap timer without colliding). Fires `OnBootSec=5min` after boot, then every `--interval` after that. The generated `ExecStart` re-embeds `--schedule`, `--user`, `--unit-dir`, and `--scope` explicitly, so every periodic re-sync is a faithful reproduction of this `install` call — same as `task-crontab install`'s bootstrap line.

**Unlike a task unit, this stem is never touched by `sync`'s reconciliation** — `sync` diffs against the *task* set returned by the server, and the bootstrap timer isn't a server task, it's the thing that keeps calling `sync`. Removing it is a deliberate act: `task-systemd clear --with-bootstrap`, or manually:

```bash
systemctl disable --now ophix-tasks-sync.timer
rm /etc/systemd/system/ophix-tasks-sync.{timer,service}
systemctl daemon-reload
```

Re-running `install` for the same schedule overwrites the same two unit files rather than adding a second timer — idempotent by construction, since it's just two fixed file paths.

> **Reserved name.** Task names must not be `tasks-sync` or `tasks-sync-<anything>` — those stems are reserved for the bootstrap timer and will never be picked up by the sync reconciliation loop.

### `import`

Scan the target scope for existing `.timer` units that **don't** start with `ophix-` and translate them into tasks on the server. This is the systemd equivalent of `task-crontab import`, adapted for the fact that a systemd job is two correlated files rather than one text line.

```bash
task-systemd import --schedule server-maintenance
task-systemd import --schedule server-maintenance --delete-originals
task-systemd import --schedule server-maintenance --scope user
```

| Argument | Default | Description |
| --- | --- | --- |
| `--schedule` | *(required)* | Schedule name to import tasks into |
| `--unit-dir` | auto | Directory to scan for foreign units |
| `--scope` | auto | `system` or `user`. Default: auto-detect from effective UID. |
| `--delete-originals` | off | Remove each original unit once its replacement task is confirmed to exist server-side |

For each foreign `.timer`, `import` resolves the `.service` it triggers (same basename by default, or whatever `Unit=` names explicitly), and reads `OnCalendar=` + `ExecStart=` (+ `Description=`, `User=`) to build a task. It never guesses on anything ambiguous — a unit is **skipped with a reason**, not imported, when it has:

- more than one `OnCalendar=` line (not representable as a single interval),
- a monotonic-only timer (`OnBootSec=`/`OnUnitActiveSec=` with no `OnCalendar=` at all — `task-systemd` only supports calendar-scheduled tasks),
- more than one `ExecStart=` line (not representable as a single command), or
- a `Service` `Type=` other than `oneshot` (a long-running `Type=simple` unit woken by a timer isn't a "run and exit" task).

`ExecStart=` prefix modifiers (`-`, `+`, `!`, `!!`) and `%`-specifiers (`%n`, `%i`, etc.) are copied through **verbatim, not interpreted** — check the printed command before trusting it, especially before running with `--delete-originals`.

Task creation is idempotent (duplicate command on the same schedule → `skipped`, not a second row), and `import` without `--delete-originals` never touches the filesystem at all — so the intended workflow is exactly the two-step pattern you'd expect:

1. `task-systemd import --schedule X` — creates tasks, originals untouched. Safe to run repeatedly.
2. Check the server (`Schedule` / `ScheduledTask` admin, or `show`/`sync --schedule X` locally) — confirm the translated tasks look right.
3. `task-systemd import --schedule X --delete-originals` — re-parses the same units; anything already imported comes back `skipped` (still counts as "exists"), so the now-redundant original is removed. Units that failed to parse the first time are still left alone.

`--delete-originals` only ever removes a unit **after** its replacement is confirmed to exist server-side (this run or a prior one) — never on an error, and never speculatively ahead of the create call. If the server call itself fails, that unit's original is left in place and reported under "errors."

**One thing `import` can't fully carry over: per-job users.** `sync`/`install` apply a single `--user` to every task written in that call — there's no per-task user field on the server. If an original unit's `Service` section had `User=someuser`, `import` prints a note calling that out, but doesn't act on it; if you need different tasks running as different accounts, split them into separate Schedules the way the crontab README's "Option A" pattern already does, and pass the matching `--user` on each `install`/`sync`.

---

## Automating the Sync

`install` (above) is the recommended way to keep units in sync going forward. The manual alternative — useful if you'd rather drive it from cron or an existing timer outside `task-systemd`'s own management — is unchanged:

```text
# /etc/cron.d/ophix-tasks-sync
*/15 * * * * root /opt/venv/bin/task-systemd sync --schedule server-maintenance
```
