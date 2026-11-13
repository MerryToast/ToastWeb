"""Review regressions: no production network, CA, systemd, DNS or filesystem changes."""

from __future__ import annotations

import argparse
import contextlib
import http.client
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import toastweb as tw


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = tw.Paths(Path(temporary.name))
        self.connections: list[mock.Mock] = []
        self.errors: dict[str, Exception] = {}
        self.responses: dict[str, tuple[int, bytes]] = {}
        self.request_errors: dict[str, Exception] = {}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(tw, "local_addresses", return_value={"127.0.0.1", "192.0.2.10"}).start()
        mock.patch.object(tw.http.client, "HTTPConnection", side_effect=self.connection).start()

    def connection(self, address: str, port: int, timeout: int) -> mock.Mock:
        self.assertEqual(port, 80)
        connection = mock.Mock()
        connection.connect.side_effect = self.errors.get(address)
        connection.request.side_effect = self.request_errors.get(address)
        response = mock.Mock()
        response.status = self.responses.get(address, (200, b""))[0]
        response.read.side_effect = lambda size: (
            self.responses[address][1]
            if address in self.responses
            else next((self.paths.acme / ".well-known/acme-challenge").iterdir()).read_bytes()
        )
        connection.getresponse.return_value = response
        self.connections.append(connection)
        return connection

    def probe(self, address: str, expected: list[str] | None = None) -> None:
        tw.http_probe(self.paths, "docs.example.org", {address}, expected)

    def assert_challenge_removed(self) -> None:
        directory = self.paths.acme / ".well-known/acme-challenge"
        self.assertEqual(list(directory.iterdir()) if directory.exists() else [], [])
        self.assertTrue(all(connection.close.called for connection in self.connections))

    def test_explicit_nonlocal_connection_failure_warns(self) -> None:
        self.errors["198.51.100.1"] = TimeoutError()
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.probe("198.51.100.1", ["198.51.100.1"])
        self.assertIn("the CA must still validate", output.getvalue())
        self.assert_challenge_removed()

    def test_loopback_and_local_addresses_remain_strict(self) -> None:
        for address in ("127.0.0.1", "192.0.2.10"):
            with self.subTest(address=address):
                self.errors = {address: ConnectionRefusedError()}
                with self.assertRaises(tw.ToastError):
                    self.probe(address, [address])
                self.assert_challenge_removed()

    def test_implicit_nonlocal_address_remains_strict(self) -> None:
        self.errors["198.51.100.1"] = OSError("unreachable")
        with self.assertRaises(tw.ToastError):
            self.probe("198.51.100.1")
        self.assert_challenge_removed()

    def test_nat_http_error_and_token_mismatch_are_fatal(self) -> None:
        for status, body in ((404, b"missing"), (200, b"wrong"), (301, b"redirect")):
            with self.subTest(status=status):
                self.responses["198.51.100.1"] = status, body
                with self.assertRaises(tw.ToastError):
                    self.probe("198.51.100.1", ["198.51.100.1"])
                self.assert_challenge_removed()

    def test_connected_nat_endpoint_protocol_failure_remains_fatal(self) -> None:
        for error in (TimeoutError(), http.client.BadStatusLine("bad response")):
            with self.subTest(error=error):
                self.request_errors["198.51.100.1"] = error
                with self.assertRaises(tw.ToastError):
                    self.probe("198.51.100.1", ["198.51.100.1"])
                self.assert_challenge_removed()

    def test_invalid_expected_ip_creates_no_challenge(self) -> None:
        with self.assertRaises(ValueError):
            self.probe("198.51.100.1", ["invalid"])
        self.assert_challenge_removed()


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = tw.Paths(Path(temporary.name))
        for number in range(1, 9):
            self.release(number)
        self.state = {
            "managed_by": tw.OWNER,
            "domain": "docs.example.org",
            "port": 8443,
            "active_release": self.identifier(8),
            "previous_release": self.identifier(7),
        }
        self.save_state()

    @staticmethod
    def identifier(number: int) -> str:
        return f"20260101T000000-{number:08x}"

    def save_state(self) -> None:
        tw.write_atomic(self.paths.state, json.dumps(self.state), 0o600)

    def release(self, number: int, *, registered: bool = True) -> Path:
        identifier = self.identifier(number)
        directory = self.paths.releases / identifier
        tw.write_atomic(directory / "index.html", "static content")
        if registered:
            tw.write_atomic(
                self.paths.release_records / (identifier + ".json"),
                json.dumps({"managed_by": tw.OWNER, "release": identifier}),
                0o600,
            )
        os.utime(directory, ns=(number * 10**9, number * 10**9))
        return directory

    def backup(self, number: int, refs: list[str], *, legacy: bool = False) -> Path:
        directory = self.paths.backups / self.identifier(number)
        if legacy:
            tw.write_atomic(directory / "1", json.dumps({"managed_by": tw.OWNER, "active_release": refs[0]}), 0o600)
        else:
            tw.write_atomic(
                directory / "manifest.json", json.dumps({"managed_by": tw.OWNER, "release_refs": refs}), 0o600
            )
        os.utime(directory, ns=(number * 10**9, number * 10**9))
        return directory

    def symlink(self, target: Path, link: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError:
            self.skipTest("OS does not allow this user to create symlinks")

    def test_preview_then_cleanup_preserves_active_previous_and_backup_dependencies(self) -> None:
        old_backup = self.backup(1, [self.identifier(3)])
        retained_backup = self.backup(2, [self.identifier(1)])
        legacy = self.backup(0, [self.identifier(2)], legacy=True)
        unregistered = self.release(9, registered=False)
        plan = tw.prune_history(self.paths, 1, 1)
        self.assertFalse(plan["applied"])
        self.assertTrue(old_backup.exists())
        self.assertEqual(set(plan["remove_releases"]), {self.identifier(i) for i in (3, 4, 5, 6)})
        self.assertEqual(plan["remove_backups"], [old_backup.name])
        self.assertEqual(set(plan["protected_releases"]), {self.identifier(i) for i in (1, 2, 7, 8)})
        result = tw.prune_history(self.paths, 1, 1, apply=True)
        self.assertTrue(result["applied"])
        self.assertFalse(old_backup.exists())
        self.assertTrue(retained_backup.exists())
        self.assertTrue(legacy.exists())
        self.assertTrue(unregistered.exists())
        for identifier in plan["remove_releases"]:
            self.assertFalse((self.paths.releases / identifier).exists())
            self.assertFalse((self.paths.release_records / (identifier + ".json")).exists())

    def test_current_pointer_is_protected_even_if_state_differs(self) -> None:
        self.paths.current.parent.mkdir(parents=True, exist_ok=True)
        target = self.paths.releases / self.identifier(1)
        self.symlink(target, self.paths.current)
        self.assertNotIn(target.name, tw.prune_history(self.paths, 1, 1)["remove_releases"])

    def test_nested_symlink_prevents_all_deletion(self) -> None:
        old_backup = self.backup(1, [])
        self.backup(2, [])
        outside = self.paths.root / "outside.txt"
        outside.write_text("preserve", encoding="utf-8")
        candidate = self.paths.releases / self.identifier(1)
        self.symlink(outside, candidate / "link")
        os.utime(candidate, ns=(10**9, 10**9))
        with self.assertRaises(tw.ToastError):
            tw.prune_history(self.paths, 1, 1, apply=True)
        self.assertTrue(old_backup.exists())
        self.assertEqual(outside.read_text(), "preserve")

    def test_top_level_symlink_is_never_followed_or_deleted(self) -> None:
        outside = self.paths.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("preserve")
        link = self.paths.releases / self.identifier(10)
        self.symlink(outside, link)
        tw.prune_history(self.paths, 1, 1, apply=True)
        self.assertTrue(link.is_symlink())
        self.assertTrue((outside / "keep").exists())

    def test_manifest_and_registry_symlinks_fail_closed(self) -> None:
        backup = self.backup(1, [])
        marker = backup / "manifest.json"
        marker.unlink()
        self.symlink(self.paths.state, marker)
        with self.assertRaises(tw.ToastError):
            tw.prune_history(self.paths, 1, 1, apply=True)
        self.assertTrue((self.paths.releases / self.identifier(1)).exists())

    def test_mismatched_ownership_record_fails_closed(self) -> None:
        tw.write_atomic(
            self.paths.release_records / (self.identifier(1) + ".json"),
            json.dumps({"managed_by": tw.OWNER, "release": "different"}),
        )
        with self.assertRaises(tw.ToastError):
            tw.prune_history(self.paths, 1, 1, apply=True)
        self.assertTrue((self.paths.releases / self.identifier(1)).exists())

    def test_retention_bounds_and_invalid_backup_references(self) -> None:
        for value in (0, -1, 51, True, "5"):
            with self.subTest(value=value), self.assertRaises(tw.ToastError):
                tw.prune_history(self.paths, value, 1)
        directory = self.backup(1, [])
        for metadata in (
            {"managed_by": tw.OWNER},
            {"managed_by": tw.OWNER, "release_refs": None},
            {"managed_by": tw.OWNER, "release_refs": "not-a-list"},
            {"managed_by": tw.OWNER, "release_refs": ["../../outside"]},
        ):
            with self.subTest(metadata=metadata):
                tw.write_atomic(directory / "manifest.json", json.dumps(metadata), 0o600)
                with self.assertRaises(tw.ToastError):
                    tw.prune_history(self.paths, 1, 1, apply=True)
                self.assertTrue((self.paths.releases / self.identifier(1)).exists())

    def test_cleanup_io_failure_is_actionable_and_does_not_remove_active_content(self) -> None:
        with (
            mock.patch.object(tw.shutil, "rmtree", side_effect=OSError("denied")),
            self.assertRaisesRegex(tw.ToastError, "partially complete"),
        ):
            tw.prune_history(self.paths, 1, 1, apply=True)
        self.assertTrue((self.paths.releases / self.identifier(8)).exists())

    def test_post_commit_cleanup_failure_is_a_warning_not_a_publication_failure(self) -> None:
        with (
            mock.patch.object(tw, "prune_history", side_effect=tw.ToastError("unsafe history")),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            tw.post_success_cleanup(self.paths)
        self.assertIn("publication succeeded", output.getvalue())
        self.assertEqual(tw.load_state(self.paths)["active_release"], self.identifier(8))


class RedirectPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = tw.Paths(Path(temporary.name))
        self.state = {
            "managed_by": tw.OWNER,
            "domain": "docs.example.org",
            "port": 8443,
            "active_release": "existing",
            "http_redirect": False,
        }
        tw.write_atomic(self.paths.state, json.dumps(self.state), 0o600)
        tw.write_atomic(self.paths.config, tw.MARKER + "\nold config\n")
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(tw, "require_host").start()
        mock.patch.object(tw, "operation_lock", side_effect=lambda paths: contextlib.nullcontext()).start()
        mock.patch.object(tw, "modern_nginx", return_value=True).start()
        mock.patch.object(tw, "ipv6_available", return_value=False).start()
        self.runner = mock.patch.object(tw, "run", return_value=subprocess.CompletedProcess([], 0, "", "")).start()

    def test_enable_requires_public_https_confirmation_before_changes(self) -> None:
        before = self.paths.config.read_bytes(), self.paths.state.read_bytes()
        with self.assertRaises(tw.ToastError):
            tw.set_http_redirect(argparse.Namespace(mode="enable", confirm_public_https=False), self.paths)
        self.assertEqual(before, (self.paths.config.read_bytes(), self.paths.state.read_bytes()))
        self.assertFalse(self.paths.backups.exists())

    def test_enable_and_disable_preserve_http01_and_use_fixed_domain(self) -> None:
        for mode in ("enable", "disable"):
            with self.subTest(mode=mode), contextlib.redirect_stdout(io.StringIO()):
                tw.set_http_redirect(argparse.Namespace(mode=mode, confirm_public_https=mode == "enable"), self.paths)
            config = self.paths.config.read_text()
            self.assertIn("location ^~ /.well-known/acme-challenge/", config)
            self.assertIn(
                "return 301 https://docs.example.org$request_uri;"
                if mode == "enable"
                else "location / { return 404; }",
                config,
            )
            self.assertNotIn("https://$host", config)
            self.assertEqual(tw.load_state(self.paths)["http_redirect"], mode == "enable")

    def test_failed_config_check_restores_state_and_configuration(self) -> None:
        before = self.paths.config.read_bytes(), self.paths.state.read_bytes()

        def fail_config(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "/usr/sbin/nginx":
                raise tw.ToastError("test config failure")
            return subprocess.CompletedProcess(args, 0, "", "")

        self.runner.side_effect = fail_config
        with self.assertRaises(tw.ToastError):
            tw.set_http_redirect(argparse.Namespace(mode="enable", confirm_public_https=True), self.paths)
        self.assertEqual(before, (self.paths.config.read_bytes(), self.paths.state.read_bytes()))


class ReviewRenderTests(unittest.TestCase):
    def test_redirect_never_enabled_by_tls_readiness_alone(self) -> None:
        for tls in (False, True):
            config = tw.render_nginx(tw.Paths(), "docs.example.org", 8443, tls=tls, modern_http2=True, ipv6=False)
            self.assertIn("location / { return 404; }", config)
            self.assertNotIn("return 301", config)
        config = tw.render_nginx(
            tw.Paths(), "docs.example.org", 8443, tls=False, modern_http2=True, ipv6=False, http_redirect=True
        )
        self.assertNotIn("return 301", config)

    def test_connection_budget_pairs_nginx_and_systemd_limits(self) -> None:
        for count in (64, 512, 2048, 8192):
            with self.subTest(count=count):
                config = tw.render_nginx(
                    tw.Paths(),
                    "docs.example.org",
                    8443,
                    tls=True,
                    modern_http2=True,
                    ipv6=False,
                    worker_connections=count,
                )
                self.assertIn(f"worker_connections {count};", config)
                self.assertIn(f"worker_rlimit_nofile {tw.file_limit(count)};", config)
                self.assertIn(f"LimitNOFILE={tw.file_limit(count)}", tw.render_unit(count))
        for count in (0, 63, 8193, True, "2048"):
            with self.subTest(count=count), self.assertRaises(tw.ToastError):
                tw.validate_connections(count)

    def test_plan_connection_and_retention_options_are_nonmutating(self) -> None:
        with mock.patch.object(tw, "run") as runner, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                tw.main(
                    [
                        "plan",
                        "--domain",
                        "docs.example.org",
                        "--email",
                        "ops@example.org",
                        "--worker-connections",
                        "2048",
                        "--keep-releases",
                        "4",
                        "--keep-backups",
                        "3",
                    ]
                ),
                0,
            )
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["worker_connections"], 2048)
        self.assertEqual(plan["file_descriptor_limit"], 8192)
        self.assertEqual(plan["keep_releases"], 4)
        self.assertEqual(plan["keep_backups"], 3)
        self.assertFalse(plan["http_redirect"])
        runner.assert_not_called()

    def test_builtin_public_assets_omit_deployer_branding_not_all_fingerprints(self) -> None:
        for root in (tw.BUNDLE / "site", *(tw.BUNDLE / "themes" / name for name in ("docs", "status", "api"))):
            for path in root.rglob("*"):
                if path.is_file():
                    with self.subTest(path=path):
                        self.assertNotIn("toastweb", path.read_text(encoding="utf-8").lower())
        self.assertTrue(all("Toast" not in defaults[0] for defaults in tw.THEMES.values()))


if __name__ == "__main__":
    unittest.main()
