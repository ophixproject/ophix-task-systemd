"""
ophix_task_systemd.core
~~~~~~~~~~~~~~~~~~~~~~~
systemd timer unit generator for ophix-tasks.

Each Ophix task becomes a pair of unit files in the target directory:

    ophix-<name>.timer   — the schedule trigger
    ophix-<name>.service — the command to run

Units are identified as Ophix-managed by the ophix- prefix on the filename.
On every sync the full set is reconciled: new units written, changed units
updated, tasks no longer in the server response cleaned up.

Interval format
---------------
Tasks fetched by this client are pre-filtered by ?scheduler=systemd, so the
interval field already contains a systemd OnCalendar= expression. It is used
verbatim — no conversion is performed. The server validates the format at
creation time.

Disabled tasks
--------------
Tasks with enabled=False have any existing units stopped and removed.

Output handling
---------------
  inherit → no StandardOutput/StandardError directive (systemd journals by default)
  null    → StandardOutput=null / StandardError=null
  file    → StandardOutput=append:<path> / StandardError=append:<path>
  merge   → StandardError=inherit (routes stderr to the same place as stdout)
  report  → shell pipe to 'task-client report <id>' in ExecStart
"""

import os
import re
import subprocess
from datetime import datetime, timezone as dt_timezone
from typing import Dict, List, Optional, Set, Tuple

MANAGED_PREFIX = "ophix-"
DEFAULT_UNIT_DIR = "/etc/systemd/system"
DEFAULT_USER = "root"


# ---------------------------------------------------------------------------
# Unit naming
# ---------------------------------------------------------------------------

def _sanitize_name(name):
    # type: (str) -> str
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-")


def stem_for_task(task):
    # type: (Dict) -> str
    """Return the unit stem (without extension) for a task."""
    return MANAGED_PREFIX + _sanitize_name(task.get("name", "unnamed"))


# ---------------------------------------------------------------------------
# Calendar conversion
# ---------------------------------------------------------------------------

def run_at_to_calendar(run_at_str):
    # type: (str) -> str
    """Convert an ISO datetime string to a systemd OnCalendar= UTC spec."""
    if run_at_str.endswith("Z"):
        run_at_str = run_at_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(run_at_str)
    if dt.tzinfo is not None:
        dt = dt.astimezone(dt_timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def resolve_calendar(task):
    # type: (Dict) -> Tuple[Optional[str], Optional[str]]
    """
    Determine the OnCalendar= value for a task.

    Returns (calendar_spec, None) on success or (None, reason) on skip.
    """
    run_at = task.get("run_at")
    interval = (task.get("interval") or "").strip()

    if run_at:
        return run_at_to_calendar(run_at), None

    if not interval:
        return None, "no run_at or interval set"

    return interval, None


# ---------------------------------------------------------------------------
# ExecStart and output directives
# ---------------------------------------------------------------------------

def _sq_escape(s):
    # type: (str) -> str
    """Escape a string for embedding in a POSIX single-quoted shell argument."""
    return s.replace("'", "'\"'\"'")


def _build_exec_start(task):
    # type: (Dict) -> str
    """
    Build the ExecStart= value.

    For 'report' stdout/stderr handling a shell pipe to task-client is
    required. All other output routing is handled via systemd directives.
    """
    command = (task.get("command") or "").strip()
    task_id = task.get("id")
    stdout = task.get("stdout_handling", "inherit")
    stderr = task.get("stderr_handling", "inherit")

    if stdout == "report" and task_id is not None:
        if stderr in ("report", "merge"):
            shell_cmd = "{} 2>&1 | task-client report {}".format(command, task_id)
        else:
            shell_cmd = "{} | task-client report {}".format(command, task_id)
    elif stderr == "report" and task_id is not None:
        shell_cmd = "{} 2>&1 1>/dev/null | task-client report {}".format(command, task_id)
    else:
        shell_cmd = command

    return "/bin/bash -c '{}'".format(_sq_escape(shell_cmd))


def _output_directives(task):
    # type: (Dict) -> List[str]
    """Return StandardOutput=/StandardError= directives for non-report handling."""
    stdout = task.get("stdout_handling", "inherit")
    stderr = task.get("stderr_handling", "inherit")
    log_file = (task.get("log_file") or "").strip()
    directives = []  # type: List[str]

    if stdout == "null":
        directives.append("StandardOutput=null")
    elif stdout == "file" and log_file:
        directives.append("StandardOutput=append:{}".format(log_file))

    if stderr == "null":
        directives.append("StandardError=null")
    elif stderr == "file" and log_file:
        directives.append("StandardError=append:{}".format(log_file))
    elif stderr == "merge" and stdout != "report":
        directives.append("StandardError=inherit")

    return directives


# ---------------------------------------------------------------------------
# Unit file content
# ---------------------------------------------------------------------------

def timer_unit(task, calendar_spec):
    # type: (Dict, str) -> str
    name = task.get("name", "unnamed")
    description = (task.get("description") or "").strip() or name
    # One-off timers must not re-fire if missed; recurring ones should.
    persistent = "false" if task.get("run_at") else "true"

    return "\n".join([
        "[Unit]",
        "Description=Ophix: {}".format(description),
        "",
        "[Timer]",
        "OnCalendar={}".format(calendar_spec),
        "Persistent={}".format(persistent),
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])


def service_unit(task, user):
    # type: (Dict, str) -> str
    name = task.get("name", "unnamed")
    description = (task.get("description") or "").strip() or name
    exec_start = _build_exec_start(task)
    directives = _output_directives(task)

    lines = [
        "[Unit]",
        "Description=Ophix: {}".format(description),
        "",
        "[Service]",
        "Type=oneshot",
        "User={}".format(user),
        "ExecStart={}".format(exec_start),
    ]
    lines.extend(directives)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Managed unit discovery
# ---------------------------------------------------------------------------

def existing_stems(unit_dir):
    # type: (str) -> Set[str]
    """Return stems of all ophix-managed timer units in unit_dir."""
    stems = set()  # type: Set[str]
    try:
        for fname in os.listdir(unit_dir):
            if fname.startswith(MANAGED_PREFIX) and fname.endswith(".timer"):
                stems.add(fname[:-len(".timer")])
    except OSError:
        pass
    return stems


# ---------------------------------------------------------------------------
# systemctl helpers
# ---------------------------------------------------------------------------

def _systemctl(*args):
    # type: (*str) -> None
    subprocess.run(["systemctl"] + list(args), check=False)


def _remove_stem(unit_dir, stem):
    # type: (str, str) -> None
    _systemctl("stop", stem + ".timer")
    _systemctl("disable", stem + ".timer")
    for ext in (".timer", ".service"):
        path = os.path.join(unit_dir, stem + ext)
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_units(tasks, unit_dir=DEFAULT_UNIT_DIR, user=DEFAULT_USER):
    # type: (List[Dict], str, str) -> Dict
    """
    Reconcile systemd units against the task list.

    Returns a summary dict with keys: written, skipped, removed, errors.
    """
    before = existing_stems(unit_dir)
    active = set()  # type: Set[str]
    summary = {
        "written": 0,
        "skipped": [],  # type: List[str]
        "removed": 0,
        "errors": [],   # type: List[str]
    }

    for task in tasks:
        name = task.get("name", "unnamed")
        stem = stem_for_task(task)

        if not task.get("enabled", True):
            if stem in before:
                try:
                    _remove_stem(unit_dir, stem)
                    summary["removed"] += 1
                except OSError as exc:
                    summary["errors"].append("{}: {}".format(name, exc))
            summary["skipped"].append("{} (disabled)".format(name))
            continue

        calendar, skip_reason = resolve_calendar(task)
        if skip_reason:
            summary["skipped"].append("{}: {}".format(name, skip_reason))
            continue

        try:
            timer_path = os.path.join(unit_dir, stem + ".timer")
            service_path = os.path.join(unit_dir, stem + ".service")
            with open(timer_path, "w", encoding="utf-8") as f:
                f.write(timer_unit(task, calendar))
            with open(service_path, "w", encoding="utf-8") as f:
                f.write(service_unit(task, user))
            active.add(stem)
            summary["written"] += 1
        except OSError as exc:
            summary["errors"].append("{}: {}".format(name, exc))

    # Remove units for tasks no longer returned by the server
    for stem in before - active:
        try:
            _remove_stem(unit_dir, stem)
            summary["removed"] += 1
        except OSError as exc:
            summary["errors"].append("remove {}: {}".format(stem, exc))

    if active or (before - active):
        _systemctl("daemon-reload")
        for stem in active:
            _systemctl("enable", "--now", stem + ".timer")

    return summary


# ---------------------------------------------------------------------------
# Show (dry run)
# ---------------------------------------------------------------------------

def show_units(tasks, user=DEFAULT_USER):
    # type: (List[Dict], str) -> str
    """Return a text preview of unit files that would be written."""
    lines = []  # type: List[str]
    for task in tasks:
        name = task.get("name", "unnamed")
        stem = stem_for_task(task)

        if not task.get("enabled", True):
            lines.append("# SKIP (disabled): {}".format(name))
            continue

        calendar, skip_reason = resolve_calendar(task)
        if skip_reason:
            lines.append("# SKIP {}: {}".format(name, skip_reason))
            continue

        lines.append("### {}.timer".format(stem))
        lines.append(timer_unit(task, calendar))
        lines.append("### {}.service".format(stem))
        lines.append(service_unit(task, user))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------

def clear_units(unit_dir=DEFAULT_UNIT_DIR):
    # type: (str) -> int
    """Stop, disable, and remove all ophix-managed units. Returns count removed."""
    stems = existing_stems(unit_dir)
    for stem in stems:
        _remove_stem(unit_dir, stem)
    if stems:
        _systemctl("daemon-reload")
    return len(stems)
