"""
ophix_task_systemd.cli
~~~~~~~~~~~~~~~~~~~~~~
Command-line interface for the ophix-task-systemd Tier 2 client.

Entry point: task-systemd (registered in pyproject.toml).

Commands:
    sync  — fetch tasks from the server and apply as systemd unit files
    show  — print the unit files that would be written, without writing
    clear — stop, disable, and remove all ophix-managed units
"""

import argparse
import sys

from ophix_task_systemd._version import __version__
from ophix_task_systemd.core import (
    DEFAULT_UNIT_DIR,
    DEFAULT_USER,
    clear_units,
    show_units,
    sync_units,
)

from task_client.core import get_tasks


def cmd_sync(args):
    try:
        tasks = get_tasks(schedule=args.schedule or None)
    except Exception as exc:
        print("Failed to fetch tasks: {}".format(exc))
        sys.exit(1)

    try:
        summary = sync_units(tasks, unit_dir=args.unit_dir, user=args.user)
    except PermissionError:
        print("Permission denied writing to {}. Run as root or use sudo.".format(args.unit_dir))
        sys.exit(1)
    except Exception as exc:
        print("Failed to sync units: {}".format(exc))
        sys.exit(1)

    print("Written: {}  Removed: {}  Skipped: {}".format(
        summary["written"], summary["removed"], len(summary["skipped"])
    ))
    for msg in summary["skipped"]:
        print("  skip: {}".format(msg))
    for msg in summary["errors"]:
        print("  error: {}".format(msg))
    if summary["errors"]:
        sys.exit(1)


def cmd_show(args):
    try:
        tasks = get_tasks(schedule=args.schedule or None)
    except Exception as exc:
        print("Failed to fetch tasks: {}".format(exc))
        sys.exit(1)

    print(show_units(tasks, user=args.user), end="")


def cmd_clear(args):
    try:
        count = clear_units(unit_dir=args.unit_dir)
    except PermissionError:
        print("Permission denied. Run as root or use sudo.")
        sys.exit(1)
    except Exception as exc:
        print("Failed to clear units: {}".format(exc))
        sys.exit(1)

    if count:
        print("Removed {} ophix-managed unit(s) from {}.".format(count, args.unit_dir))
    else:
        print("No ophix-managed units found in {}.".format(args.unit_dir))


def build_parser():
    # type: () -> argparse.ArgumentParser
    parser = argparse.ArgumentParser(
        prog="task-systemd",
        description="Apply ophix-tasks schedules as systemd timer units.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="task-systemd {}".format(__version__),
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # sync
    p = sub.add_parser("sync", help="Fetch tasks and write systemd unit files.")
    p.add_argument("--schedule", default="", help="Only fetch tasks from this named schedule (default: all)")
    p.add_argument(
        "--user",
        default=DEFAULT_USER,
        help="Unix user to run tasks as (default: {})".format(DEFAULT_USER),
    )
    p.add_argument(
        "--unit-dir",
        default=DEFAULT_UNIT_DIR,
        help="Directory to write unit files (default: {})".format(DEFAULT_UNIT_DIR),
    )

    # show
    p = sub.add_parser("show", help="Print unit files that would be written, without writing.")
    p.add_argument("--schedule", default="", help="Only fetch tasks from this named schedule (default: all)")
    p.add_argument(
        "--user",
        default=DEFAULT_USER,
        help="Unix user to run tasks as (default: {})".format(DEFAULT_USER),
    )

    # clear
    p = sub.add_parser("clear", help="Stop, disable, and remove all ophix-managed units.")
    p.add_argument(
        "--unit-dir",
        default=DEFAULT_UNIT_DIR,
        help="Directory to remove units from (default: {})".format(DEFAULT_UNIT_DIR),
    )

    return parser


COMMANDS = {
    "sync": cmd_sync,
    "show": cmd_show,
    "clear": cmd_clear,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)
    handler = COMMANDS.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
