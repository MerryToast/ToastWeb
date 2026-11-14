"""Pure monitoring configuration, unit templates and alert state for ToastWeb.

This module deliberately has no I/O, no imports from ``toastweb`` and never runs
systemctl or a notification command.  The runner owns paths, locking, delivery
and persistence; this module only decides what *should* be delivered next.

Configuration is validated defensively because it is loaded from a root-owned
file.  State is a fixed-size, JSON-safe summary: raw diagnostics messages,
details, URLs and notification commands are never copied into it.

Notification semantics
----------------------
``advance`` returns ``(state, event)``.  The returned state has *not* recorded
the event as delivered.  The runner sends the event (or treats the journal as
the delivery when ``notify_command`` is ``None``) and then calls
``record_delivery``.  A failed delivery is retried after
``RETRY_DELAY_SECONDS``; a clock moving backwards never suppresses that retry
forever.
"""

from __future__ import annotations

import hashlib
import math
import posixpath
import re
import unicodedata
from datetime import datetime
from typing import Any, NoReturn

MANAGED_BY = "ToastWeb"
SCHEMA_VERSION = 1

HEALTH_SERVICE = "toastweb-health.service"
HEALTH_TIMER = "toastweb-health.timer"

RETRY_DELAY_SECONDS = 300

DEFAULT_MONITOR: dict[str, Any] = {
    "enabled": False,
    "interval_seconds": 120,
    "failure_threshold": 3,
    "warn_cert_days": 20,
    "notify_command": None,
}

CONFIG_ERROR = "invalid monitor configuration"
STATE_ERROR = "invalid monitor state"
REPORT_ERROR = "invalid monitor report"
EVENT_ERROR = "invalid monitor event"
UNIT_ERROR = "invalid unit template input"

_STATUSES = frozenset({"ok", "warning", "error", "unknown"})
_LEVELS = frozenset({"ok", "info", "warning", "error", "unknown"})
_ISSUE_LEVELS = frozenset({"warning", "error", "unknown"})
_LEVEL_RANK = {"unknown": 1, "warning": 2, "error": 3}

_MAX_TIME = 10**12
_MAX_COUNTER = 1_000_000
_MAX_SIGNATURE_IDS = 4096
_MAX_MARKER = 256

_STATE_KEYS = (
    "managed_by",
    "schema_version",
    "domain",
    "port",
    "consecutive_failures",
    "alerted_signature",
    "last_attempt_at",
    "last_attempt_signature",
    "last_attempt_kind",
    "last_attempt_succeeded",
    "last_level",
    "last_checked_at",
)
_EVENT_KEYS = frozenset({"kind", "severity", "status", "domain", "port", "checked_at", "issue_ids"})
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_SIGNATURE_RE = re.compile(
    r"(?:error|warning|unknown)\|(?:sha256:[0-9a-f]{64}|"
    r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}(?:,[A-Za-z0-9][A-Za-z0-9_.:-]{0,63})*)\Z"
)


def _fail(message: str) -> NoReturn:
    raise ValueError(message)


def _is_unsafe_character(character: str) -> bool:
    return unicodedata.category(character) in _UNSAFE_CATEGORIES


def _finite(value: Any, message: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(message)
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(message)
        return value
    if abs(value) > _MAX_TIME:
        _fail(message)
    return float(value)


def _report_checked_at(value: Any) -> float:
    """Normalize a diagnostics report timestamp to a finite epoch second.

    Diagnostics emit a UTC ISO-8601 string (with a trailing ``Z``), while
    programmatic callers may supply an epoch number.  Internal state and events
    remain strict numeric-only contracts.
    """

    if isinstance(value, str):
        if not value or len(value) > 128 or value != value.strip():
            _fail(REPORT_ERROR)
        if any(_is_unsafe_character(character) for character in value):
            _fail(REPORT_ERROR)
        text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        try:
            parsed = datetime.fromisoformat(text)
        except (ValueError, OverflowError):
            _fail(REPORT_ERROR)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _fail(REPORT_ERROR)
        try:
            timestamp = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            _fail(REPORT_ERROR)
        if not math.isfinite(timestamp) or timestamp < 0:
            _fail(REPORT_ERROR)
        return timestamp

    timestamp = _finite(value, REPORT_ERROR)
    if timestamp < 0:
        _fail(REPORT_ERROR)
    return timestamp


def _port(value: Any, message: str) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        _fail(message)
    return value


def _domain(value: Any, message: str) -> str:
    if type(value) is not str or not value or len(value) > 253:
        _fail(message)
    if any(character.isspace() or _is_unsafe_character(character) for character in value):
        _fail(message)
    if "/" in value or "\\" in value:
        _fail(message)
    return value


def _marker(value: Any) -> str:
    if type(value) is not str or not value or len(value) > _MAX_MARKER:
        _fail(UNIT_ERROR)
    if any(_is_unsafe_character(character) for character in value):
        _fail(UNIT_ERROR)
    return value


def _notify_command(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        _fail(CONFIG_ERROR)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(CONFIG_ERROR)
    if len(value) > 4096 or len(encoded) > 4096:
        _fail(CONFIG_ERROR)
    if not posixpath.isabs(value):
        _fail(CONFIG_ERROR)
    if any(_is_unsafe_character(character) for character in value):
        _fail(CONFIG_ERROR)
    return value


def normalize_monitor(value: Any) -> dict[str, Any]:
    """Return a validated copy of monitor settings merged with defaults.

    ``notify_command`` is a single executable path, never a shell string.  It is
    not split into arguments here; the runner must execute it as one path.
    """

    if not isinstance(value, dict):
        _fail(CONFIG_ERROR)
    if set(value) - set(DEFAULT_MONITOR):
        _fail(CONFIG_ERROR)

    result = dict(DEFAULT_MONITOR)
    result.update(value)

    if type(result["enabled"]) is not bool:
        _fail(CONFIG_ERROR)
    for key, lower, upper in (
        ("interval_seconds", 60, 3600),
        ("failure_threshold", 1, 10),
        ("warn_cert_days", 1, 90),
    ):
        setting = result[key]
        if type(setting) is not int or not lower <= setting <= upper:
            _fail(CONFIG_ERROR)

    result["notify_command"] = _notify_command(result["notify_command"])
    return result


def render_health_unit(marker: str) -> str:
    """Render the opt-in health-check oneshot unit.

    The unit only runs the local check.  It never pulls in, starts or restarts
    the website unit and deliberately has no restart/watchdog directives.
    """

    marker = _marker(marker)
    return f"""{marker}
[Unit]
Description=ToastWeb health check
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/toastweb monitor check --json
TimeoutStartSec=120s
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
NoNewPrivileges=true
"""


def render_health_timer(marker: str, interval_seconds: int = 120) -> str:
    """Render the monotonic health-check timer; no Persistent= is needed."""

    marker = _marker(marker)
    if type(interval_seconds) is not int or not 60 <= interval_seconds <= 3600:
        _fail(UNIT_ERROR)
    return f"""{marker}
[Unit]
Description=ToastWeb health check timer

[Timer]
OnActiveSec=10s
OnUnitInactiveSec={interval_seconds}s
AccuracySec=5s
Unit={HEALTH_SERVICE}

[Install]
WantedBy=timers.target
"""


def _new_state(domain: str, port: int) -> dict[str, Any]:
    return {
        "managed_by": MANAGED_BY,
        "schema_version": SCHEMA_VERSION,
        "domain": domain,
        "port": port,
        "consecutive_failures": 0,
        "alerted_signature": None,
        "last_attempt_at": None,
        "last_attempt_signature": None,
        "last_attempt_kind": None,
        "last_attempt_succeeded": None,
        "last_level": None,
        "last_checked_at": None,
    }


def _validate_signature(value: Any) -> str:
    if type(value) is not str or len(value) > 8192 or not _SIGNATURE_RE.fullmatch(value):
        _fail(STATE_ERROR)
    return value


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(_STATE_KEYS):
        _fail(STATE_ERROR)
    if value["managed_by"] != MANAGED_BY:
        _fail(STATE_ERROR)
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        _fail(STATE_ERROR)

    domain = _domain(value["domain"], STATE_ERROR)
    port = _port(value["port"], STATE_ERROR)

    count = value["consecutive_failures"]
    if type(count) is not int or not 0 <= count <= _MAX_COUNTER:
        _fail(STATE_ERROR)

    alerted = value["alerted_signature"]
    if alerted is not None:
        alerted = _validate_signature(alerted)

    last_level = value["last_level"]
    if last_level is not None and (type(last_level) is not str or last_level not in _ISSUE_LEVELS):
        _fail(STATE_ERROR)

    last_checked = value["last_checked_at"]
    if last_checked is not None:
        last_checked = _finite(last_checked, STATE_ERROR)

    last_attempt_at = value["last_attempt_at"]
    last_attempt_kind = value["last_attempt_kind"]
    last_attempt_signature = value["last_attempt_signature"]
    last_attempt_succeeded = value["last_attempt_succeeded"]

    if last_attempt_at is None:
        if last_attempt_kind is not None or last_attempt_signature is not None or last_attempt_succeeded is not None:
            _fail(STATE_ERROR)
    else:
        last_attempt_at = _finite(last_attempt_at, STATE_ERROR)
        if type(last_attempt_kind) is not str or last_attempt_kind not in {"failure", "recovery"}:
            _fail(STATE_ERROR)
        if type(last_attempt_succeeded) is not bool:
            _fail(STATE_ERROR)
        if last_attempt_kind == "failure":
            last_attempt_signature = _validate_signature(last_attempt_signature)
        elif last_attempt_signature is not None:
            _fail(STATE_ERROR)

    return {
        "managed_by": MANAGED_BY,
        "schema_version": SCHEMA_VERSION,
        "domain": domain,
        "port": port,
        "consecutive_failures": count,
        "alerted_signature": alerted,
        "last_attempt_at": last_attempt_at,
        "last_attempt_signature": last_attempt_signature,
        "last_attempt_kind": last_attempt_kind,
        "last_attempt_succeeded": last_attempt_succeeded,
        "last_level": last_level,
        "last_checked_at": last_checked,
    }


def validate_runtime_state(value: Any) -> dict[str, Any]:
    """Validate owned persisted metadata before a runner displays or reuses it."""
    return _validate_state(value)


def _safe_issue_id(raw: str) -> str:
    if _SAFE_ID_RE.fullmatch(raw):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8", "backslashreplace")).hexdigest()[:16]
    return "issue-" + digest


def _signature(severity: str, issue_ids: list[str]) -> str:
    joined = ",".join(issue_ids)
    if len(joined) > _MAX_SIGNATURE_IDS:
        joined = "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return f"{severity}|{joined}"


def _validate_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _EVENT_KEYS:
        _fail(EVENT_ERROR)

    kind = value["kind"]
    if type(kind) is not str or kind not in {"failure", "recovery"}:
        _fail(EVENT_ERROR)

    severity = value["severity"]
    if type(severity) is not str or severity not in _LEVELS:
        _fail(EVENT_ERROR)
    if kind == "failure" and severity not in _ISSUE_LEVELS:
        _fail(EVENT_ERROR)

    status = value["status"]
    if type(status) is not str or status not in _STATUSES:
        _fail(EVENT_ERROR)

    domain = _domain(value["domain"], EVENT_ERROR)
    port = _port(value["port"], EVENT_ERROR)
    checked_at = _finite(value["checked_at"], EVENT_ERROR)

    issue_ids = value["issue_ids"]
    if type(issue_ids) is not list or any(type(item) is not str for item in issue_ids):
        _fail(EVENT_ERROR)
    if any(not _SAFE_ID_RE.fullmatch(item) for item in issue_ids):
        _fail(EVENT_ERROR)
    if issue_ids != sorted(set(issue_ids)):
        _fail(EVENT_ERROR)
    if kind == "failure" and not issue_ids:
        _fail(EVENT_ERROR)
    if kind == "recovery" and issue_ids:
        _fail(EVENT_ERROR)

    return {
        "kind": kind,
        "severity": severity,
        "status": status,
        "domain": domain,
        "port": port,
        "checked_at": checked_at,
        "issue_ids": issue_ids,
    }


def _validate_report(report: Any) -> tuple[str, int, float, str, dict[str, str]]:
    if not isinstance(report, dict):
        _fail(REPORT_ERROR)

    domain = _domain(report.get("domain"), REPORT_ERROR)
    port = _port(report.get("port"), REPORT_ERROR)
    checked_at = _report_checked_at(report.get("checked_at"))

    status = report.get("status")
    if type(status) is not str or status not in _STATUSES:
        _fail(REPORT_ERROR)

    checks = report.get("checks")
    if type(checks) is not list:
        _fail(REPORT_ERROR)

    issues: dict[str, str] = {}
    for check in checks:
        if not isinstance(check, dict):
            _fail(REPORT_ERROR)
        check_id = check.get("id")
        if type(check_id) is not str or not check_id:
            _fail(REPORT_ERROR)
        level = check.get("level")
        if type(level) is not str or level not in _LEVELS:
            _fail(REPORT_ERROR)
        critical = check.get("critical")
        if type(critical) is not bool:
            _fail(REPORT_ERROR)
        if not critical or level not in _ISSUE_LEVELS:
            continue
        safe_id = _safe_issue_id(check_id)
        previous_level = issues.get(safe_id)
        if previous_level is None or _LEVEL_RANK[level] > _LEVEL_RANK[previous_level]:
            issues[safe_id] = level

    return domain, port, checked_at, status, issues


def _highest_level(issues: dict[str, str]) -> str:
    return max(issues.values(), key=lambda level: _LEVEL_RANK[level])


def _retry_throttled(state: dict[str, Any], kind: str, signature: str | None, now: float) -> bool:
    if state["last_attempt_succeeded"] is not False:
        return False
    last_attempt_at = state["last_attempt_at"]
    if last_attempt_at is None or state["last_attempt_kind"] != kind:
        return False
    if kind == "failure" and state["last_attempt_signature"] != signature:
        return False
    # A backwards clock must not be able to suppress retries indefinitely.
    if now < last_attempt_at:
        return False
    return now - last_attempt_at < RETRY_DELAY_SECONDS


def advance(
    previous: dict[str, Any] | None,
    report: dict[str, Any],
    settings: dict[str, Any],
    *,
    now: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Compute the next monitor state and, at most, one event to deliver.

    The caller gates monitoring on ``enabled`` as well; this core also returns
    no event when settings are disabled.  State is validated strictly so a
    foreign or damaged file is never silently overwritten.  A state belonging to
    another domain/port is legitimately reset as a new monitoring object.
    """

    settings = normalize_monitor(settings)
    now = _finite(now, STATE_ERROR)
    domain, port, checked_at, status, issues = _validate_report(report)

    if previous is None:
        state = _new_state(domain, port)
    else:
        state = _validate_state(previous)
        if state["domain"] != domain or state["port"] != port:
            state = _new_state(domain, port)

    if not settings["enabled"]:
        return state, None

    state["last_checked_at"] = checked_at

    if issues:
        severity = _highest_level(issues)
        issue_ids = sorted(issues)
        signature = _signature(severity, issue_ids)
        state["last_level"] = severity
        count = state["consecutive_failures"]
        state["consecutive_failures"] = min(count + 1, _MAX_COUNTER)

        event: dict[str, Any] | None = None
        if (
            state["consecutive_failures"] >= settings["failure_threshold"]
            and signature != state["alerted_signature"]
            and not _retry_throttled(state, "failure", signature, now)
        ):
            event = {
                "kind": "failure",
                "severity": severity,
                "status": status,
                "domain": domain,
                "port": port,
                "checked_at": checked_at,
                "issue_ids": issue_ids,
            }
        return state, event

    state["consecutive_failures"] = 0
    state["last_level"] = None
    if state["alerted_signature"] is None or _retry_throttled(state, "recovery", None, now):
        return state, None

    return state, {
        "kind": "recovery",
        "severity": "info",
        "status": status,
        "domain": domain,
        "port": port,
        "checked_at": checked_at,
        "issue_ids": [],
    }


def record_delivery(
    state: dict[str, Any],
    event: dict[str, Any],
    *,
    success: bool,
    now: float,
) -> dict[str, Any]:
    """Record a delivery attempt after the runner has actually tried it.

    A failed attempt updates only the retry bookkeeping.  It never marks the
    failure as alerted, so the next eligible round can retry it.  A successful
    recovery clears the alerted marker; a recovery that was never announced does
    not exist because ``advance`` only creates one after a delivered failure.
    """

    state = _validate_state(state)
    event = _validate_event(event)
    if type(success) is not bool:
        _fail(EVENT_ERROR)
    now = _finite(now, STATE_ERROR)

    signature = _signature(event["severity"], event["issue_ids"]) if event["kind"] == "failure" else None
    state["last_attempt_at"] = now
    state["last_attempt_kind"] = event["kind"]
    state["last_attempt_signature"] = signature
    state["last_attempt_succeeded"] = success

    if success:
        state["alerted_signature"] = signature

    return state
