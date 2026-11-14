"""First-batch regressions; all services, package managers and network calls are mocked."""

from __future__ import annotations

import configparser
import contextlib
import errno
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import toastweb as tw


class PackageFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = tw.Paths(Path(temporary.name))
        self.state = {
            "managed_by": tw.OWNER,
            "domain": "docs.example.org",
            "port": 8443,
            "package_upgrade_restart": True,
        }
        self.save_state()
        tw.write_atomic(self.paths.config, tw.MARKER + "\nconfig\n")

    def save_state(self) -> None:
        tw.write_atomic(self.paths.state, json.dumps(self.state), 0o600)


class PackageRenderTests(PackageFixture):
    def test_restart_and_start_limits_are_in_the_correct_sections(self) -> None:
        unit = configparser.ConfigParser(interpolation=None)
        unit.optionxform = str  # type: ignore[method-assign]
        unit.read_string(tw.render_unit())
        self.assertEqual(unit["Unit"]["StartLimitIntervalSec"], "60s")
        self.assertEqual(unit["Unit"]["StartLimitBurst"], "5")
        self.assertEqual(unit["Service"]["Restart"], "on-failure")
        self.assertEqual(unit["Service"]["RestartSec"], "2s")
        self.assertEqual(unit["Service"]["TimeoutStartSec"], "30s")
        self.assertNotIn("StartLimitBurst", unit["Service"])
        self.assertNotIn("WatchdogSec", unit["Service"])

    def test_package_job_is_scoped_static_and_asynchronous(self) -> None:
        unit, hook = tw.render_package_unit(), tw.render_package_hook()
        for text in (unit, hook):
            self.assertIn(tw.MARKER, text)
            self.assertNotIn("xray", text.lower())
            self.assertNotIn("ssh", text.lower())
            self.assertNotIn("nginx.service", text)
        self.assertIn("Type=oneshot", unit)
        self.assertIn("toastweb package-refresh", unit)
        self.assertNotIn("[Install]", unit)
        self.assertNotIn("Requires=", unit)
        self.assertIn("DPkg::Post-Invoke", hook)
        self.assertIn("--no-block start " + tw.PACKAGE_SERVICE, hook)
        self.assertIn("logger -t toastweb", hook)
        self.assertIn("fi; true", hook)
        self.assertTrue(hook.startswith("// "))

    @unittest.skipUnless(shutil.which("apt-config"), "APT parser available only on Linux")
    def test_apt_accepts_generated_hook_without_executing_it(self) -> None:
        tw.write_atomic(self.paths.package_hook, tw.render_package_hook())
        result = subprocess.run(
            ["apt-config", "-c", str(self.paths.package_hook), "dump"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("DPkg::Post-Invoke", result.stdout)
        self.assertIn(tw.PACKAGE_SERVICE, result.stdout)

    def test_missing_policy_is_off_and_non_boolean_policy_is_rejected(self) -> None:
        self.state.pop("package_upgrade_restart")
        self.save_state()
        state = tw.load_state(self.paths)
        assert state is not None
        self.assertFalse(state["package_upgrade_restart"])
        for value in ("true", 1, None, []):
            with self.subTest(value=value):
                self.state["package_upgrade_restart"] = value
                self.save_state()
                with self.assertRaises(tw.ToastError):
                    tw.load_state(self.paths)

    def test_plan_and_cli_policy_are_explicit_and_non_mutating(self) -> None:
        for flag, expected in (
            ([], False),
            (["--package-upgrade-restart"], True),
            (["--no-package-upgrade-restart"], False),
        ):
            with (
                self.subTest(flag=flag),
                mock.patch.object(tw, "run") as runner,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    tw.main(["plan", "--domain", "docs.example.org", "--email", "ops@example.org", *flag]), 0
                )
                self.assertEqual(json.loads(output.getvalue())["package_upgrade_restart"], expected)
                runner.assert_not_called()
        self.assertIsNone(tw.parser().parse_args(["install"]).package_upgrade_restart)
        self.assertEqual(tw.parser().parse_args(["package-refresh"]).command, "package-refresh")

    def test_status_reports_stored_package_policy(self) -> None:
        with (
            mock.patch.object(tw, "run", return_value=subprocess.CompletedProcess([], 0, "active", "")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            tw.status(self.paths)
        self.assertTrue(json.loads(output.getvalue())["package_upgrade_restart"])

    def test_foreign_hook_cannot_be_removed_by_install_ownership_check(self) -> None:
        tw.write_atomic(self.paths.package_hook, "// administrator-owned\n")
        with self.assertRaises(tw.ToastError):
            tw.assert_owned(self.paths)
        self.assertEqual(self.paths.package_hook.read_text(), "// administrator-owned\n")


class PackageRuntimeTests(PackageFixture):
    def setUp(self) -> None:
        super().setUp()
        self.calls: list[list[str]] = []
        self.active = ("active", 321, "enabled")
        self.properties = [self.active, self.active, ("active", 654, "enabled")]
        self.detections = [True, False]
        self.restart_error = False
        self.config_error = False
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(tw, "require_host").start()
        self.lock = mock.patch.object(
            tw, "operation_lock", side_effect=lambda *a, **k: contextlib.nullcontext()
        ).start()
        self.busy = mock.patch.object(tw, "package_manager_busy", return_value=False).start()
        self.service = mock.patch.object(
            tw, "package_service_state", side_effect=lambda: self.properties.pop(0)
        ).start()
        self.detect = mock.patch.object(
            tw, "package_restart_needed", side_effect=lambda: self.detections.pop(0)
        ).start()
        self.probe = mock.patch.object(tw, "verify_package_backend").start()
        self.sleep = mock.patch.object(tw.time, "sleep").start()
        self.runner = mock.patch.object(tw, "run", side_effect=self.fake_run).start()
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def fake_run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["/usr/sbin/nginx", "-t"] and self.config_error:
            raise tw.ToastError("invalid new configuration")
        if args[:2] == ["systemctl", "try-restart"] and self.restart_error:
            raise tw.ToastError("restart failed")
        return subprocess.CompletedProcess(args, 0, "", "")

    def refresh(self) -> None:
        tw.package_refresh(self.paths)

    def assert_no_restart(self) -> None:
        self.assertFalse(any("try-restart" in args or "restart" in args for args in self.calls))

    def test_disabled_or_absent_state_changes_nothing(self) -> None:
        self.state["package_upgrade_restart"] = False
        self.save_state()
        self.refresh()
        self.paths.state.unlink()
        self.refresh()
        self.lock.assert_not_called()
        self.busy.assert_not_called()
        self.runner.assert_not_called()
        self.detect.assert_not_called()
        self.probe.assert_not_called()

    def test_inactive_failed_or_masked_service_is_never_started(self) -> None:
        for state in (("inactive", 0, "disabled"), ("failed", 0, "enabled"), ("active", 321, "masked")):
            with self.subTest(state=state):
                self.properties = [state]
                self.refresh()
        self.detect.assert_not_called()
        self.runner.assert_not_called()

    def test_unrelated_update_does_not_restart(self) -> None:
        self.detections = [False]
        self.refresh()
        self.assert_no_restart()
        self.probe.assert_not_called()

    def test_only_stale_active_toastweb_is_checked_restarted_and_verified(self) -> None:
        self.refresh()
        self.assertEqual(
            self.calls,
            [["/usr/sbin/nginx", "-t", "-c", str(self.paths.config)], ["systemctl", "try-restart", tw.SERVICE]],
        )
        self.assertEqual(self.detect.call_count, 2)
        self.probe.assert_called_once_with(tw.load_state(self.paths))
        self.assertIn("new master", self.output.getvalue())

    def test_invalid_configuration_leaves_old_master_untouched(self) -> None:
        self.config_error = True
        with self.assertRaises(tw.ToastError):
            self.refresh()
        self.assert_no_restart()
        self.probe.assert_not_called()

    def test_detection_error_does_not_restart(self) -> None:
        self.detect.side_effect = tw.ToastError("unknown update state")
        with self.assertRaises(tw.ToastError):
            self.refresh()
        self.assert_no_restart()

    def test_policy_rc_d_denial_is_respected(self) -> None:
        tw.write_atomic(self.paths.at("/usr/sbin/policy-rc.d"), "#!/bin/sh\nexit 101\n")
        self.runner.side_effect = lambda args, **kwargs: subprocess.CompletedProcess(args, 101, "", "")
        self.refresh()
        self.assertIn("policy-rc.d", self.output.getvalue())
        self.probe.assert_not_called()

    def test_concurrent_stop_or_restart_prevents_our_restart(self) -> None:
        for state in (("inactive", 0, "disabled"), ("active", 999, "enabled")):
            with self.subTest(state=state):
                self.properties = [self.active, state]
                self.detections = [True]
                self.refresh()
        self.assert_no_restart()

    def test_try_restart_failure_is_not_hidden_and_never_forces_a_start(self) -> None:
        self.restart_error = True
        with self.assertRaises(tw.ToastError):
            self.refresh()
        self.probe.assert_not_called()
        self.assertNotIn(["systemctl", "start", tw.SERVICE], self.calls)

    def test_missing_new_master_is_not_success(self) -> None:
        for state in (self.active, ("failed", 0, "enabled")):
            with self.subTest(state=state):
                self.properties = [self.active, self.active, state]
                self.detections = [True]
                with self.assertRaises(tw.ToastError):
                    self.refresh()
        self.probe.assert_not_called()

    def test_post_restart_https_failure_is_retried_but_not_hidden(self) -> None:
        self.probe.side_effect = tw.ToastError("TLS failed")
        with self.assertRaises(tw.ToastError):
            self.refresh()
        self.assertEqual(self.probe.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_transient_probe_failure_can_recover(self) -> None:
        self.probe.side_effect = [tw.ToastError("not ready"), None]
        self.refresh()
        self.assertEqual(self.probe.call_count, 2)

    def test_still_stale_after_restart_is_not_reported_as_patched(self) -> None:
        self.detections = [True, True]
        with self.assertRaisesRegex(tw.ToastError, "still reports"):
            self.refresh()

    def test_busy_package_manager_is_waited_for(self) -> None:
        self.busy.side_effect = [True, False, False, False, False]
        self.refresh()
        self.sleep.assert_called_once_with(2)

    def test_new_package_operation_before_restart_is_not_reported_as_clean(self) -> None:
        self.busy.side_effect = [False, False, True]
        with self.assertRaisesRegex(tw.ToastError, "became busy before restart"):
            self.refresh()
        self.assert_no_restart()

    def test_package_operation_after_restart_is_not_reported_as_clean(self) -> None:
        self.busy.side_effect = [False, False, False, True]
        with self.assertRaisesRegex(tw.ToastError, "Another package operation"):
            self.refresh()

    def test_busy_package_manager_timeout_never_restarts(self) -> None:
        self.busy.return_value = True
        with mock.patch.object(tw.time, "monotonic", side_effect=[0, 901]), self.assertRaises(tw.ToastError):
            self.refresh()
        self.assert_no_restart()
        self.service.assert_not_called()


class PackageHelperTests(PackageFixture):
    def test_needrestart_is_list_only_and_ignores_other_services(self) -> None:
        output = "NEEDRESTART-VER: 3.11\nNEEDRESTART-SVC: ssh.service\nNEEDRESTART-SVC: nginx.service\n"
        with mock.patch.object(tw, "run", return_value=subprocess.CompletedProcess([], 0, output, "")) as runner:
            self.assertFalse(tw.package_restart_needed())
            self.assertEqual(runner.call_args.args[0], ["/usr/sbin/needrestart", "-b", "-r", "l", "-l"])
            self.assertEqual(runner.call_args.kwargs["env"]["NEEDRESTART_MODE"], "l")
        output += "NEEDRESTART-SVC: " + tw.SERVICE + "\n"
        with mock.patch.object(tw, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
            self.assertTrue(tw.package_restart_needed())

    def test_invalid_needrestart_output_or_exit_is_unknown_not_clean(self) -> None:
        for code, output in ((1, "NEEDRESTART-VER: 3.11\n"), (0, ""), (0, "ordinary text")):
            with (
                self.subTest(code=code, output=output),
                mock.patch.object(tw, "run", return_value=subprocess.CompletedProcess([], code, output, "")),
                self.assertRaises(tw.ToastError),
            ):
                tw.package_restart_needed()

    def test_systemd_properties_are_parsed_or_fail_closed(self) -> None:
        output = "MainPID=321\nActiveState=active\nUnitFileState=enabled\n"
        with mock.patch.object(tw, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
            self.assertEqual(tw.package_service_state(), ("active", 321, "enabled"))
        for output in ("", "ActiveState=active\nMainPID=oops\nUnitFileState=enabled"):
            with (
                mock.patch.object(tw, "run", return_value=subprocess.CompletedProcess([], 0, output, "")),
                self.assertRaises(tw.ToastError),
            ):
                tw.package_service_state()

    def test_package_locks_use_record_locks_and_are_not_retained(self) -> None:
        names = (
            "/var/lib/dpkg/lock-frontend",
            "/var/lib/dpkg/lock",
            "/var/lib/apt/lists/lock",
            "/var/cache/apt/archives/lock",
        )
        for name in names:
            tw.write_atomic(self.paths.at(name), "")
        fcntl = mock.Mock(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        with mock.patch.object(tw, "import_module", return_value=fcntl):
            self.assertFalse(tw.package_manager_busy(self.paths))
            self.assertEqual([call.args[1] for call in fcntl.lockf.call_args_list], [6, 8, 6, 8, 6, 8, 6, 8])
            fcntl.flock.assert_not_called()
            for target in names[2:]:

                def lockf(stream: object, operation: int, *, target: str = target) -> None:
                    name = str(getattr(stream, "name", "")).replace("\\", "/")
                    if operation != fcntl.LOCK_UN and name.endswith(target):
                        raise BlockingIOError(errno.EAGAIN, "locked")

                fcntl.lockf.side_effect = lockf
                self.assertTrue(tw.package_manager_busy(self.paths))
            fcntl.lockf.side_effect = BlockingIOError(errno.EAGAIN, "locked")
            self.assertTrue(tw.package_manager_busy(self.paths))
            fcntl.lockf.side_effect = OSError(errno.EIO, "I/O error")
            with self.assertRaises(OSError):
                tw.package_manager_busy(self.paths)

    def test_operation_lock_is_nonblocking_and_raises_distinct_error(self) -> None:
        fcntl = mock.Mock(LOCK_EX=2, LOCK_NB=4)
        fcntl.flock.side_effect = BlockingIOError()
        with mock.patch.object(tw, "import_module", return_value=fcntl):
            with self.assertRaises(tw.OperationBusy):
                with tw.operation_lock(self.paths):
                    self.fail("Lock should not be acquired")
        fcntl.flock.assert_called_once()

    def test_package_refresh_lock_waits_without_holding_project_lock(self) -> None:
        events: list[str] = []

        @contextlib.contextmanager
        def tracked_lock(paths: tw.Paths):
            events.append("lock-enter")
            try:
                yield
            finally:
                events.append("lock-exit")

        with (
            mock.patch.object(tw, "operation_lock", tracked_lock),
            mock.patch.object(tw, "package_manager_busy", side_effect=[True, False, False]),
            mock.patch.object(tw.time, "sleep", side_effect=lambda delay: events.append("wait")) as sleep,
        ):
            with tw.package_refresh_lock(self.paths):
                events.append("body")
        self.assertEqual(events, ["wait", "lock-enter", "body", "lock-exit"])
        sleep.assert_called_once_with(2)

    def test_package_refresh_lock_releases_project_lock_if_package_manager_busy(self) -> None:
        events: list[str] = []

        @contextlib.contextmanager
        def tracked_lock(paths: tw.Paths):
            events.append("lock-enter")
            try:
                yield
            finally:
                events.append("lock-exit")

        with (
            mock.patch.object(tw, "operation_lock", tracked_lock),
            mock.patch.object(tw, "package_manager_busy", side_effect=[False, True, False, False]),
            mock.patch.object(tw.time, "sleep") as sleep,
        ):
            with tw.package_refresh_lock(self.paths):
                events.append("body")
        self.assertEqual(events, ["lock-enter", "lock-exit", "lock-enter", "body", "lock-exit"])
        sleep.assert_called_once_with(2)

    def test_needrestart_dependency_is_opt_in_and_install_guard_is_cleaned(self) -> None:
        calls: list[list[str]] = []

        def fake(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[0] == "dpkg-query":
                return subprocess.CompletedProcess(args, 1 if args[-1] == "needrestart" else 0, "ii ", "")
            return subprocess.CompletedProcess(args, 0, "loaded", "")

        with mock.patch.object(tw, "run", side_effect=fake):
            tw.ensure_dependencies(self.paths)
            self.assertFalse(any("needrestart" in args for args in calls))
            tw.ensure_dependencies(self.paths, package_upgrade_restart=True)
        installs = [args for args in calls if args[:2] == ["apt-get", "install"]]
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0][-1], "needrestart")
        self.assertFalse(self.paths.at("/usr/sbin/policy-rc.d").exists())

    def test_https_probe_checks_trust_sni_tls13_and_http_status(self) -> None:
        with (
            mock.patch.object(tw.ssl, "create_default_context") as factory,
            mock.patch.object(tw.socket, "create_connection") as connect,
            mock.patch.object(tw.http.client, "HTTPResponse") as response,
        ):
            response.return_value.__enter__.return_value.status = 200
            tw.verify_package_backend(self.state)
            context = factory.return_value
            self.assertEqual(context.minimum_version, tw.ssl.TLSVersion.TLSv1_3)
            self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "docs.example.org")
            self.assertEqual(connect.call_args.args[0], ("127.0.0.1", 8443))
            tls = context.wrap_socket.return_value.__enter__.return_value
            self.assertIn(b"HEAD / HTTP/1.1", tls.sendall.call_args.args[0])
            response.return_value.__enter__.return_value.status = 404
            with self.assertRaises(tw.ToastError):
                tw.verify_package_backend(self.state)
            connect.side_effect = tw.ssl.SSLError("bad certificate")
            with self.assertRaises(tw.ToastError):
                tw.verify_package_backend(self.state)


class PackageInstallTests(PackageFixture):
    def setUp(self) -> None:
        super().setUp()
        self.paths.state.unlink()
        self.args = tw.parser().parse_args(
            [
                "install",
                "--domain",
                "docs.example.org",
                "--email",
                "ops@example.org",
                "--accept-tos",
                "--confirm-port-80",
                "--non-interactive",
            ]
        )
        self.addCleanup(mock.patch.stopall)
        for name in (
            "require_host",
            "check_ports",
            "check_other_acme",
            "http_probe",
            "install_bundle",
            "activate_release",
            "post_success_cleanup",
        ):
            mock.patch.object(tw, name).start()
        self.dependencies = mock.patch.object(tw, "ensure_dependencies").start()
        mock.patch.object(tw, "create_release", return_value="test_release").start()
        mock.patch.object(tw, "operation_lock", side_effect=lambda *a, **k: contextlib.nullcontext()).start()
        mock.patch.object(tw, "check_dns", return_value={"192.0.2.1"}).start()
        mock.patch.object(tw, "modern_nginx", return_value=True).start()
        mock.patch.object(tw, "ipv6_available", return_value=False).start()
        self.runner = mock.patch.object(tw, "run", side_effect=self.fake_run).start()
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def fake_run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["certbot", "certonly"]:
            tw.write_atomic(self.paths.lineage("docs.example.org") / "fullchain.pem", "test certificate")
            tw.write_atomic(self.paths.lineage("docs.example.org") / "privkey.pem", "test key", 0o600)
        code = 3 if args[:2] in (["systemctl", "is-active"], ["systemctl", "is-enabled"]) else 0
        return subprocess.CompletedProcess(args, code, "", "")

    def test_default_off_opt_in_persistence_and_explicit_disable(self) -> None:
        tw.install(self.args, self.paths)
        state = tw.load_state(self.paths)
        assert state is not None
        self.assertFalse(state["package_upgrade_restart"])
        self.assertFalse(self.paths.package_hook.exists())
        self.dependencies.assert_called_with(self.paths, package_upgrade_restart=False)
        self.args.package_upgrade_restart = True
        tw.install(self.args, self.paths)
        self.assertTrue(self.paths.package_hook.exists())
        self.assertTrue(self.paths.package_unit.exists())
        self.dependencies.assert_called_with(self.paths, package_upgrade_restart=True)
        self.args.package_upgrade_restart = None
        tw.install(self.args, self.paths)
        state = tw.load_state(self.paths)
        assert state is not None
        self.assertTrue(state["package_upgrade_restart"])
        self.args.package_upgrade_restart = False
        tw.install(self.args, self.paths)
        state = tw.load_state(self.paths)
        assert state is not None
        self.assertFalse(state["package_upgrade_restart"])
        self.assertFalse(self.paths.package_hook.exists())
        self.assertFalse(self.paths.package_unit.exists())
        self.assertFalse(
            any("remove" in call.args[0] or "purge" in call.args[0] for call in self.runner.call_args_list)
        )

    def test_post_commit_scheduling_failure_warns_without_undoing_site(self) -> None:
        self.args.package_upgrade_restart = True

        def fail_scheduling(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args == ["systemctl", "--no-block", "start", tw.PACKAGE_SERVICE]:
                raise tw.ToastError("systemctl unavailable after commit")
            return self.fake_run(args, **kwargs)

        self.runner.side_effect = fail_scheduling
        with contextlib.redirect_stderr(io.StringIO()) as output:
            tw.install(self.args, self.paths)
        state = tw.load_state(self.paths)
        assert state is not None
        self.assertTrue(state["package_upgrade_restart"])
        self.assertTrue(self.paths.package_hook.exists())
        self.assertIn("site installed", output.getvalue())

    def test_hook_change_failure_restores_owned_hook_unit_and_policy(self) -> None:
        self.args.package_upgrade_restart = True
        tw.install(self.args, self.paths)
        before = (
            self.paths.state.read_bytes(),
            self.paths.package_hook.read_bytes(),
            self.paths.package_unit.read_bytes(),
        )
        original = tw.configure_package_refresh

        def fail(paths: tw.Paths, enabled: bool) -> None:
            original(paths, enabled)
            raise tw.ToastError("simulated failure after hook removal")

        self.args.package_upgrade_restart = False
        with mock.patch.object(tw, "configure_package_refresh", side_effect=fail), self.assertRaises(tw.ToastError):
            tw.install(self.args, self.paths)
        self.assertEqual(
            before,
            (self.paths.state.read_bytes(), self.paths.package_hook.read_bytes(), self.paths.package_unit.read_bytes()),
        )


if __name__ == "__main__":
    unittest.main()
