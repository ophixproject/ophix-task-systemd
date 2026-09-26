"""
ophix_task_systemd.cli
~~~~~~~~~~~~~~~~~~~~~~
Command-line interface for the ophix-task-systemd Tier 2 client.

Entry point: task-systemd (registered in pyproject.toml).
"""

import os
import sys
import types

import requests

from client_core.parser import make_main
from ophix_task_systemd._version import __version__
from ophix_task_systemd.core import (
    DEFAULT_SYNC_INTERVAL,
    DEFAULT_UNIT_DIR,
    DEFAULT_USER,
    SCOPE_SYSTEM,
    SCOPE_USER,
    clear_units,
    default_unit_dir,
    discover_foreign_timers,
    install_sync_timer,
    parse_foreign_unit,
    reload_daemon,
    remove_foreign_unit,
    show_units,
    sync_units,
)
from task_client.core import create_task, get_tasks


def _detect_scope():
    # type: () -> str
    """Return SCOPE_SYSTEM if running as root (or on Windows), SCOPE_USER otherwise."""
    try:
        return SCOPE_SYSTEM if os.getuid() == 0 else SCOPE_USER
    except AttributeError:
        return SCOPE_SYSTEM  # Windows has no getuid; assume system


def _resolve_scope(args):
    # type: (types.SimpleNamespace) -> str
    scope = getattr(args, "scope", "")
    if scope in (SCOPE_SYSTEM, SCOPE_USER):
        return scope
    return _detect_scope()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _print_sync_summary(summary):
    print("Written: {}  Removed: {}  Skipped: {}".format(
        summary["written"], summary["removed"], len(summary["skipped"])
    ))
    for msg in summary["skipped"]:
        print("  skip: {}".format(msg))
    for msg in summary["errors"]:
        print("  error: {}".format(msg))


def cmd_sync(args):
    try:
        tasks = get_tasks(schedule=args.schedule or None, scheduler="systemd")
    except Exception as exc:
        print("Failed to fetch tasks: {}".format(exc))
        sys.exit(1)

    scope = _resolve_scope(args)
    unit_dir = args.unit_dir or default_unit_dir(scope)

    try:
        summary = sync_units(tasks, unit_dir=unit_dir, user=args.user, scope=scope)
    except PermissionError:
        print("Permission denied writing to {}. Run as root or use sudo.".format(unit_dir))
        sys.exit(1)
    except Exception as exc:
        print("Failed to sync units: {}".format(exc))
        sys.exit(1)

    _print_sync_summary(summary)
    if summary["errors"]:
        sys.exit(1)


def cmd_show(args):
    try:
        tasks = get_tasks(schedule=args.schedule or None, scheduler="systemd")
    except Exception as exc:
        print("Failed to fetch tasks: {}".format(exc))
        sys.exit(1)

    print(show_units(tasks, user=args.user, scope=_resolve_scope(args)), end="")


def cmd_clear(args):
    scope = _resolve_scope(args)
    unit_dir = args.unit_dir or default_unit_dir(scope)

    try:
        count = clear_units(unit_dir=unit_dir, scope=scope, include_bootstrap=args.with_bootstrap)
    except PermissionError:
        print("Permission denied. Run as root or use sudo.")
        sys.exit(1)
    except Exception as exc:
        print("Failed to clear units: {}".format(exc))
        sys.exit(1)

    if count:
        print("Removed {} ophix-managed unit(s) from {}.".format(count, unit_dir))
    else:
        print("No ophix-managed units found in {}.".format(unit_dir))


def cmd_install(args):
    try:
        tasks = get_tasks(schedule=args.schedule or None, scheduler="systemd")
    except Exception as exc:
        print("Failed to fetch tasks: {}".format(exc))
        sys.exit(1)

    scope = _resolve_scope(args)
    unit_dir = args.unit_dir or default_unit_dir(scope)

    try:
        stem = install_sync_timer(
            schedule=args.schedule or None, interval=args.interval,
            unit_dir=unit_dir, user=args.user, scope=scope,
        )
    except PermissionError:
        print("Permission denied writing to {}. Run as root or use sudo.".format(unit_dir))
        sys.exit(1)
    except Exception as exc:
        print("Failed to install sync timer: {}".format(exc))
        sys.exit(1)

    print("Installed bootstrapping timer {}.timer (re-syncs every {}).".format(stem, args.interval))

    try:
        summary = sync_units(tasks, unit_dir=unit_dir, user=args.user, scope=scope)
    except PermissionError:
        print("Permission denied writing to {}. Run as root or use sudo.".format(unit_dir))
        sys.exit(1)
    except Exception as exc:
        print("Failed to sync units: {}".format(exc))
        sys.exit(1)

    _print_sync_summary(summary)
    if summary["errors"]:
        sys.exit(1)


def cmd_import(args):
    scope = _resolve_scope(args)
    unit_dir = args.unit_dir or default_unit_dir(scope)

    stems = discover_foreign_timers(unit_dir)
    if not stems:
        print("No foreign (non-ophix) .timer units found in {}.".format(unit_dir))
        return

    print("Found {} foreign timer unit(s) in {}. Importing into schedule '{}'...\n".format(
        len(stems), unit_dir, args.schedule))

    created = 0
    already_existed = 0
    unparseable = 0
    errors = 0
    removed = 0

    for stem in stems:
        parsed = parse_foreign_unit(unit_dir, stem)
        if parsed["skip_reason"]:
            unparseable += 1
            print("  skip     {}: {}".format(stem, parsed["skip_reason"]))
            continue

        try:
            result = create_task(
                schedule=args.schedule,
                scheduler="systemd",
                name=parsed["name"],
                command=parsed["command"],
                description=parsed["description"],
                interval=parsed["interval"],
            )
        except requests.exceptions.HTTPError as e:
            errors += 1
            detail = ""
            if e.response is not None:
                try:
                    detail = " — {}".format(e.response.json())
                except Exception:
                    detail = " — {}".format(e.response.text[:200])
            print("  error    {}: {}{}".format(stem, e, detail))
            continue
        except Exception as e:
            errors += 1
            print("  error    {}: {}".format(stem, e))
            continue

        task_status = result.get("status")
        task_id = result.get("id")
        note = ""
        if parsed["user"] not in ("", DEFAULT_USER):
            note = " (originally ran as: {} — pass --user {} on install/sync if this schedule should run as that account)".format(
                parsed["user"], parsed["user"])

        if task_status == "created":
            created += 1
            print("  created  #{}: {} ({}){}".format(task_id, parsed["name"], parsed["command"][:60], note))
        elif task_status == "skipped":
            already_existed += 1
            print("  skipped  #{} (command already exists): {}{}".format(task_id, parsed["command"][:60], note))

        # The task exists server-side now (created this run, or already did)
        # — safe to remove the original if asked. Never delete on an error,
        # and never delete before the replacement is confirmed to exist.
        if args.delete_originals and task_status in ("created", "skipped"):
            try:
                remove_foreign_unit(unit_dir, parsed["timer_stem"], parsed["service_stem"], scope=scope)
                removed += 1
                print("  removed  {}.timer / {}.service".format(parsed["timer_stem"], parsed["service_stem"]))
            except Exception as e:
                print("  error    removing {}: {}".format(stem, e))

    if args.delete_originals and removed:
        reload_daemon(scope=scope)

    print("\nImport complete: {} created, {} already existed, {} unparseable, {} errors{}.".format(
        created, already_existed, unparseable, errors,
        ", {} original unit(s) removed".format(removed) if args.delete_originals else "",
    ))
    if not args.delete_originals and (created or already_existed):
        print(
            "Verify server-side, then re-run with --delete-originals to remove "
            "the originals (safe to re-run: already-imported units are "
            "skipped as duplicates, not re-created)."
        )


# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------

_SCOPE_HELP = (
    "Unit scope: 'system' (/etc/systemd/system, systemctl, User= directive) or "
    "'user' (~/.config/systemd/user, systemctl --user, --user arg ignored). "
    "Default: auto-detect from effective UID (root -> system, non-root -> user)."
)

COMMANDS = {
    "sync": {
        "help": "Fetch tasks from the server and write systemd unit files.",
        "arguments": [
            {"name": "--schedule", "default": "",
             "help": "Only fetch tasks from this named schedule (default: all)"},
            {"name": "--user", "default": DEFAULT_USER,
             "help": "Unix user to run tasks as in system scope (default: {}, ignored in user scope)".format(DEFAULT_USER)},
            {"name": "--unit-dir", "dest": "unit_dir", "default": None,
             "help": "Directory to write unit files (default: {} for system scope, ~/.config/systemd/user for user scope)".format(DEFAULT_UNIT_DIR)},
            {"name": "--scope", "choices": [SCOPE_SYSTEM, SCOPE_USER], "default": "",
             "help": _SCOPE_HELP},
        ],
        "handler": cmd_sync,
    },

    "show": {
        "help": "Print unit files that would be written, without writing them.",
        "arguments": [
            {"name": "--schedule", "default": "",
             "help": "Only fetch tasks from this named schedule (default: all)"},
            {"name": "--user", "default": DEFAULT_USER,
             "help": "Unix user to run tasks as in system scope (default: {}, ignored in user scope)".format(DEFAULT_USER)},
            {"name": "--scope", "choices": [SCOPE_SYSTEM, SCOPE_USER], "default": "",
             "help": _SCOPE_HELP},
        ],
        "handler": cmd_show,
    },

    "clear": {
        "help": "Stop, disable, and remove ophix-managed task units.",
        "arguments": [
            {"name": "--unit-dir", "dest": "unit_dir", "default": None,
             "help": "Directory to remove units from (default: {} for system scope, ~/.config/systemd/user for user scope)".format(DEFAULT_UNIT_DIR)},
            {"name": "--scope", "choices": [SCOPE_SYSTEM, SCOPE_USER], "default": "",
             "help": _SCOPE_HELP},
            {"name": "--with-bootstrap", "dest": "with_bootstrap", "action": "store_true",
             "help": "Also remove the bootstrapping sync timer installed by 'install' (default: left in place)"},
        ],
        "handler": cmd_clear,
    },

    "install": {
        "help": "Install a self-perpetuating timer that keeps re-running sync, then sync immediately.",
        "arguments": [
            {"name": "--schedule", "default": "",
             "help": "Only fetch tasks from this named schedule (default: all)"},
            {"name": "--interval", "default": DEFAULT_SYNC_INTERVAL,
             "help": "systemd time span for OnUnitActiveSec= on the bootstrapping timer (default: {})".format(DEFAULT_SYNC_INTERVAL)},
            {"name": "--user", "default": DEFAULT_USER,
             "help": "Unix user to run tasks as in system scope (default: {}, ignored in user scope)".format(DEFAULT_USER)},
            {"name": "--unit-dir", "dest": "unit_dir", "default": None,
             "help": "Directory to write unit files (default: {} for system scope, ~/.config/systemd/user for user scope)".format(DEFAULT_UNIT_DIR)},
            {"name": "--scope", "choices": [SCOPE_SYSTEM, SCOPE_USER], "default": "",
             "help": _SCOPE_HELP},
        ],
        "handler": cmd_install,
    },

    "import": {
        "help": "Translate existing, non-ophix .timer units into tasks on the server.",
        "arguments": [
            {"name": "--schedule", "required": True,
             "help": "Schedule name to import tasks into"},
            {"name": "--unit-dir", "dest": "unit_dir", "default": None,
             "help": "Directory to scan for foreign units (default: {} for system scope, ~/.config/systemd/user for user scope)".format(DEFAULT_UNIT_DIR)},
            {"name": "--scope", "choices": [SCOPE_SYSTEM, SCOPE_USER], "default": "",
             "help": _SCOPE_HELP},
            {"name": "--delete-originals", "dest": "delete_originals", "action": "store_true",
             "help": "Remove each original unit once its replacement task is confirmed to "
                     "exist server-side (default: leave originals untouched — parse and "
                     "create tasks only, safe to re-run)"},
        ],
        "handler": cmd_import,
    },
}

_CONFIG = types.SimpleNamespace(
    prog="task-systemd",
    description="Apply ophix-tasks schedules as systemd timer units.",
    version=__version__,
)

main = make_main(_CONFIG, COMMANDS)


if __name__ == "__main__":
    main()
