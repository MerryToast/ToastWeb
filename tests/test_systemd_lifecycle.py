"""Opt-in ROOT CI tests: disposable systemd units, /run fixtures and loopback high ports.

Never install ToastWeb, run APT, modify a real site, or operate a production service.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
import uuid
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import toastweb as tw

LIVE = os.name == "posix" and os.environ.get("TOASTWEB_LIVE_SYSTEMD") == "1"
# systemd's own refusal message, with or without the older ", refusing to start" suffix.
RATE_LIMIT_EVIDENCE = "start request repeated too quickly"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@unittest.skipUnless(LIVE, "Set TOASTWEB_LIVE_SYSTEMD=1 in a disposable root Linux CI host")
class SystemdLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        if getattr(os, "geteuid", lambda: 1)() != 0 or not Path("/run/systemd/system").is_dir():
            self.fail("Explicit systemd test mode requires root and a running systemd")
        for executable in ("journalctl", "nginx", "openssl", "systemctl", "systemd-analyze", "needrestart"):
            if not shutil.which(executable):
                self.fail("Missing systemd CI prerequisite: " + executable)
        self.name = "toastweb-ci-" + uuid.uuid4().hex
        self.service = self.name + ".service"
        self.unit = Path("/etc/systemd/system") / self.service
        if self.unit.exists() or self.unit.is_symlink():
            self.fail("Refusing to replace an existing fixture unit")
        self.created = False
        temporary = tempfile.TemporaryDirectory(prefix=self.name + "-fixture-", dir="/run")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(self.cleanup_unit)
        self.root = Path(temporary.name)
        self.root.chmod(0o755)
        self.paths = tw.Paths(self.root)
        self.binary = self.root / "nginx"
        shutil.copy2("/usr/sbin/nginx", self.binary)
        self.binary.chmod(0o755)
        self.http_port, self.https_port = free_port(), free_port()
        while self.http_port == self.https_port:
            self.https_port = free_port()
        lineage = self.paths.lineage("notes.example.org")
        lineage.mkdir(parents=True, mode=0o700)
        self.certificate = lineage / "fullchain.pem"
        self.command(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "2",
                "-subj",
                "/CN=notes.example.org",
                "-addext",
                "subjectAltName=DNS:notes.example.org",
                "-keyout",
                str(lineage / "privkey.pem"),
                "-out",
                str(self.certificate),
            ],
            timeout=30,
        )
        (lineage / "privkey.pem").chmod(0o600)
        self.paths.logs.mkdir(parents=True)
        release = tw.create_release(
            self.paths, tw.BUNDLE / "site", "CI fixture", "Not a production site", "notes.example.org", builtin=True
        )
        tw.activate_release(self.paths, release)
        config = tw.render_nginx(
            self.paths, "notes.example.org", self.https_port, tls=True, modern_http2=tw.modern_nginx(), ipv6=False
        )
        config = config.replace("listen 80", f"listen 127.0.0.1:{self.http_port}")
        config = config.replace("/run/toastweb-nginx", "/run/" + self.name)
        temps = "\n".join(
            f"    {name}_temp_path {tw.nginx_quote(self.root / name)};"
            for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi")
        )
        config = config.replace("http {\n", "http {\n" + temps + "\n")
        tw.write_atomic(self.paths.config, config)
        unit = tw.render_unit().replace("/etc/toastweb/nginx.conf", str(self.paths.config))
        unit = unit.replace("/run/toastweb-nginx", "/run/" + self.name)
        unit = unit.replace("RuntimeDirectory=toastweb-nginx", "RuntimeDirectory=" + self.name)
        unit = unit.replace("/usr/sbin/nginx", str(self.binary))
        tw.write_atomic(self.unit, unit)
        self.created = True
        self.command(["systemd-analyze", "verify", str(self.unit)])
        self.command(["systemctl", "daemon-reload"])
        tw.write_atomic(
            self.paths.state,
            json.dumps(
                {
                    "managed_by": tw.OWNER,
                    "domain": "notes.example.org",
                    "port": self.https_port,
                    "package_upgrade_restart": True,
                }
            ),
            0o600,
        )

    def command(self, args: list[str], *, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check)

    def cleanup_unit(self) -> None:
        if not self.created:
            return
        self.command(["systemctl", "stop", self.service], check=False)
        self.command(["systemctl", "reset-failed", self.service], check=False)
        self.unit.unlink(missing_ok=True)
        self.command(["systemctl", "daemon-reload"])

    def properties(self) -> dict[str, str]:
        result = self.command(
            [
                "systemctl",
                "show",
                self.service,
                "--property=MainPID,ActiveState,Result,Restart,StartLimitBurst,StartLimitIntervalUSec,NRestarts",
            ]
        )
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def restart_count(self, values: dict[str, str] | None = None) -> int:
        current = self.properties() if values is None else values
        try:
            return int(current["NRestarts"])
        except (KeyError, ValueError):
            self.fail(f"Disposable systemd fixture exposed no usable NRestarts property: {current}")

    def fixture_journal(self, lines: int = 60) -> str:
        """Return a bounded journal tail for this disposable fixture unit only."""
        try:
            result = self.command(
                ["journalctl", "-u", self.service, "-n", str(lines), "--no-pager", "-o", "cat"],
                check=False,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as error:
            return f"<journal unavailable: {error.__class__.__name__}>"
        return (result.stdout or "").strip() or "<no journal entries>"

    def wait_for(
        self,
        predicate: Callable[[dict[str, str]], bool],
        timeout: int = 20,
        description: str = "expected state",
    ) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        values = self.properties()
        while time.monotonic() < deadline:
            if predicate(values):
                return values
            time.sleep(0.1)
            values = self.properties()
        self.fail(
            f"Disposable systemd fixture did not reach {description!r} within {timeout}s. "
            f"Last properties: {values}. Bounded journal for {self.service}:\n{self.fixture_journal()}"
        )

    def test_master_crash_recovers_with_new_pid(self) -> None:
        self.command(["systemctl", "start", self.service])
        before = self.properties()
        self.assertEqual(before["Restart"], "on-failure")
        self.assertEqual(before["StartLimitBurst"], "5")
        pid = int(before["MainPID"])
        self.assertGreater(pid, 1)
        self.assertEqual(Path(f"/proc/{pid}/exe").resolve(), self.binary)
        kill_signal = getattr(signal, "SIGKILL", None)
        if kill_signal is None:
            self.fail("SIGKILL is required by the Linux systemd fixture")
        os.kill(pid, kill_signal)
        self.wait_for(lambda p: p["ActiveState"] == "active" and int(p["MainPID"]) > 1 and int(p["MainPID"]) != pid)

    def test_repeated_invalid_starts_are_rate_limited(self) -> None:
        tw.write_atomic(self.paths.config, tw.MARKER + "\ninvalid_ci_directive;\n")
        before = self.properties()
        self.assertEqual(before["Restart"], "on-failure")
        self.assertEqual(before["StartLimitBurst"], "5")
        interval = before.get("StartLimitIntervalUSec", "")
        self.assertTrue(interval and interval not in {"0", "infinity"}, f"Start limit is not enabled: {before}")
        burst = int(before["StartLimitBurst"])
        # Result may still report the last ExecStartPre failure (exit-code) instead of
        # start-limit-hit, so require both a restart count the burst can prove and a
        # real refusal in the journal. The explicit start consumes one slot of the burst.
        restarts_required = burst - 1
        self.assertGreaterEqual(restarts_required, 4)
        self.command(["systemctl", "start", self.service], check=False)
        deadline = time.monotonic() + 60
        while True:
            values = self.properties()
            restarts = self.restart_count(values)
            journal = ""
            if values["ActiveState"] == "failed" and restarts >= restarts_required:
                journal = self.fixture_journal()
                if values["Result"] == "start-limit-hit" or RATE_LIMIT_EVIDENCE in journal.casefold():
                    break
            if time.monotonic() >= deadline:
                self.fail(
                    "Disposable systemd fixture never proved start limiting: expected ActiveState=failed, "
                    f"at least {restarts_required} automatic restarts and a rate-limit refusal. "
                    f"Last properties: {values}. Bounded journal for {self.service}:\n"
                    f"{journal or self.fixture_journal()}"
                )
            time.sleep(0.2)
        # RestartSec=2s: staying failed for three seconds proves no restart is still pending.
        time.sleep(3)
        settled = self.properties()
        self.assertEqual(settled["ActiveState"], "failed", f"Unit left the failed state: {settled}")
        self.assertEqual(self.restart_count(settled), restarts, f"Unit restarted after the rate limit: {settled}")

    def test_reload_keeps_deleted_executable_but_package_refresh_replaces_it(self) -> None:
        self.command(["systemctl", "start", self.service])
        before = int(self.properties()["MainPID"])
        replacement = self.root / "nginx.new"
        shutil.copy2(self.binary, replacement)
        os.replace(replacement, self.binary)  # Simulate an atomic package binary replacement, not an APT upgrade.
        self.command([str(self.binary), "-s", "reload", "-c", str(self.paths.config)])
        self.assertEqual(int(self.properties()["MainPID"]), before)
        self.assertTrue(os.readlink(f"/proc/{before}/exe").endswith(" (deleted)"))
        factory = ssl.create_default_context
        with (
            mock.patch.object(tw, "SERVICE", self.service),
            mock.patch.object(tw, "require_host"),  # The isolated Paths intentionally do not describe the host OS.
            mock.patch.object(
                tw.ssl, "create_default_context", side_effect=lambda: factory(cafile=str(self.certificate))
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertTrue(tw.package_restart_needed())
            tw.package_refresh(self.paths)
        after = int(self.properties()["MainPID"])
        self.assertNotEqual(before, after)
        self.assertEqual(Path(f"/proc/{after}/exe").resolve(), self.binary)
        self.assertIn("trusted HTTPS HEAD passed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
