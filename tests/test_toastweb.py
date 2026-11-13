"""No external DNS, CA account, system service, or VPS is changed by unit tests."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

import toastweb as tw


def completed(args: list[str], code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, code, stdout, stderr)


class ValidationTests(unittest.TestCase):
    def test_valid_domain(self) -> None:
        self.assertEqual(tw.normalize_domain(" Notes.Example.ORG. "), "notes.example.org")
        self.assertTrue(tw.normalize_domain("笔记.example.org").startswith("xn--"))

    def test_unsafe_domains(self) -> None:
        for value in (
            "https://x.org",
            "x.org:443",
            "*.x.org",
            "127.0.0.1",
            "localhost",
            "a_b.org",
            "-a.org",
            "x.org;return 200",
            "x.org\nserver{}",
            "a..org",
            "a/xx.org",
            "a" * 64 + ".org",
        ):
            with self.subTest(value=value), self.assertRaises(tw.ToastError):
                tw.normalize_domain(value)

    def test_contact_text_and_ports(self) -> None:
        self.assertEqual(tw.validate_email("ops@example.org"), "ops@example.org")
        for value in ("missing", "a@b", "a@b.org\nheader"):
            with self.assertRaises(tw.ToastError):
                tw.validate_email(value)
        for value in (80, 443, 65536, -1):
            with self.assertRaises(tw.ToastError):
                tw.validate_port(value)
        self.assertEqual(tw.validate_port(8443), 8443)
        with self.assertRaises(tw.ToastError):
            tw.validate_text("unsafe\ntext")

    def test_certbot_is_webroot_not_nginx_or_standalone(self) -> None:
        args = tw.certbot_command(tw.Paths(), "notes.example.org", "ops@example.org")
        self.assertIn("--webroot", args)
        self.assertNotIn("--nginx", args)
        self.assertNotIn("--standalone", args)
        self.assertEqual(args[args.index("--cert-name") + 1], "toastweb-notes.example.org")

    def test_explicit_consents(self) -> None:
        args = argparse.Namespace(accept_tos=False, confirm_port_80=False, non_interactive=True)
        with self.assertRaises(tw.ToastError):
            tw.confirm(args)
        args.accept_tos = args.confirm_port_80 = True
        tw.confirm(args)

    def test_plan_never_runs_commands(self) -> None:
        with mock.patch.object(tw, "run") as runner, contextlib.redirect_stdout(io.StringIO()) as output:
            code = tw.main(["plan", "--domain", "notes.example.org", "--email", "ops@example.org"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())["executed"])
        runner.assert_not_called()


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths = tw.Paths()

    def test_https_loopback_and_unknown_sni(self) -> None:
        config = tw.render_nginx(self.paths, "notes.example.org", 8443, tls=True, modern_http2=True, ipv6=True)
        self.assertIn("listen 127.0.0.1:8443 ssl", config)
        self.assertNotIn("listen 443", config)
        self.assertNotIn("0.0.0.0:8443", config)
        self.assertIn("ssl_reject_handshake on", config)
        self.assertIn("http2 on;", config)
        self.assertNotIn("ssl_ecdh_curve", config)
        self.assertNotIn("proxy_pass", config)
        self.assertIn("listen [::]:80", config)
        self.assertIn("location / { return 404; }", config)

    def test_legacy_http2_syntax_and_bootstrap(self) -> None:
        config = tw.render_nginx(self.paths, "notes.example.org", 8443, tls=True, modern_http2=False, ipv6=False)
        self.assertIn("listen 127.0.0.1:8443 ssl http2;", config)
        self.assertNotIn("http2 on;", config)
        config = tw.render_nginx(self.paths, "notes.example.org", 8443, tls=False, modern_http2=True, ipv6=False)
        self.assertNotIn("ssl_certificate", config)
        self.assertNotIn("8443", config)
        self.assertNotIn("[::]", config)

    def test_systemd_units_and_hook_scope(self) -> None:
        unit = tw.render_unit()
        self.assertIn("/etc/toastweb/nginx.conf", unit)
        self.assertNotIn("xray", unit.lower())
        self.assertNotIn("x-ui", unit.lower())
        hook = tw.render_hook("notes.example.org")
        self.assertIn("${RENEWED_LINEAGE:-}", hook)
        self.assertIn("/etc/letsencrypt/live/toastweb-notes.example.org", hook)
        self.assertIn("reload toastweb-nginx.service", hook)
        renewal = tw.render_renew_unit("notes.example.org")
        self.assertIn("--cert-name toastweb-notes.example.org", renewal)
        self.assertIn("Persistent=true", tw.render_renew_timer())
        self.assertIn("rotate 7", tw.render_logrotate())

    def test_supported_nginx_versions(self) -> None:
        for version, expected in (("1.24.0", False), ("1.26.3", True)):
            with mock.patch.object(tw, "run", return_value=completed([], stderr="nginx version: nginx/" + version)):
                self.assertEqual(tw.modern_nginx(), expected)


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = tw.Paths(Path(self.temporary.name))

    def symlink(self, target: Path, link: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError:
            self.skipTest("OS does not allow this user to create symlinks")

    def test_atomic_write_and_state_validation(self) -> None:
        tw.write_atomic(self.paths.state, '{"managed_by":"someone else"}')
        with self.assertRaises(tw.ToastError):
            tw.load_state(self.paths)
        tw.write_atomic(self.paths.state, "not JSON")
        with self.assertRaises(tw.ToastError):
            tw.load_state(self.paths)
        tw.write_atomic(self.paths.state, "[]")
        with self.assertRaises(tw.ToastError):
            tw.read_json(self.paths.state)

    def test_existing_foreign_files_not_overwritten(self) -> None:
        tw.write_atomic(self.paths.config, "# existing administrator config")
        with self.assertRaises(tw.ToastError):
            tw.assert_owned(self.paths)
        self.assertEqual(self.paths.config.read_text(), "# existing administrator config")

    def test_symlink_overwrite_refused(self) -> None:
        target = self.paths.at("/elsewhere")
        tw.write_atomic(target, "original")
        link = self.paths.at("/link")
        self.symlink(target, link)
        with self.assertRaises(tw.ToastError):
            tw.write_atomic(link, "replacement")
        self.assertEqual(target.read_text(), "original")

    def test_release_html_escape_and_local_assets(self) -> None:
        release = tw.create_release(
            self.paths, tw.BUNDLE / "site", '<b>My "site"</b>', "A & B", "notes.example.org", builtin=True
        )
        root = self.paths.releases / release
        text = (root / "index.html").read_text(encoding="utf-8")
        self.assertIn("&lt;b&gt;My &quot;site&quot;&lt;/b&gt;", text)
        self.assertNotIn("{{SITE_TITLE}}", text)
        self.assertIn("A &amp; B", text)
        collector = LinkCollector()
        for page in root.rglob("*.html"):
            collector.feed(page.read_text(encoding="utf-8"))
        for url in collector.links:
            self.assertTrue(url.startswith("/"), url)
            target = root / url.lstrip("/")
            self.assertTrue((target / "index.html").is_file() if url.endswith("/") else target.is_file(), url)

    @unittest.skipUnless(os.name == "posix", "POSIX umask semantics")
    def test_public_paths_with_restrictive_umask(self) -> None:
        previous = os.umask(0o077)
        try:
            release = tw.create_release(
                self.paths, tw.BUNDLE / "site", "Title", "Description", "notes.example.org", builtin=True
            )
            for directory in (
                self.paths.current.parent,
                self.paths.releases,
                self.paths.acme.parent,
                self.paths.acme / ".well-known/acme-challenge",
                self.paths.releases / release,
            ):
                self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
        finally:
            os.umask(previous)

    def test_hidden_content_rejected(self) -> None:
        source = self.paths.at("/input")
        tw.write_atomic(source / "index.html", "hello")
        tw.write_atomic(source / ".env", "DO_NOT_PUBLISH")
        with self.assertRaises(tw.ToastError):
            tw.create_release(self.paths, source, "Title", "Description", "notes.example.org", builtin=False)

    def test_release_symlink_content_rejected(self) -> None:
        source = self.paths.at("/input")
        tw.write_atomic(source / "index.html", "hello")
        other = self.paths.at("/secret")
        tw.write_atomic(other, "DO_NOT_PUBLISH")
        self.symlink(other, source / "leak.txt")
        with self.assertRaises(tw.ToastError):
            tw.create_release(self.paths, source, "Title", "Description", "notes.example.org", builtin=False)

    def test_missing_index_rejected(self) -> None:
        with self.assertRaises(tw.ToastError):
            tw.create_release(
                self.paths, self.paths.at("/absent"), "Title", "Description", "notes.example.org", builtin=False
            )

    def test_current_pointer_rejects_external_location(self) -> None:
        other = self.paths.at("/unrelated")
        other.mkdir(parents=True)
        self.paths.current.parent.mkdir(parents=True)
        self.symlink(other, self.paths.current)
        with self.assertRaises(tw.ToastError):
            tw.assert_owned(self.paths)


class LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.links.extend(value for name, value in attrs if name in {"href", "src"} and value)


class PreflightTests(unittest.TestCase):
    def test_wrong_aaaa_and_cloudflare_addresses_rejected(self) -> None:
        with (
            mock.patch.object(tw, "resolve_domain", return_value={"93.184.216.34", "2606:4700::1111"}),
            mock.patch.object(tw, "local_addresses", return_value={"93.184.216.34"}),
        ):
            with self.assertRaises(tw.ToastError):
                tw.check_dns("notes.example.org", [])
        with (
            mock.patch.object(tw, "resolve_domain", return_value={"93.184.216.34"}),
            mock.patch.object(tw, "local_addresses", return_value={"10.0.0.2"}),
        ):
            self.assertEqual(tw.check_dns("notes.example.org", ["93.184.216.34"]), {"93.184.216.34"})

    def test_port_conflicts_are_not_stopped(self) -> None:
        def fake(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "ss":
                return completed(args, stdout='LISTEN 0 512 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=999,fd=6))')
            return completed(args, stdout="777\n")

        with mock.patch.object(tw, "run", side_effect=fake) as runner:
            with self.assertRaises(tw.ToastError):
                tw.check_ports(8443)
            self.assertFalse(any("stop" in call.args[0] for call in runner.call_args_list))

    def test_own_listener_is_allowed(self) -> None:
        def fake(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "ss":
                return completed(args, stdout='LISTEN 0 512 127.0.0.1:8443 0.0.0.0:* users:(("nginx",pid=777,fd=6))')
            return completed(args, stdout="777\n")

        with mock.patch.object(tw, "run", side_effect=fake):
            tw.check_ports(8443)

    def test_existing_policy_rc_d_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = tw.Paths(Path(directory))
            policy = paths.at("/usr/sbin/policy-rc.d")
            tw.write_atomic(policy, "# existing administrator policy")
            with mock.patch.object(tw, "run", return_value=completed([], 1)):
                with self.assertRaises(tw.ToastError):
                    tw.ensure_dependencies(paths)
            self.assertEqual(policy.read_text(), "# existing administrator policy")

    def test_existing_acme_requires_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = tw.Paths(Path(directory))
            tw.write_atomic(paths.at("/root/.acme.sh/acme.sh"), "# existing installation")
            with mock.patch.object(tw, "run", return_value=completed([], 1)):
                with self.assertRaises(tw.ToastError):
                    tw.check_other_acme(paths, False)
                tw.check_other_acme(paths, True)


@unittest.skipUnless(os.name == "posix", "Linux filesystem transaction tests run in CI")
class InstallTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = tw.Paths(Path(self.temporary.name))
        self.calls: list[list[str]] = []
        self.fail_ca = False
        self.fail_reload = False
        self.args = tw.parser().parse_args(
            [
                "install",
                "--domain",
                "notes.example.org",
                "--email",
                "ops@example.org",
                "--accept-tos",
                "--confirm-port-80",
                "--non-interactive",
            ]
        )
        for name in ("require_host", "check_ports", "check_other_acme", "ensure_dependencies", "http_probe"):
            patcher = mock.patch.object(tw, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("check_dns", {"93.184.216.34"}), ("modern_nginx", True), ("ipv6_available", True)):
            patcher = mock.patch.object(tw, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(tw, "operation_lock", return_value=contextlib.nullcontext())
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(tw, "run", side_effect=self.fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def fake_run(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[0] == "systemctl" and args[1] in {"is-active", "is-enabled"}:
            return completed(args, 3)
        if args[:2] == ["certbot", "certonly"]:
            if self.fail_ca:
                raise tw.ToastError("Simulated CA validation failure")
            lineage = self.paths.lineage("notes.example.org")
            tw.write_atomic(lineage / "fullchain.pem", "TEST CERTIFICATE")
            tw.write_atomic(lineage / "privkey.pem", "TEST KEY", 0o600)
        if args[:2] == ["systemctl", "reload"] and self.fail_reload:
            self.fail_reload = False
            raise tw.ToastError("Simulated reload failure")
        return completed(args)

    def test_install_and_idempotent_reinstall(self) -> None:
        tw.install(self.args, self.paths)
        first = tw.load_state(self.paths) or self.fail("Missing first installation state")
        self.assertTrue(self.paths.current.is_symlink())
        tw.assert_owned(self.paths)
        tw.install(self.args, self.paths)
        second = tw.load_state(self.paths) or self.fail("Missing second installation state")
        self.assertEqual(second["previous_release"], first["active_release"])
        self.assertNotEqual(second["active_release"], first["active_release"])
        self.assertFalse(
            any(any(term in arg for term in ("x-ui", "xray", "ufw", "iptables")) for call in self.calls for arg in call)
        )
        self.assertTrue(self.paths.renew_timer.exists())
        for theme in ("docs", "status", "api"):
            self.assertTrue((self.paths.app / "themes" / theme / "index.html").is_file())

    def test_policy_persistence_and_private_history_permissions(self) -> None:
        self.args.worker_connections = 2048
        self.args.keep_releases = 1
        self.args.keep_backups = 2
        tw.install(self.args, self.paths)
        tw.set_http_redirect(argparse.Namespace(mode="enable", confirm_public_https=True), self.paths)
        self.args.worker_connections = self.args.keep_releases = self.args.keep_backups = None
        tw.install(self.args, self.paths)
        state = tw.load_state(self.paths) or self.fail("Missing reinstalled state")
        self.assertEqual((state["worker_connections"], state["keep_releases"], state["keep_backups"]), (2048, 1, 2))
        self.assertTrue(state["http_redirect"])
        self.assertIn("worker_connections 2048;", self.paths.config.read_text())
        self.assertIn("LimitNOFILE=8192", self.paths.unit.read_text())
        for directory in (self.paths.release_records, self.paths.backups, *self.paths.backups.iterdir()):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for directory in self.paths.backups.iterdir():
            self.assertEqual((directory / "manifest.json").stat().st_mode & 0o777, 0o600)
            for release in tw.read_json(directory / "manifest.json")["release_refs"]:
                self.assertTrue((self.paths.releases / release).is_dir())

    def test_failed_transaction_never_runs_retention_cleanup(self) -> None:
        tw.install(self.args, self.paths)
        before = tw.load_state(self.paths)
        self.fail_reload = True
        with mock.patch.object(tw, "prune_history") as cleanup, self.assertRaises(tw.ToastError):
            tw.update(argparse.Namespace(content_dir=None, theme="api"), self.paths)
        cleanup.assert_not_called()
        self.assertEqual(tw.load_state(self.paths), before)

    def test_theme_switch_rollback_and_failure_recovery(self) -> None:
        tw.install(self.args, self.paths)
        original = tw.load_state(self.paths) or self.fail("Missing initial state")
        self.assertEqual(original["theme"], "docs")
        tw.update(argparse.Namespace(content_dir=None, theme="status"), self.paths)
        status_state = tw.load_state(self.paths) or self.fail("Missing status state")
        self.assertEqual(status_state["theme"], "status")
        self.assertIn('data-theme="status"', (self.paths.current / "index.html").read_text(encoding="utf-8"))
        tw.update(argparse.Namespace(), self.paths, rollback=True)
        self.assertEqual((tw.load_state(self.paths) or {})["theme"], "docs")
        tw.update(argparse.Namespace(), self.paths, rollback=True)
        self.assertEqual((tw.load_state(self.paths) or {})["theme"], "status")
        self.fail_reload = True
        with self.assertRaises(tw.ToastError):
            tw.update(argparse.Namespace(content_dir=None, theme="api"), self.paths)
        restored = tw.load_state(self.paths) or self.fail("Missing restored state")
        self.assertEqual(restored["theme"], "status")
        self.assertEqual(restored["active_release"], status_state["active_release"])
        tw.update(argparse.Namespace(content_dir=None), self.paths)
        self.assertEqual((tw.load_state(self.paths) or {})["theme"], "status")

    def test_legacy_theme_survives_upgrade(self) -> None:
        self.args.theme = "notes"
        tw.install(self.args, self.paths)
        legacy = tw.read_json(self.paths.state)
        legacy.pop("theme", None)
        legacy.pop("previous_theme", None)
        legacy["version"] = "0.1.0"
        tw.write_atomic(self.paths.state, json.dumps(legacy), 0o600)
        self.args.theme = "auto"
        tw.install(self.args, self.paths)
        self.assertEqual((tw.load_state(self.paths) or {})["theme"], "notes")
        self.assertTrue((self.paths.current / "assets/site.css").is_file())
        self.assertFalse((self.paths.current / "assets/theme.css").exists())

    def test_custom_content_requires_explicit_theme_switch(self) -> None:
        tw.install(self.args, self.paths)
        custom = self.paths.at("/custom-public")
        tw.write_atomic(custom / "index.html", "Custom content")
        tw.update(argparse.Namespace(content_dir=str(custom)), self.paths)
        with self.assertRaises(tw.ToastError):
            tw.install(self.args, self.paths)
        with self.assertRaises(tw.ToastError):
            tw.update(argparse.Namespace(content_dir=None), self.paths)
        self.assertEqual((self.paths.current / "index.html").read_text(), "Custom content")
        tw.update(argparse.Namespace(content_dir=None, theme="api"), self.paths)
        self.assertEqual((tw.load_state(self.paths) or {})["theme"], "api")
        tw.update(argparse.Namespace(), self.paths, rollback=True)
        state = tw.load_state(self.paths) or self.fail("Missing custom rollback state")
        self.assertEqual(state["content_type"], "custom")
        self.assertIsNone(state["theme"])
        self.assertEqual((self.paths.current / "index.html").read_text(), "Custom content")

    def test_failed_first_install_restores_owned_files(self) -> None:
        self.fail_ca = True
        with self.assertRaises(tw.ToastError):
            tw.install(self.args, self.paths)
        self.assertFalse(self.paths.config.exists())
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.unit.exists())
        self.assertFalse(self.paths.current.is_symlink())
        self.assertIn(["systemctl", "stop", tw.SERVICE], self.calls)
        self.assertIn(["systemctl", "stop", tw.RENEW_TIMER], self.calls)

    def test_content_update_rollback_and_failure_recovery(self) -> None:
        tw.install(self.args, self.paths)
        initial = tw.load_state(self.paths) or self.fail("Missing initial state")
        custom = self.paths.at("/input")
        tw.write_atomic(custom / "index.html", "Custom content")
        tw.update(argparse.Namespace(content_dir=str(custom)), self.paths)
        self.assertEqual((self.paths.current / "index.html").read_text(), "Custom content")
        tw.update(argparse.Namespace(), self.paths, rollback=True)
        restored = tw.load_state(self.paths) or self.fail("Missing restored state")
        self.assertEqual(restored["active_release"], initial["active_release"])
        self.assertEqual(restored["content_type"], "builtin")
        self.fail_reload = True
        with self.assertRaises(tw.ToastError):
            tw.update(argparse.Namespace(content_dir=str(custom)), self.paths)
        after_failure = tw.load_state(self.paths) or self.fail("Missing recovered state")
        self.assertEqual(after_failure["active_release"], initial["active_release"])
        self.assertEqual(self.paths.current.resolve().name, initial["active_release"])


if __name__ == "__main__":
    unittest.main()
