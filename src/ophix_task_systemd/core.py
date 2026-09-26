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

Scope
-----
System scope (default when run as root): units go in /etc/systemd/system,
systemctl is called without --user, and each service unit carries a
User=<name> directive so systemd runs the command as that account.

User scope (default when run as non-root): units go in
~/.config/systemd/user (created if missing), systemctl is called with
--user, and no User= directive is written — the unit already runs as
whichever account invoked task-systemd. --user is ignored in this scope.
task-systemd must be invoked AS the target account (direct login, or a
cron/su invocation running as that user) — there is no remote-management
path for another user's session bus.

User scope requires the account to be "lingering"
(loginctl enable-linger <user>) if it is a service account with no
persistent login session. Without it, systemd stops the user's --user
manager instance when their last session ends, and its timers stop
firing until they log in again.

Install (bootstrap sync timer)
-------------------------------
install_sync_timer() writes a *reserved*, non-task timer/service pair
(stem "ophix-tasks-sync[-<schedule>]") whose only job is to periodically
re-run `task-systemd sync` with the exact flags it was installed with.
Unlike a real task unit, this stem is never subject to the sync/clear
reconciliation loop (see _is_bootstrap_stem) — it isn't a server task, it's
the mechanism that keeps the real task units in sync. `clear` leaves it in
place unless include_bootstrap=True is passed explicitly.

Output handling
---------------
  inherit → no StandardOutput/StandardError directive (systemd journals by default)
  null    → StandardOutput=null / StandardError=null
  file    → StandardOutput=append:<path> / StandardError=append:<path>
  merge   → StandardError=inherit (routes stderr to the same place as stdout)
  report  → shell pipe to 'task-client report <id>' in ExecStart

Import (bringing existing, non-ophix units under management)
---------------------------------------------------------------
discover_foreign_timers() finds .timer files in the target scope that do
NOT start with ophix- — i.e. units nobody here created. parse_foreign_unit()
best-effort translates one into a task definition (name, command, interval,
description). It never guesses on ambiguity: a unit is skipped with a
reason, not imported, when it has more than one OnCalendar= or ExecStart=
line, a monotonic-only timer (OnBootSec=/OnUnitActiveSec= with no
OnCalendar=), or a Service Type= other than oneshot. ExecStart= prefix
modifiers (-, +, !, !!) and % specifiers are copied through verbatim, not
interpreted — review before relying on --delete-originals for those.

Translation only creates the task server-side; it never touches the
filesystem by itself. Deleting the original unit (remove_foreign_unit) is
a separate, explicit act gated by the caller (cli.py's --delete-originals),
and only ever happens once the replacement task is confirmed to exist
server-side (created this run, or already present from an earlier run) —
matching task-crontab's import/install split, just collapsed into one
command since systemd has no single shared file to defer the cleanup to.
Because task creation is idempotent (duplicate command -> "skipped", not
a second row) and deletion is opt-in, re-running import first without
--delete-originals and then again with it, once the server side looks
right, is the intended two-step workflow.
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timezone as dt_timezone
from typing import Dict, List, Optional, Set, Tuple

# task-client lives in the same venv bin directory as this process. Using
# the full path ensures systemd (whose units run with a minimal environment,
# not the invoking shell's PATH) can find it - same fix task-crontab already
# has, applied here after a real end-to-end test caught its absence: a
# report-mode unit failed with exit 127 (command not found) under a real
# systemd --user session.
_VENV_BIN = os.path.dirname(sys.executable)
_TASK_CLIENT = os.path.join(_VENV_BIN, "task-client")

MANAGED_PREFIX = "ophix-"
DEFAULT_UNIT_DIR = "/etc/systemd/system"
DEFAULT_USER_UNIT_DIR = "~/.config/systemd/user"
DEFAULT_USER = "root"

SCOPE_SYSTEM = "system"  # /etc/systemd/system, systemctl, User= directive
SCOPE_USER = "user"      # ~/.config/systemd/user, systemctl --user, no User=

# Reserved stem for the self-perpetuating "install" sync timer. Excluded from
# sync_units()/clear_units() reconciliation so it never gets torn down as an
# orphaned task unit — it isn't a server task, it's what re-runs `sync`.
BOOTSTRAP_PREFIX = "ophix-tasks-sync"
DEFAULT_SYNC_INTERVAL = "15min"  # OnUnitActiveSec= value


def default_unit_dir(scope):
    # type: (str) -> str
    if scope == SCOPE_USER:
        return os.path.expanduser(DEFAULT_USER_UNIT_DIR)
    return DEFAULT_UNIT_DIR


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


def bootstrap_stem(schedule):
    # type: (Optional[str]) -> str
    """Return the reserved stem for the self-perpetuating sync timer."""
    if schedule:
        return "{}-{}".format(BOOTSTRAP_PREFIX, _sanitize_name(schedule))
    return BOOTSTRAP_PREFIX


def _is_bootstrap_stem(stem):
    # type: (str) -> bool
    return stem == BOOTSTRAP_PREFIX or stem.startswith(BOOTSTRAP_PREFIX + "-")


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
            shell_cmd = "{} 2>&1 | {} report {}".format(command, _TASK_CLIENT, task_id)
        else:
            shell_cmd = "{} | {} report {}".format(command, _TASK_CLIENT, task_id)
    elif stderr == "report" and task_id is not None:
        shell_cmd = "{} 2>&1 1>/dev/null | {} report {}".format(command, _TASK_CLIENT, task_id)
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


def service_unit(task, user, scope=SCOPE_SYSTEM):
    # type: (Dict, str, str) -> str
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
    ]
    # User= is only valid on system-scope units. A --user unit already runs
    # as whichever account owns the session; systemd rejects User= there.
    if scope != SCOPE_USER:
        lines.append("User={}".format(user))
    lines.append("ExecStart={}".format(exec_start))
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

def _systemctl(*args, **kwargs):
    # type: (*str, **str) -> None
    scope = kwargs.pop("scope", SCOPE_SYSTEM)
    cmd = ["systemctl"]
    if scope == SCOPE_USER:
        cmd.append("--user")
    cmd.extend(args)
    subprocess.run(cmd, check=False)


def _remove_stem(unit_dir, stem, scope=SCOPE_SYSTEM):
    # type: (str, str, str) -> None
    _systemctl("stop", stem + ".timer", scope=scope)
    _systemctl("disable", stem + ".timer", scope=scope)
    for ext in (".timer", ".service"):
        path = os.path.join(unit_dir, stem + ext)
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_units(tasks, unit_dir=None, user=DEFAULT_USER, scope=SCOPE_SYSTEM):
    # type: (List[Dict], Optional[str], str, str) -> Dict
    """
    Reconcile systemd units against the task list.

    Returns a summary dict with keys: written, skipped, removed, errors.
    """
    if unit_dir is None:
        unit_dir = default_unit_dir(scope)
    if scope == SCOPE_USER:
        os.makedirs(unit_dir, exist_ok=True)
    before = {s for s in existing_stems(unit_dir) if not _is_bootstrap_stem(s)}
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

        if not task.get("enabled", True) or task.get("paused", False):
            if stem in before:
                try:
                    _remove_stem(unit_dir, stem, scope=scope)
                    summary["removed"] += 1
                except OSError as exc:
                    summary["errors"].append("{}: {}".format(name, exc))
            label = "paused" if task.get("paused", False) else "disabled"
            summary["skipped"].append("{} ({})".format(name, label))
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
                f.write(service_unit(task, user, scope=scope))
            active.add(stem)
            summary["written"] += 1
        except OSError as exc:
            summary["errors"].append("{}: {}".format(name, exc))

    # Remove units for tasks no longer returned by the server
    for stem in before - active:
        try:
            _remove_stem(unit_dir, stem, scope=scope)
            summary["removed"] += 1
        except OSError as exc:
            summary["errors"].append("remove {}: {}".format(stem, exc))

    if active or (before - active):
        _systemctl("daemon-reload", scope=scope)
        for stem in active:
            _systemctl("enable", "--now", stem + ".timer", scope=scope)

    return summary


# ---------------------------------------------------------------------------
# Show (dry run)
# ---------------------------------------------------------------------------

def show_units(tasks, user=DEFAULT_USER, scope=SCOPE_SYSTEM):
    # type: (List[Dict], str, str) -> str
    """Return a text preview of unit files that would be written."""
    lines = []  # type: List[str]
    for task in tasks:
        name = task.get("name", "unnamed")
        stem = stem_for_task(task)

        if not task.get("enabled", True) or task.get("paused", False):
            label = "paused" if task.get("paused", False) else "disabled"
            lines.append("# SKIP ({}): {}".format(label, name))
            continue

        calendar, skip_reason = resolve_calendar(task)
        if skip_reason:
            lines.append("# SKIP {}: {}".format(name, skip_reason))
            continue

        lines.append("### {}.timer".format(stem))
        lines.append(timer_unit(task, calendar))
        lines.append("### {}.service".format(stem))
        lines.append(service_unit(task, user, scope=scope))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------

def clear_units(unit_dir=None, scope=SCOPE_SYSTEM, include_bootstrap=False):
    # type: (Optional[str], str, bool) -> int
    """
    Stop, disable, and remove ophix-managed task units. Returns count removed.

    The install-time bootstrap sync timer (see install_sync_timer) is left
    alone unless include_bootstrap=True — clear is meant to reset the task
    set, not the mechanism that keeps re-syncing it.
    """
    if unit_dir is None:
        unit_dir = default_unit_dir(scope)
    all_stems = existing_stems(unit_dir)
    stems = all_stems if include_bootstrap else {
        s for s in all_stems if not _is_bootstrap_stem(s)
    }
    for stem in stems:
        _remove_stem(unit_dir, stem, scope=scope)
    if stems:
        _systemctl("daemon-reload", scope=scope)
    return len(stems)


# ---------------------------------------------------------------------------
# Install (self-perpetuating sync timer)
# ---------------------------------------------------------------------------

def _venv_bin_path(name):
    # type: (str) -> str
    """Full path to a console-script installed in the same venv as this process."""
    return os.path.join(os.path.dirname(sys.executable), name)


def bootstrap_timer_unit(interval):
    # type: (str) -> str
    return "\n".join([
        "[Unit]",
        "Description=Ophix: periodic task-systemd sync",
        "",
        "[Timer]",
        "OnBootSec=5min",
        "OnUnitActiveSec={}".format(interval),
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])


def bootstrap_service_unit(sync_args, scope):
    # type: (List[str], str) -> str
    exec_start = " ".join([_venv_bin_path("task-systemd")] + sync_args)
    lines = [
        "[Unit]",
        "Description=Ophix: periodic task-systemd sync",
        "",
        "[Service]",
        "Type=oneshot",
    ]
    # The bootstrap job itself writes unit files and calls systemctl, so in
    # system scope it always runs as root regardless of the --user value
    # (which only governs the *task* units it goes on to write).
    if scope != SCOPE_USER:
        lines.append("User=root")
    lines.append("ExecStart={}".format(exec_start))
    lines.append("")
    return "\n".join(lines)


def install_sync_timer(schedule=None, interval=DEFAULT_SYNC_INTERVAL,
                        unit_dir=None, user=DEFAULT_USER, scope=SCOPE_SYSTEM):
    # type: (Optional[str], str, Optional[str], str, str) -> str
    """
    Write and enable a timer that re-runs `task-systemd sync` on its own,
    using the exact flags passed here, so the periodic re-sync is a faithful
    reproduction of this install call. Idempotent: re-running with the same
    schedule overwrites the same two unit files rather than adding a second
    timer. Returns the stem written.
    """
    if unit_dir is None:
        unit_dir = default_unit_dir(scope)
    if scope == SCOPE_USER:
        os.makedirs(unit_dir, exist_ok=True)

    stem = bootstrap_stem(schedule)
    sync_args = ["sync"]
    if schedule:
        sync_args += ["--schedule", schedule]
    sync_args += ["--user", user, "--unit-dir", unit_dir, "--scope", scope]

    timer_path = os.path.join(unit_dir, stem + ".timer")
    service_path = os.path.join(unit_dir, stem + ".service")
    with open(timer_path, "w", encoding="utf-8") as f:
        f.write(bootstrap_timer_unit(interval))
    with open(service_path, "w", encoding="utf-8") as f:
        f.write(bootstrap_service_unit(sync_args, scope))

    _systemctl("daemon-reload", scope=scope)
    _systemctl("enable", "--now", stem + ".timer", scope=scope)
    return stem


# ---------------------------------------------------------------------------
# Reload (exposed for callers that remove units outside sync/install/clear)
# ---------------------------------------------------------------------------

def reload_daemon(scope=SCOPE_SYSTEM):
    # type: (str) -> None
    _systemctl("daemon-reload", scope=scope)


# ---------------------------------------------------------------------------
# Import (translate existing, non-ophix units into task definitions)
# ---------------------------------------------------------------------------

def _parse_unit_sections(text):
    # type: (str) -> Dict[str, Dict[str, List[str]]]
    """
    Parse a systemd unit file into {section: {key: [values]}}. Repeated
    keys (e.g. multiple ExecStart=/OnCalendar= lines, a real and legal
    systemd pattern) are kept as a list rather than the last-one-wins
    behaviour of most INI parsers, so callers can detect and refuse to
    guess at ambiguous units instead of silently picking one.
    """
    sections = {}  # type: Dict[str, Dict[str, List[str]]]
    current = None  # type: Optional[str]
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            sections.setdefault(current, {})
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        sections[current].setdefault(key.strip(), []).append(value.strip())
    return sections


def discover_foreign_timers(unit_dir):
    # type: (str) -> List[str]
    """Return stems of .timer files in unit_dir that are NOT ophix-managed."""
    stems = []  # type: List[str]
    try:
        for fname in os.listdir(unit_dir):
            if fname.endswith(".timer") and not fname.startswith(MANAGED_PREFIX):
                stems.append(fname[: -len(".timer")])
    except OSError:
        pass
    return sorted(stems)


def parse_foreign_unit(unit_dir, stem):
    # type: (str, str) -> Dict
    """
    Best-effort translate an existing (non-ophix) .timer + its target
    .service into a task definition. Always returns a dict; when the unit
    doesn't map cleanly onto our single-command, single-schedule model,
    'skip_reason' is non-empty and every other field should be ignored.
    """
    timer_path = os.path.join(unit_dir, stem + ".timer")
    try:
        with open(timer_path, "r", encoding="utf-8") as f:
            timer_sections = _parse_unit_sections(f.read())
    except OSError as exc:
        return {"name": stem, "skip_reason": "cannot read {}: {}".format(timer_path, exc)}

    timer_section = timer_sections.get("Timer", {})
    calendars = timer_section.get("OnCalendar", [])
    if not calendars:
        return {
            "name": stem,
            "skip_reason": "no OnCalendar= in {}.timer (monotonic-only timers, "
                            "e.g. OnBootSec=/OnUnitActiveSec= with no calendar, "
                            "are not supported by import)".format(stem),
        }
    if len(calendars) > 1:
        return {
            "name": stem,
            "skip_reason": "multiple OnCalendar= lines in {}.timer — not "
                            "representable as a single interval".format(stem),
        }
    interval = calendars[0]

    # A timer activates the like-named .service by default, unless it names
    # a different target explicitly via Unit=.
    unit_refs = timer_section.get("Unit", [])
    if unit_refs and unit_refs[0].endswith(".service"):
        service_stem = unit_refs[0][: -len(".service")]
    else:
        service_stem = stem
    service_path = os.path.join(unit_dir, service_stem + ".service")

    try:
        with open(service_path, "r", encoding="utf-8") as f:
            service_sections = _parse_unit_sections(f.read())
    except OSError as exc:
        return {"name": stem, "skip_reason": "cannot read {}: {}".format(service_path, exc)}

    unit_section = service_sections.get("Unit", {})
    service_section = service_sections.get("Service", {})

    # Type= defaults to "simple" when absent. Only oneshot (run-and-exit)
    # maps onto our task model; anything else is left alone rather than
    # guessed at.
    service_type = service_section.get("Type", ["simple"])[0]
    if service_type != "oneshot":
        return {
            "name": stem,
            "skip_reason": "Type={} in {}.service (only Type=oneshot units "
                            "are supported by import)".format(service_type, service_stem),
        }

    exec_starts = service_section.get("ExecStart", [])
    if not exec_starts:
        return {"name": stem, "skip_reason": "no ExecStart= in {}.service".format(service_stem)}
    if len(exec_starts) > 1:
        return {
            "name": stem,
            "skip_reason": "multiple ExecStart= lines in {}.service — not "
                            "representable as a single command".format(service_stem),
        }

    return {
        "name": stem,
        "description": (unit_section.get("Description", [stem])[0]),
        "command": exec_starts[0],
        "interval": interval,
        "user": service_section.get("User", [DEFAULT_USER])[0],
        "timer_stem": stem,
        "service_stem": service_stem,
        "skip_reason": "",
    }


def remove_foreign_unit(unit_dir, timer_stem, service_stem, scope=SCOPE_SYSTEM):
    # type: (str, str, str, str) -> None
    """
    Stop, disable, and delete an arbitrary (non-ophix) timer and the service
    it targets. Unlike _remove_stem, timer and service stems may differ
    (Unit= override), and no ophix- prefix is assumed.
    """
    _systemctl("stop", timer_stem + ".timer", scope=scope)
    _systemctl("disable", timer_stem + ".timer", scope=scope)
    for stem, ext in ((timer_stem, ".timer"), (service_stem, ".service")):
        path = os.path.join(unit_dir, stem + ext)
        if os.path.exists(path):
            os.remove(path)
