# ophix-task-systemd

systemd timer Tier 2 client for [Ophix Project](https://ophixproject.com) task scheduling.

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

## Commands

### `sync`

Fetch tasks and apply as systemd unit files.

```bash
task-systemd sync
task-systemd sync --schedule server-maintenance
task-systemd sync --user www-data --unit-dir /etc/systemd/system
```

| Argument | Default | Description |
| --- | --- | --- |
| `--schedule` | (all) | Only fetch tasks from this named Schedule |
| `--user` | `root` | Unix user to run tasks as |
| `--unit-dir` | `/etc/systemd/system` | Directory to write unit files |

Requires write permission to the unit directory and the ability to run `systemctl`. Run as root or via sudo.

### `show`

Print the unit files that would be written, without writing them.

```bash
task-systemd show
task-systemd show --schedule server-maintenance --user www-data
```

### `clear`

Stop, disable, and remove all ophix-managed units.

```bash
task-systemd clear
task-systemd clear --unit-dir /etc/systemd/system
```

---

## Automating the Sync

Add the sync call as a root cron entry or a separate systemd timer outside the managed set:

```text
# /etc/cron.d/ophix-tasks-sync
*/15 * * * * root /opt/venv/bin/task-systemd sync --schedule server-maintenance
```
