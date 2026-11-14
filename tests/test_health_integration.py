"""Local integration of diagnostics, monitor persistence and CLI; no real services/network."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import toastweb as tw
import toastweb_monitor as monitoring


class HealthIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = tw.Paths(Path(temporary.name))
        self.state = {
            "managed_by": tw.OWNER,
            "domain": "docs.example.org",
            "port": 8443,
            "monitor": dict(monitoring.DEFAULT_MONITOR, enabled=True, failure_threshold=2),
        }
        self.save_state()
        tw.write_atomic(self.paths.config, tw.MARKER + "\nfixture config\n")
        self.calls: list[list[str]] = []
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(tw, "require_host").start()
        mock.patch.object(tw, "operation_lock", side_effect=lambda *a, **k: contextlib.nullcontext()).start()
        self.health_lock = mock.patch.object(
            tw, "health_lock", side_effect=lambda *a, **k: contextlib.nullcontext()
        ).start()
        mock.patch.object(tw, "post_success_cleanup").start()
        self.runner = mock.patch.object(tw, "run", side_effect=self.fake_run).start()
        self.collect = mock.patch.object(tw, "collect_diagnostics", side_effect=lambda *a, **k: self.report()).start()
        self.delivery = mock.patch.object(tw, "deliver_health_event", return_value=True).start()
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def save_state(self) -> None:
        tw.write_atomic(self.paths.state, json.dumps(self.state), 0o600)

    def report(self, level: str = "ok") -> dict:
        return {
            "schema_version": 1,
            "domain": self.state["domain"],
            "port": 8443,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": level,
            "scope": "local_backend",
            "checks": [
                {"id": "backend_tls", "level": level, "critical": True, "message": "Safe summary", "details": {}}
            ],
            "not_tested": ["public_https_443", "reality_authentication"],
        }

    def fake_run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        code = 3 if args[:2] in (["systemctl", "is-active"], ["systemctl", "is-enabled"]) else 0
        return subprocess.CompletedProcess(args, code, "", "")

    def check(self) -> int:
        return tw.monitor_check(self.paths, json_output=True)

    def assert_no_website_mutation(self) -> None:
        self.assertFalse(any(args[0] == "/usr/sbin/nginx" for args in self.calls))
        self.assertFalse(any(tw.SERVICE in args or tw.RENEW_TIMER in args for args in self.calls))

    def test_doctor_json_is_read_only_and_exit_code_is_preserved(self) -> None:
        before = self.paths.state.read_bytes()
        self.collect.return_value = self.report("unknown")
        self.collect.side_effect = None
        self.assertEqual(tw.doctor(self.paths, json_output=True), 2)
        self.assertEqual(json.loads(self.output.getvalue())["status"], "unknown")
        self.assertEqual(before, self.paths.state.read_bytes())
        self.assertFalse(self.paths.health_state.exists())
        self.runner.assert_not_called()

    def test_doctor_missing_installation_returns_json_error_without_exception_leak(self) -> None:
        self.paths.state.unlink()
        self.assertEqual(tw.doctor(self.paths, json_output=True), 1)
        result = json.loads(self.output.getvalue())
        self.assertEqual(result["checks"][0]["id"], "installation_state")
        self.assertNotIn(str(self.paths.root), self.output.getvalue())

    def test_doctor_cli_forwards_explicit_dns_and_threshold_options(self) -> None:
        with (
            mock.patch.object(tw, "Paths", return_value=self.paths),
            mock.patch.object(tw, "doctor", return_value=2) as doctor,
        ):
            self.assertEqual(tw.main(["doctor", "--json", "--dns-checks", "--warn-cert-days", "12"]), 2)
        doctor.assert_called_once_with(self.paths, json_output=True, dns_checks=True, warn_cert_days=12)

    def test_default_monitor_is_disabled_in_old_state(self) -> None:
        self.state.pop("monitor")
        self.save_state()
        loaded = tw.load_state(self.paths)
        assert loaded is not None
        self.assertFalse(loaded["monitor"]["enabled"])
        self.assertEqual(self.check(), 0)
        self.collect.assert_not_called()
        self.health_lock.assert_not_called()
        self.assertFalse(self.paths.health_state.exists())

    def test_continuous_failures_dedup_and_recovery_use_real_state_machine(self) -> None:
        self.collect.side_effect = lambda *a, **k: self.report("error")
        self.assertEqual(self.check(), 1)
        self.delivery.assert_not_called()
        self.assertEqual(self.check(), 1)
        self.assertEqual(self.check(), 1)
        self.assertEqual(self.delivery.call_count, 1)
        event = self.delivery.call_args.args[2]
        self.assertEqual(event["kind"], "failure")
        self.assertNotIn("details", event)
        self.assertNotIn("message", event)
        self.collect.side_effect = lambda *a, **k: self.report()
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.delivery.call_count, 2)
        self.assertEqual(self.delivery.call_args.args[2]["kind"], "recovery")
        self.assert_no_website_mutation()

    def test_certificate_warning_can_alert_without_declaring_doctor_failure(self) -> None:
        self.collect.side_effect = lambda *a, **k: self.report("warning")
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.delivery.call_count, 1)
        self.assertEqual(self.delivery.call_args.args[2]["severity"], "warning")

    def test_delivery_failure_remains_pending_and_is_retried_after_cooldown(self) -> None:
        self.state["monitor"]["failure_threshold"] = 1
        self.save_state()
        self.collect.side_effect = lambda *a, **k: self.report("error")
        self.delivery.side_effect = [False, True]
        with mock.patch.object(tw.time, "time", side_effect=[1000.0, 1120.0, 1320.0]):
            self.check()
            self.assertEqual(self.delivery.call_count, 1)
            self.check()
            self.assertEqual(self.delivery.call_count, 1)
            self.check()
        self.assertEqual(self.delivery.call_count, 2)
        saved = tw.read_json(self.paths.health_state)
        self.assertTrue(saved["last_attempt_succeeded"])
        self.assertIsNotNone(saved["alerted_signature"])

    def test_foreign_runtime_state_is_never_overwritten(self) -> None:
        tw.write_atomic(self.paths.health_state, '{"managed_by":"somebody else","schema_version":1}')
        before = self.paths.health_state.read_bytes()
        with self.assertRaises(ValueError):
            self.check()
        self.assertEqual(before, self.paths.health_state.read_bytes())
        self.delivery.assert_not_called()

    def test_monitor_settings_validation_is_strict_in_site_state(self) -> None:
        for value in (None, [], {"enabled": "true"}, {"interval_seconds": True}, {"unknown": 1}):
            with self.subTest(value=value):
                self.state["monitor"] = value
                self.save_state()
                with self.assertRaises(ValueError):
                    tw.load_state(self.paths)

    def test_enable_disable_only_manages_owned_probe_units_and_site_settings(self) -> None:
        self.state["monitor"]["enabled"] = False
        self.save_state()
        args = tw.parser().parse_args(["monitor", "enable", "--interval", "90", "--failures", "3"])
        tw.set_monitor(args, self.paths)
        self.assertTrue(self.paths.health_unit.exists())
        self.assertIn("OnUnitInactiveSec=90s", self.paths.health_timer.read_text())
        loaded = tw.load_state(self.paths)
        assert loaded is not None
        self.assertTrue(loaded["monitor"]["enabled"])
        tw.set_monitor(tw.parser().parse_args(["monitor", "disable"]), self.paths)
        self.assertFalse(self.paths.health_unit.exists())
        self.assertFalse(self.paths.health_timer.exists())
        self.assert_no_website_mutation()

    def test_failed_enable_restores_policy_and_units_without_reloading_website(self) -> None:
        self.state["monitor"]["enabled"] = False
        self.save_state()
        before = self.paths.state.read_bytes(), self.paths.config.read_bytes()

        def fail(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args == ["systemctl", "enable", "--now", tw.HEALTH_TIMER]:
                self.calls.append(args)
                raise tw.ToastError("simulated timer start failure")
            return self.fake_run(args, **kwargs)

        self.runner.side_effect = fail
        with self.assertRaises(tw.ToastError):
            tw.set_monitor(tw.parser().parse_args(["monitor", "enable"]), self.paths)
        self.assertEqual(before, (self.paths.state.read_bytes(), self.paths.config.read_bytes()))
        self.assertFalse(self.paths.health_unit.exists())
        self.assertFalse(self.paths.health_timer.exists())
        self.assert_no_website_mutation()

    def test_running_probe_stop_failure_aborts_without_mutating_website(self) -> None:
        before = self.paths.state.read_bytes()

        def fail(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] in (["systemctl", "is-active"], ["systemctl", "is-enabled"]):
                return subprocess.CompletedProcess(args, 0, "", "")
            if args == ["systemctl", "stop", tw.HEALTH_TIMER]:
                self.assertTrue(kwargs.get("check"))
                raise tw.ToastError("could not stop the active probe timer")
            return subprocess.CompletedProcess(args, 0, "", "")

        self.runner.side_effect = fail
        with self.assertRaises(tw.ToastError):
            tw.set_monitor(tw.parser().parse_args(["monitor", "disable"]), self.paths)
        self.assertEqual(before, self.paths.state.read_bytes())
        self.assert_no_website_mutation()

    def test_disabled_policy_reconciles_only_previously_owned_runtime_units(self) -> None:
        tw.configure_health_files(self.paths, dict(monitoring.DEFAULT_MONITOR))
        self.runner.assert_not_called()
        tw.configure_health_files(self.paths, dict(monitoring.DEFAULT_MONITOR, enabled=True))
        tw.configure_health_files(self.paths, dict(monitoring.DEFAULT_MONITOR))
        self.assertFalse(self.paths.health_timer.exists())
        self.assertIn(["systemctl", "stop", tw.HEALTH_TIMER], self.calls)
        self.assertIn(["systemctl", "disable", tw.HEALTH_TIMER], self.calls)
        self.assert_no_website_mutation()

    @unittest.skipUnless(os.name == "posix" and shutil.which("systemd-analyze"), "Linux systemd unit syntax verifier")
    def test_health_unit_templates_pass_systemd_verify_without_activation(self) -> None:
        executable = self.paths.root / "noop"
        tw.write_atomic(executable, "#!/bin/sh\nexit 0\n", 0o755)
        service = self.paths.root / tw.HEALTH_SERVICE
        timer = self.paths.root / tw.HEALTH_TIMER
        tw.write_atomic(
            service, monitoring.render_health_unit(tw.MARKER).replace("/usr/local/bin/toastweb", str(executable))
        )
        tw.write_atomic(timer, monitoring.render_health_timer(tw.MARKER, 120))
        result = subprocess.run(
            ["systemd-analyze", "verify", str(service), str(timer)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0)

    def test_foreign_probe_unit_is_not_replaced_or_removed(self) -> None:
        tw.write_atomic(self.paths.health_unit, "# existing administrator unit\n")
        with self.assertRaises(tw.ToastError):
            tw.set_monitor(tw.parser().parse_args(["monitor", "disable"]), self.paths)
        self.assertEqual(self.paths.health_unit.read_text(), "# existing administrator unit\n")

    def test_status_does_not_export_notification_command(self) -> None:
        self.state["monitor"]["notify_command"] = "/etc/toastweb/private-name.sh"
        self.save_state()
        tw.monitor_status(self.paths)
        result = json.loads(self.output.getvalue())
        self.assertTrue(result["settings"]["notifier_configured"])
        self.assertNotIn("private-name", self.output.getvalue())

    def test_status_rejects_malformed_owned_metadata_instead_of_echoing_it(self) -> None:
        tw.write_atomic(
            self.paths.health_state,
            json.dumps({"managed_by": tw.OWNER, "schema_version": 1, "last_level": "not-a-real-level"}),
        )
        with self.assertRaises(ValueError):
            tw.monitor_status(self.paths)
        self.assertNotIn("not-a-real-level", self.output.getvalue())

    def test_single_probe_does_not_enable_timer_or_run_mutating_service_commands(self) -> None:
        self.check()
        self.assertEqual(self.calls, [])
        self.collect.assert_called_once()
        self.assertFalse(self.collect.call_args.kwargs.get("dns_checks", False))

    def test_notification_delivery_uses_stdin_no_shell_and_discards_output(self) -> None:
        # Exercise the real delivery function even though other integration tests mock it.
        with (
            mock.patch.object(tw, "validate_notifier", return_value=self.paths.root / "notifier") as validate,
            mock.patch.object(tw.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as execute,
        ):
            result = REAL_DELIVER(
                self.paths,
                {"notify_command": "/etc/toastweb/notify.sh"},
                {"kind": "test", "domain": "docs.example.org"},
            )
            self.assertTrue(result)
            self.assertEqual(execute.call_args.args[0], [str(self.paths.root / "notifier")])
            self.assertNotIn("shell", execute.call_args.kwargs)
            self.assertEqual(execute.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(execute.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(execute.call_args.kwargs["timeout"], 10)
            self.assertNotIn("notify.sh", execute.call_args.kwargs["input"])
            validate.assert_called_once()

    def test_journal_delivery_never_executes_an_external_command(self) -> None:
        with mock.patch.object(tw.subprocess, "run") as execute:
            self.assertTrue(REAL_DELIVER(self.paths, {"notify_command": None}, {"kind": "test"}))
            execute.assert_not_called()

    def test_notifier_timeout_or_invalid_path_is_not_success(self) -> None:
        with (
            mock.patch.object(tw, "validate_notifier", side_effect=tw.ToastError("unsafe")),
            mock.patch.object(tw.subprocess, "run") as execute,
        ):
            self.assertFalse(REAL_DELIVER(self.paths, {"notify_command": "/etc/unsafe"}, {}))
            execute.assert_not_called()
        with (
            mock.patch.object(tw, "validate_notifier", return_value=self.paths.root / "notifier"),
            mock.patch.object(tw.subprocess, "run", side_effect=subprocess.TimeoutExpired("notifier", 10)),
        ):
            self.assertFalse(REAL_DELIVER(self.paths, {"notify_command": "/etc/notify"}, {}))

    def test_notification_command_path_and_ownership_checks_fail_closed(self) -> None:
        for value in ("relative", "/etc/../root/notify", "/root/notify", "/home/user/notify", "/etc/notify\x00"):
            with self.subTest(value=value), self.assertRaises(tw.ToastError):
                tw.validate_notifier(self.paths, value)
        metadata = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o700)
        with mock.patch.object(Path, "stat", return_value=metadata):
            self.assertEqual(tw.validate_notifier(self.paths, "/etc/notify"), self.paths.at("/etc/notify"))
            metadata.st_uid = 1000
            with self.assertRaises(tw.ToastError):
                tw.validate_notifier(self.paths, "/etc/notify")
            metadata.st_uid = 0
            metadata.st_mode |= stat.S_IWGRP
            with self.assertRaises(tw.ToastError):
                tw.validate_notifier(self.paths, "/etc/notify")


REAL_DELIVER = tw.deliver_health_event


if __name__ == "__main__":
    unittest.main()
