"""Pure monitoring tests: no diagnostics module, services, network or real notification."""

from __future__ import annotations

import json
import math
import unittest
from datetime import datetime, timezone

import toastweb_monitor as tm

DOMAIN = "docs.example.org"
PORT = 8443
STATE_KEYS = {
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
}
EVENT_KEYS = {"kind", "severity", "status", "domain", "port", "checked_at", "issue_ids"}


def settings(**overrides: object) -> dict[str, object]:
    value = dict(tm.DEFAULT_MONITOR)
    value.update(overrides)
    return value


def check(check_id: str, level: str, critical: bool = True) -> dict[str, object]:
    return {
        "id": check_id,
        "level": level,
        "critical": critical,
        "message": "raw message with secret-token",
        "details": {"url": "https://private.example/token", "command": "do-not-store"},
    }


def report(
    *checks: dict[str, object],
    domain: str = DOMAIN,
    port: int = PORT,
    status: str = "ok",
    checked_at: object = 1000.0,
) -> dict[str, object]:
    return {
        "domain": domain,
        "port": port,
        "checked_at": checked_at,
        "status": status,
        "checks": list(checks),
    }


class ConfigTests(unittest.TestCase):
    def test_default_monitor_is_disabled(self) -> None:
        self.assertEqual(
            tm.DEFAULT_MONITOR,
            {
                "enabled": False,
                "interval_seconds": 120,
                "failure_threshold": 3,
                "warn_cert_days": 20,
                "notify_command": None,
            },
        )
        normalized = tm.normalize_monitor({})
        self.assertEqual(normalized, tm.DEFAULT_MONITOR)
        self.assertIsNot(normalized, tm.DEFAULT_MONITOR)

    def test_partial_settings_are_merged_without_mutating_defaults(self) -> None:
        value = tm.normalize_monitor({"enabled": True, "failure_threshold": 1, "notify_command": "/bin/notify"})
        self.assertEqual(value["interval_seconds"], 120)
        self.assertEqual(value["warn_cert_days"], 20)
        self.assertTrue(value["enabled"])
        self.assertEqual(value["notify_command"], "/bin/notify")
        self.assertFalse(tm.DEFAULT_MONITOR["enabled"])

    def test_non_dict_and_unknown_keys_are_rejected(self) -> None:
        for value in (None, [], "enabled", 1, True):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                tm.normalize_monitor(value)
            self.assertEqual(str(caught.exception), tm.CONFIG_ERROR)
        with self.assertRaises(ValueError):
            tm.normalize_monitor({"enabled": True, "surprise": 1})

    def test_enabled_must_be_a_real_bool(self) -> None:
        for value in (1, 0, "true", None, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tm.normalize_monitor({"enabled": value})

    def test_numeric_ranges_reject_bools_and_floats(self) -> None:
        for key, lower, upper in (
            ("interval_seconds", 60, 3600),
            ("failure_threshold", 1, 10),
            ("warn_cert_days", 1, 90),
        ):
            for value in (lower, upper):
                with self.subTest(key=key, value=value):
                    normalized = tm.normalize_monitor({key: value})
                    self.assertEqual(normalized[key], value)
            for value in (lower - 1, upper + 1, True, float(lower), str(lower)):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    tm.normalize_monitor({key: value})

    def test_notify_command_is_one_absolute_path(self) -> None:
        self.assertIsNone(tm.normalize_monitor({"notify_command": None})["notify_command"])
        self.assertEqual(tm.normalize_monitor({"notify_command": "/bin/notify"})["notify_command"], "/bin/notify")
        spaced = "/usr/local/bin/notify me"
        self.assertEqual(tm.normalize_monitor({"notify_command": spaced})["notify_command"], spaced)
        for value in ("notify", "./notify", "bin/notify", "", 1, [], "/bin/notify\narg", "/bin/notify\x7f"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tm.normalize_monitor({"notify_command": value})

    def test_notify_command_size_limit(self) -> None:
        self.assertEqual(len(tm.normalize_monitor({"notify_command": "/" + "a" * 4095})["notify_command"]), 4096)
        with self.assertRaises(ValueError):
            tm.normalize_monitor({"notify_command": "/" + "a" * 4096})

    def test_errors_do_not_echo_rejected_values(self) -> None:
        secret = "/tmp/secret\n--password=hunter2"
        with self.assertRaises(ValueError) as caught:
            tm.normalize_monitor({"notify_command": secret})
        self.assertEqual(str(caught.exception), tm.CONFIG_ERROR)
        self.assertNotIn("hunter2", str(caught.exception))


class UnitTemplateTests(unittest.TestCase):
    def test_health_unit_is_local_and_does_not_start_the_site(self) -> None:
        unit = tm.render_health_unit("# test marker")
        self.assertIn("# test marker", unit)
        self.assertIn("[Unit]\nDescription=", unit)
        self.assertIn("After=network-online.target", unit)
        self.assertIn("Type=oneshot", unit)
        self.assertIn("ExecStart=/usr/local/bin/toastweb monitor check --json", unit)
        self.assertIn("TimeoutStartSec=120s", unit)
        for line in ("PrivateTmp=true", "ProtectHome=true", "ProtectSystem=full", "NoNewPrivileges=true"):
            self.assertIn(line, unit)
        lowered = unit.lower()
        for forbidden in ("wants=", "requires=", "restart=", "watchdogsec=", "nginx"):
            self.assertNotIn(forbidden, lowered)
        self.assertNotIn("[Install]", unit)

    def test_health_timer_uses_a_monotonic_interval(self) -> None:
        timer = tm.render_health_timer("# test marker", 75)
        self.assertIn("# test marker", timer)
        self.assertIn("OnActiveSec=10s", timer)
        self.assertIn("OnUnitInactiveSec=75s", timer)
        self.assertIn("AccuracySec=5s", timer)
        self.assertIn("Unit=toastweb-health.service", timer)
        self.assertIn("[Install]\nWantedBy=timers.target", timer)
        self.assertNotIn("Persistent=", timer)

    def test_timer_interval_is_bounded(self) -> None:
        for value in (60, 3600):
            self.assertIn(f"OnUnitInactiveSec={value}s", tm.render_health_timer("# marker", value))
        for value in (59, 3601, True, 120.0, "120"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tm.render_health_timer("# marker", value)  # type: ignore[arg-type]

    def test_marker_must_be_single_line(self) -> None:
        for value in ("", "# one\n# two", "# tab\there", 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tm.render_health_unit(value)  # type: ignore[arg-type]


class CoreTests(unittest.TestCase):
    def test_advance_creates_a_safe_state(self) -> None:
        state, event = tm.advance(None, report(check("tls-cert", "ok")), settings(enabled=True), now=1000.0)
        self.assertIsNone(event)
        self.assertEqual(set(state), STATE_KEYS)
        self.assertEqual(state["managed_by"], tm.MANAGED_BY)
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(state["domain"], DOMAIN)
        self.assertEqual(state["port"], PORT)
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertIsNone(state["alerted_signature"])
        self.assertEqual(state["last_checked_at"], 1000.0)
        json.dumps(state)

    def test_report_checked_at_accepts_zulu_and_offset_iso(self) -> None:
        expected = datetime(2026, 11, 14, 5, 33, 21, tzinfo=timezone.utc).timestamp()
        for value in ("2026-11-14T05:33:21Z", "2026-11-14T13:33:21+08:00"):
            with self.subTest(value=value):
                state, event = tm.advance(
                    None,
                    report(check("tls", "error"), status="error", checked_at=value),
                    settings(enabled=True, failure_threshold=1),
                    now=expected,
                )
                assert event is not None
                self.assertEqual(state["last_checked_at"], expected)
                self.assertEqual(event["checked_at"], expected)
                self.assertIsInstance(state["last_checked_at"], float)
                self.assertIsInstance(event["checked_at"], float)

    def test_report_checked_at_accepts_fractional_zulu(self) -> None:
        expected = datetime(2026, 11, 14, 5, 33, 21, 250000, tzinfo=timezone.utc).timestamp()
        state, event = tm.advance(
            None,
            report(check("tls", "ok"), checked_at="2026-11-14T05:33:21.250Z"),
            settings(enabled=True),
            now=expected,
        )
        self.assertIsNone(event)
        self.assertEqual(state["last_checked_at"], expected)

    def test_report_checked_at_accepts_non_negative_epoch_numbers(self) -> None:
        for value in (0, 0.0, 1000, 1000.5):
            with self.subTest(value=value):
                state, event = tm.advance(None, report(checked_at=value), settings(enabled=True), now=1000.0)
                self.assertIsNone(event)
                self.assertEqual(state["last_checked_at"], float(value))

    def test_report_checked_at_rejects_naive_or_invalid_strings(self) -> None:
        for value in (
            "2026-11-14T05:33:21",  # naive: no timezone
            "2026-11-14",
            "2026-11-14T05:33:21+25:00",
            "2026-11-14T05:33:21Zjunk",
            "",
            "not-a-date",
            " 2026-11-14T05:33:21Z",
            "2026-11-14T05:33:21Z\n",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                tm.advance(None, report(checked_at=value), settings(enabled=True), now=1000.0)
            self.assertEqual(str(caught.exception), tm.REPORT_ERROR)

    def test_report_checked_at_rejects_invalid_numbers(self) -> None:
        for value in (-1, -0.5, True, math.inf, math.nan, None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                tm.advance(None, report(checked_at=value), settings(enabled=True), now=1000.0)
            self.assertEqual(str(caught.exception), tm.REPORT_ERROR)

    def test_malformed_report_types_raise_value_error(self) -> None:
        config = settings(enabled=True, failure_threshold=1)
        cases = [
            [],
            {**report(), "status": []},
            {**report(), "checks": [check("tls", [])]},  # type: ignore[list-item]
            {**report(), "checks": "not-a-list"},
            {**report(), "domain": []},
            {**report(), "port": []},
            {**report(), "checked_at": []},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                tm.advance(None, value, config, now=1000.0)

    def test_disabled_settings_do_not_accumulate_or_emit(self) -> None:
        state, event = tm.advance(
            None, report(check("tls", "error"), status="error"), tm.normalize_monitor({}), now=1000.0
        )
        self.assertIsNone(event)
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertIsNone(state["last_checked_at"])

    def test_threshold_then_dedupe(self) -> None:
        config = settings(enabled=True, failure_threshold=3)
        state = None
        for index in (1, 2):
            state, event = tm.advance(
                state, report(check("tls", "warning"), status="warning", checked_at=1000.0 + index), config, now=1000.0
            )
            self.assertIsNone(event)
            self.assertEqual(state["consecutive_failures"], index)

        state, event = tm.advance(
            state, report(check("tls", "warning"), status="warning", checked_at=1003.0), config, now=1003.0
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(set(event), EVENT_KEYS)
        self.assertEqual(event["kind"], "failure")
        self.assertEqual(event["severity"], "warning")
        self.assertEqual(event["status"], "warning")
        self.assertEqual(event["domain"], DOMAIN)
        self.assertEqual(event["port"], PORT)
        self.assertEqual(event["checked_at"], 1003.0)
        self.assertEqual(event["issue_ids"], ["tls"])
        self.assertIsNone(state["alerted_signature"])
        self.assertIsNone(state["last_attempt_at"])

        state = tm.record_delivery(state, event, success=True, now=1004.0)
        self.assertIsNotNone(state["alerted_signature"])

        for index in range(3):
            state, event = tm.advance(
                state,
                report(check("tls", "warning"), status="warning", checked_at=1005.0 + index),
                config,
                now=1005.0 + index,
            )
            self.assertIsNone(event)

    def test_changed_issue_set_sends_a_new_event(self) -> None:
        config = settings(enabled=True, failure_threshold=1)
        state, event = tm.advance(None, report(check("cert", "warning"), status="warning"), config, now=1000.0)
        assert event is not None
        state = tm.record_delivery(state, event, success=True, now=1001.0)

        state, event = tm.advance(
            state,
            report(check("cert", "warning"), check("tls", "error"), status="error", checked_at=1010.0),
            config,
            now=1010.0,
        )
        assert event is not None
        self.assertEqual(event["issue_ids"], ["cert", "tls"])
        self.assertEqual(event["severity"], "error")

    def test_same_ids_with_higher_severity_sends_a_new_event(self) -> None:
        config = settings(enabled=True, failure_threshold=1)
        state, event = tm.advance(None, report(check("cert", "warning"), status="warning"), config, now=1000.0)
        assert event is not None
        state = tm.record_delivery(state, event, success=True, now=1001.0)
        state, event = tm.advance(
            state, report(check("cert", "error"), status="error", checked_at=1010.0), config, now=1010.0
        )
        assert event is not None
        self.assertEqual(event["severity"], "error")

    def test_only_critical_issue_levels_trigger(self) -> None:
        config = settings(enabled=True, failure_threshold=1)
        ignored = report(
            check("info", "info"),
            check("dns", "warning", critical=False),
            check("optional", "error", critical=False),
            check("optional-unknown", "unknown", critical=False),
            status="error",
        )
        state, event = tm.advance(None, ignored, config, now=1000.0)
        self.assertIsNone(event)
        self.assertEqual(state["consecutive_failures"], 0)

        for level in ("warning", "unknown", "error"):
            with self.subTest(level=level):
                state, event = tm.advance(None, report(check("tls", level), status=level), config, now=1000.0)
                assert event is not None
                self.assertEqual(event["kind"], "failure")
                self.assertEqual(event["severity"], level)
                self.assertEqual(event["issue_ids"], ["tls"])

    def test_critical_unknown_is_a_failure(self) -> None:
        config = settings(enabled=True, failure_threshold=1)
        state, event = tm.advance(None, report(check("probe", "unknown"), status="unknown"), config, now=1000.0)
        assert event is not None
        self.assertEqual(event["severity"], "unknown")
        self.assertEqual(state["last_level"], "unknown")

    def test_recovery_needs_a_previously_delivered_failure(self) -> None:
        config = settings(enabled=True, failure_threshold=1)
        state, event = tm.advance(None, report(check("tls", "error"), status="error"), config, now=1000.0)
        assert event is not None
        # Not delivered yet: a recovery must not be announced.
        state, recovery = tm.advance(state, report(checked_at=1010.0), config, now=1010.0)
        self.assertIsNone(recovery)
        self.assertIsNone(state["alerted_signature"])

        state = tm.record_delivery(state, event, success=True, now=1011.0)
        state, recovery = tm.advance(state, report(checked_at=1020.0), config, now=1020.0)
        assert recovery is not None
        self.assertEqual(recovery["kind"], "recovery")
        self.assertEqual(recovery["severity"], "info")
        self.assertEqual(recovery["issue_ids"], [])
        self.assertEqual(recovery["status"], "ok")

        state = tm.record_delivery(state, recovery, success=True, now=1021.0)
        self.assertIsNone(state["alerted_signature"])
        self.assertEqual(state["last_attempt_kind"], "recovery")
        self.assertIsNone(state["last_attempt_signature"])

    def test_failed_failure_delivery_is_retried_after_300_seconds(self) -> None:
        config = settings(enabled=True, failure_threshold=1)
        state, event = tm.advance(None, report(check("tls", "error"), status="error"), config, now=1000.0)
        assert event is not None
        state = tm.record_delivery(state, event, success=False, now=1000.0)
        self.assertIsNone(state["alerted_signature"])

        for now in (1100.0, 1299.0):
            state, retry = tm.advance(
                state, report(check("tls", "error"), status="error", checked_at=now), config, now=now
            )
            self.assertIsNone(retry)

        state, retry = tm.advance(
            state, report(check("tls", "error"), status="error", checked_at=1300.0), config, now=1300.0
        )
        assert retry is not None
        self.assertEqual(retry["kind"], "failure")

    def test_clock_rollback_allows_a_retry(self) -> None:
        config = settings(enabled=True, failure_threshold=1)
        state, event = tm.advance(None, report(check("tls", "error"), status="error"), config, now=1000.0)
        assert event is not None
        state = tm.record_delivery(state, event, success=False, now=1000.0)

        state, retry = tm.advance(
            state, report(check("tls", "error"), status="error", checked_at=500.0), config, now=500.0
        )
        assert retry is not None
        self.assertEqual(retry["kind"], "failure")

    def test_failed_delivery_of_changed_issue_is_not_throttled(self) -> None:
        config = settings(enabled=True, failure_threshold=1)
        state, event = tm.advance(None, report(check("cert", "warning"), status="warning"), config, now=1000.0)
        assert event is not None
        state = tm.record_delivery(state, event, success=False, now=1000.0)

        state, changed = tm.advance(
            state,
            report(check("cert", "warning"), check("tls", "error"), status="error", checked_at=1001.0),
            config,
            now=1001.0,
        )
        assert changed is not None
        self.assertEqual(changed["issue_ids"], ["cert", "tls"])

    def test_failed_recovery_is_retried_after_300_seconds(self) -> None:
        config = settings(enabled=True, failure_threshold=1)
        state, event = tm.advance(None, report(check("tls", "error"), status="error"), config, now=1000.0)
        assert event is not None
        state = tm.record_delivery(state, event, success=True, now=1000.0)

        state, recovery = tm.advance(state, report(checked_at=1100.0), config, now=1100.0)
        assert recovery is not None
        state = tm.record_delivery(state, recovery, success=False, now=1100.0)
        self.assertIsNotNone(state["alerted_signature"])

        for now in (1200.0, 1399.0):
            state, retry = tm.advance(state, report(checked_at=now), config, now=now)
            self.assertIsNone(retry)

        state, retry = tm.advance(state, report(checked_at=1400.0), config, now=1400.0)
        assert retry is not None
        self.assertEqual(retry["kind"], "recovery")

    def test_domain_change_resets_the_monitoring_object(self) -> None:
        config = settings(enabled=True, failure_threshold=1)
        state, event = tm.advance(None, report(check("tls", "error"), status="error"), config, now=1000.0)
        assert event is not None
        state = tm.record_delivery(state, event, success=True, now=1001.0)
        self.assertIsNotNone(state["alerted_signature"])

        state, event = tm.advance(
            state,
            report(domain="other.example.org", checked_at=1010.0),
            config,
            now=1010.0,
        )
        self.assertIsNone(event)
        self.assertEqual(state["domain"], "other.example.org")
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertIsNone(state["alerted_signature"])
        self.assertIsNone(state["last_attempt_at"])

    def test_safe_ids_and_no_raw_report_content_in_event_or_state(self) -> None:
        config = settings(enabled=True, failure_threshold=1, notify_command="/bin/secret-notifier")
        raw_id = "bad id\nhttps://private.example/token"
        raw = report(check(raw_id, "error"), status="error")
        state, event = tm.advance(None, raw, config, now=1000.0)
        assert event is not None
        self.assertEqual(len(event["issue_ids"]), 1)
        self.assertRegex(event["issue_ids"][0], r"\Aissue-[0-9a-f]{16}\Z")

        for payload in (json.dumps(event), json.dumps(state)):
            for forbidden in ("bad id", "private.example", "secret-token", "do-not-store", "/bin/secret-notifier"):
                self.assertNotIn(forbidden, payload)
        self.assertEqual(set(state), STATE_KEYS)

    def test_state_json_round_trip(self) -> None:
        config = settings(enabled=True, failure_threshold=1)
        state, event = tm.advance(None, report(check("tls", "error"), status="error"), config, now=1000.0)
        assert event is not None
        state = tm.record_delivery(state, event, success=True, now=1001.0)
        loaded = json.loads(json.dumps(state))
        next_state, next_event = tm.advance(
            loaded, report(check("tls", "error"), status="error", checked_at=1002.0), config, now=1002.0
        )
        self.assertIsNone(next_event)
        self.assertEqual(next_state["alerted_signature"], state["alerted_signature"])


class StateValidationTests(unittest.TestCase):
    def good_state(self) -> dict[str, object]:
        state, _ = tm.advance(None, report(check("tls", "ok")), settings(enabled=True), now=1000.0)
        return state

    def test_foreign_or_damaged_state_is_rejected_without_mutation(self) -> None:
        good = self.good_state()
        bad_states: list[object] = [[], "state", 1]
        bad_states.extend(
            [
                {**good, "managed_by": "Other"},
                {**good, "schema_version": 2},
                {**good, "schema_version": True},
                {**good, "consecutive_failures": True},
                {**good, "consecutive_failures": -1},
                {**good, "consecutive_failures": 1.5},
                {**good, "consecutive_failures": 10**7},
                {**good, "alerted_signature": "info|tls"},
                {**good, "alerted_signature": "error|bad id"},
                {**good, "last_level": "info"},
                {**good, "last_level": []},
                {**good, "last_checked_at": float("inf")},
                {**good, "last_checked_at": "1000"},
                {**good, "last_checked_at": "2026-11-14T05:33:21Z"},
                {**good, "last_attempt_at": 1000.0},
                {**good, "last_attempt_kind": "failure"},
                {**good, "last_attempt_succeeded": True},
                {**good, "last_attempt_at": 1000.0, "last_attempt_kind": "failure"},
                {**good, "last_attempt_at": 1000.0, "last_attempt_kind": []},
                {
                    **good,
                    "last_attempt_at": 1000.0,
                    "last_attempt_kind": "recovery",
                    "last_attempt_signature": "error|tls",
                },
                {
                    **good,
                    "last_attempt_at": math.inf,
                    "last_attempt_kind": "failure",
                    "last_attempt_signature": "error|tls",
                },
                {**good, "domain": "bad domain"},
                {**good, "domain": 1},
                {**good, "port": True},
                {**good, "port": 0},
                {**good, "port": 70000},
                {**good, "unknown": "field"},
            ]
        )
        missing = dict(good)
        missing.pop("last_level")
        bad_states.append(missing)

        for previous in bad_states:
            with self.subTest(previous=previous), self.assertRaises(ValueError) as caught:
                tm.advance(previous, report(), settings(enabled=True), now=1000.0)  # type: ignore[arg-type]
            self.assertEqual(str(caught.exception), tm.STATE_ERROR)

    def test_input_state_is_not_mutated(self) -> None:
        state = self.good_state()
        before = json.dumps(state, sort_keys=True)
        _, event = tm.advance(
            None, report(check("tls", "error"), status="error"), settings(enabled=True, failure_threshold=1), now=1.0
        )
        assert event is not None
        returned = tm.record_delivery(state, event, success=True, now=1001.0)
        self.assertEqual(json.dumps(state, sort_keys=True), before)
        self.assertIsNot(returned, state)


class RecordDeliveryValidationTests(unittest.TestCase):
    state: dict[str, object]
    event: dict[str, object]

    def setUp(self) -> None:
        state, event = tm.advance(
            None,
            report(check("tls", "error"), status="error"),
            settings(enabled=True, failure_threshold=1),
            now=1000.0,
        )
        assert event is not None
        self.state = state
        self.event = event

    def test_success_must_be_a_real_bool_and_now_finite(self) -> None:
        for success in (1, 0, "yes", None):
            with self.subTest(success=success), self.assertRaises(ValueError):
                tm.record_delivery(self.state, self.event, success=success, now=1000.0)  # type: ignore[arg-type]
        for now in (math.inf, math.nan, "1000"):
            with self.subTest(now=now), self.assertRaises(ValueError):
                tm.record_delivery(self.state, self.event, success=True, now=now)  # type: ignore[arg-type]

    def test_event_shape_is_validated(self) -> None:
        cases = [
            {**self.event, "extra": 1},
            {**self.event, "kind": "other"},
            {**self.event, "kind": []},
            {**self.event, "severity": "info"},
            {**self.event, "severity": []},
            {**self.event, "status": "bad"},
            {**self.event, "status": []},
            {**self.event, "issue_ids": []},
            {**self.event, "issue_ids": ["bad id"]},
            {**self.event, "issue_ids": ["z", "a"]},
            {**self.event, "domain": "bad domain"},
            {**self.event, "port": True},
            {**self.event, "checked_at": "2026-11-14T05:33:21Z"},
        ]
        for event in cases:
            with self.subTest(event=event), self.assertRaises(ValueError):
                tm.record_delivery(self.state, event, success=True, now=1000.0)

    def test_recovery_event_must_not_carry_issue_ids(self) -> None:
        state, recovery = tm.advance(
            tm.record_delivery(self.state, self.event, success=True, now=1000.0),
            report(checked_at=1010.0),
            settings(enabled=True, failure_threshold=1),
            now=1010.0,
        )
        assert recovery is not None
        with self.assertRaises(ValueError):
            tm.record_delivery(state, {**recovery, "issue_ids": ["tls"]}, success=True, now=1011.0)


if __name__ == "__main__":
    unittest.main()
