"""Focused local-only tests for toastweb_diagnostics.

No production service, DNS query, network connection, or ToastWeb installation is
exercised here. Subprocesses, TLS probes and DNS responses are mocked.
"""

from __future__ import annotations

import hashlib
import ssl
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import toastweb_diagnostics as diagnostics

CERTIFICATE_PEM = """-----BEGIN CERTIFICATE-----
MIIDFzCCAf+gAwIBAgIUC8rmpJZ/XA34n8sGtbw3ROM4gZ8wDQYJKoZIhvcNAQEL
BQAwGzEZMBcGA1UEAwwQZG9jcy5leGFtcGxlLm9yZzAeFw0yNjExMTQxMzMzNTha
Fw0zNjExMTExMzMzNThaMBsxGTAXBgNVBAMMEGRvY3MuZXhhbXBsZS5vcmcwggEi
MA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQC8DxU7EpdL9iF+tZO3+ONskpAi
25/rGnejbQ0o6Railcft3/83RULxNlR+RAVHL3Ut+0l+FxxwJDT8F+0982EwbHTS
1QkJbkCL5gy6/mTEkKKps/p1fS8cmDLfzEs5jorvf3HKcyIIxyXqNuVJ/Rh0WL76
Exn4lvm2Bp0aB+eGEAhZRWgdnMiaSFiDy7qPwGrswxvPXSoCJFsgwl+XoLSNTHqr
5pyizJYtMW5uQnSWTzeSsDB1rJ5FI1uPk/aiWZNBbRko73DvKBwk5NDVYg8O12UN
WCSIO4qPd5s/adduS6uQGII/HXyN+LfpamqkmVHMhoMWFcPrdW92rdMqGia5AgMB
AAGjUzBRMB0GA1UdDgQWBBRXYf7N7sN/XJ7wg8OGuei7dt/syDAfBgNVHSMEGDAW
gBRXYf7N7sN/XJ7wg8OGuei7dt/syDAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3
DQEBCwUAA4IBAQA/O5doutqDOO/k99jukDuXzMLd84lFakQgNeICfzD7m4O76a+F
aO6WRCQaT87JiwvEZNJMGsJjsphJ/T4CzXRRLvNg/nUFP5Vbo5XtXFjCGGTFvkX0
z+fHWDK4ltYu3jgFuCnfCrmixLquBmGbvHhbm5rvcXIyFOsMgJK4bi4oc9NuM0QL
QMbuWByqb2kyd+eVglGdDoramReqj1W8oirQC3fn3ITA1e7ezYWuv1mEt7eo3J4c
OWj5PA5ivKMZNb+y1Tq3k+db8z8UfYct0bquZCG+hGHViHaalY0hCH/dHcfNpiRl
lGeVQ7aYO7BuXX6fduk7OtCHGBr+4GqTVVM7
-----END CERTIFICATE-----
"""
DOMAIN = "docs.example.org"
PORT = 8443
MAIN_PID = 4321
CERT_DER = ssl.PEM_cert_to_DER_cert(CERTIFICATE_PEM)
CERT_FINGERPRINT = hashlib.sha256(CERT_DER).hexdigest()


def openssl_date(moment: datetime) -> str:
    return moment.strftime("%b %d %H:%M:%S %Y GMT")


class FakePaths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = root / "nginx.conf"
        self.logs = root / "logs"
        self.logs.mkdir()
        self.certificate = root / "live" / ("toastweb-" + DOMAIN) / "fullchain.pem"
        self.certificate.parent.mkdir(parents=True)
        self.certificate.write_text(CERTIFICATE_PEM, encoding="ascii")

    def lineage(self, domain: str) -> Path:
        return self.root / "live" / ("toastweb-" + domain)


class FakeRunner:
    def __init__(
        self,
        *,
        nginx_code: int = 0,
        nginx_raises: bool = False,
        service_output: str | None = None,
        timer_output: str | None = None,
        ss_output: str | None = None,
        enddate: str | None = None,
    ) -> None:
        future = openssl_date(datetime.now(timezone.utc) + timedelta(days=365))
        self.nginx_code = nginx_code
        self.nginx_raises = nginx_raises
        self.service_output = service_output or "\n".join(
            [
                "ActiveState=active",
                "MainPID=" + str(MAIN_PID),
                "Restart=always",
                "StartLimitIntervalUSec=10s",
                "StartLimitBurst=5",
                "LimitNOFILESoft=4096",
                "UnitFileState=enabled",
            ]
        )
        self.timer_output = timer_output or "ActiveState=active\nUnitFileState=enabled"
        self.ss_output = (
            ss_output
            if ss_output is not None
            else "\n".join(
                [
                    'LISTEN 0 511 127.0.0.1:8443 0.0.0.0:* users:(("nginx",pid=4321,fd=6))',
                    'LISTEN 0 511 [::1]:8443 [::]:* users:(("nginx",pid=4321,fd=7))',
                ]
            )
        )
        self.enddate = enddate or future
        self.calls: list[tuple[list[str], bool, int]] = []

    def __call__(self, args: list[str], *, check: bool = False, timeout: int = 0) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, check, timeout))
        if args[0] == "/usr/sbin/nginx":
            if self.nginx_raises:
                raise OSError("simulated nginx failure")
            return subprocess.CompletedProcess(args, self.nginx_code, "", "")
        if args[0] == "systemctl":
            unit = args[2]
            output = self.service_output if unit == "toastweb-nginx.service" else self.timer_output
            return subprocess.CompletedProcess(args, 0, output, "")
        if args[0] == "ss":
            return subprocess.CompletedProcess(args, 0, self.ss_output, "")
        if args[0] == "openssl":
            return subprocess.CompletedProcess(args, 0, "notAfter=" + self.enddate + "\n", "")
        raise AssertionError("unexpected command: " + repr(args))


class DiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = FakePaths(Path(temporary.name))
        self.state = {"domain": DOMAIN, "port": PORT, "worker_connections": 512}
        self.future = datetime.now(timezone.utc) + timedelta(days=365)
        self.runner = FakeRunner()
        self.addCleanup(mock.patch.stopall)
        self.h2_patch = mock.patch.object(
            diagnostics,
            "_probe_h2",
            return_value={
                "version": "TLSv1.3",
                "alpn": "h2",
                "fingerprint": CERT_FINGERPRINT,
                "not_after": openssl_date(self.future),
            },
        )
        self.h2 = self.h2_patch.start()
        self.http_patch = mock.patch.object(
            diagnostics,
            "_probe_http11",
            return_value={"status": 200, "alpn": "http/1.1"},
        )
        self.http = self.http_patch.start()

    def build(self, **overrides: Any) -> dict[str, Any]:
        runner = overrides.pop("runner", self.runner)
        return diagnostics.build_report(
            self.paths,
            self.state,
            run_command=runner,
            **overrides,
        )

    def check(self, report: dict[str, Any], identifier: str) -> dict[str, Any]:
        checks = report["checks"]
        assert isinstance(checks, list)
        for item in checks:
            if item["id"] == identifier:
                return item
        self.fail("missing check: " + identifier)

    def test_healthy_report_schema_and_exit_code(self) -> None:
        report = self.build()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(diagnostics.report_exit_code(report), 0)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["scope"], "local_backend")
        self.assertEqual(report["not_tested"], diagnostics.NOT_TESTED)
        self.assertTrue(str(report["checked_at"]).endswith("Z"))
        for identifier in (
            "nginx_config",
            "service_state",
            "renewal_timer",
            "local_listeners",
            "certificate_disk",
            "certificate_served",
            "backend_tls",
            "certificate_match",
            "backend_http",
            "access_log",
            "dns_caa",
            "dns_dnssec",
        ):
            self.assertIn(identifier, {item["id"] for item in report["checks"]})
        rendered = diagnostics.format_report(report)
        self.assertIn("ToastWeb diagnostics: ok", rendered)
        self.assertNotIn("BEGIN PRIVATE KEY", rendered)
        self.assertNotIn('users:(("nginx"', rendered)

    def test_critical_unknown_does_not_become_success(self) -> None:
        report = self.build(runner=FakeRunner(nginx_raises=True))
        nginx = self.check(report, "nginx_config")
        self.assertEqual(nginx["level"], "unknown")
        self.assertTrue(nginx["critical"])
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(diagnostics.report_exit_code(report), 2)
        rendered = diagnostics.format_report(report)
        self.assertNotIn("simulated nginx failure", rendered)

    def test_warning_still_exits_zero(self) -> None:
        service = "\n".join(
            [
                "ActiveState=active",
                "MainPID=" + str(MAIN_PID),
                "Restart=no",
                "StartLimitIntervalUSec=10s",
                "StartLimitBurst=5",
                "LimitNOFILESoft=4096",
                "UnitFileState=enabled",
            ]
        )
        report = self.build(runner=FakeRunner(service_output=service))
        self.assertEqual(self.check(report, "service_restart_policy")["level"], "warning")
        self.assertEqual(report["status"], "warning")
        self.assertEqual(diagnostics.report_exit_code(report), 0)

    def test_own_public_listener_is_critical_error(self) -> None:
        ss_output = 'LISTEN 0 511 0.0.0.0:8443 0.0.0.0:* users:(("nginx",pid=4321,fd=6))'
        report = self.build(runner=FakeRunner(ss_output=ss_output))
        listener = self.check(report, "local_listeners")
        self.assertEqual(listener["level"], "error")
        self.assertTrue(listener["critical"])
        self.assertEqual(report["status"], "error")
        self.assertEqual(diagnostics.report_exit_code(report), 1)

    def test_missing_process_information_is_unknown(self) -> None:
        ss_output = "LISTEN 0 511 127.0.0.1:8443 0.0.0.0:*"
        report = self.build(runner=FakeRunner(ss_output=ss_output))
        listener = self.check(report, "local_listeners")
        self.assertEqual(listener["level"], "unknown")
        self.assertTrue(listener["critical"])
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(diagnostics.report_exit_code(report), 2)

    def test_foreign_public_listener_is_only_warning(self) -> None:
        ss_output = "\n".join(
            [
                'LISTEN 0 511 127.0.0.1:8443 0.0.0.0:* users:(("nginx",pid=4321,fd=6))',
                'LISTEN 0 511 0.0.0.0:8443 0.0.0.0:* users:(("other",pid=9999,fd=3))',
            ]
        )
        report = self.build(runner=FakeRunner(ss_output=ss_output))
        self.assertEqual(self.check(report, "local_listeners")["level"], "ok")
        conflict = self.check(report, "port_conflict")
        self.assertEqual(conflict["level"], "warning")
        self.assertFalse(conflict["critical"])
        self.assertEqual(report["status"], "warning")
        self.assertEqual(diagnostics.report_exit_code(report), 0)

    def test_no_attributable_listener_is_unknown(self) -> None:
        report = self.build(runner=FakeRunner(ss_output=""))
        self.assertEqual(self.check(report, "local_listeners")["level"], "unknown")
        self.assertEqual(report["status"], "unknown")

    def test_near_expiry_is_warning(self) -> None:
        soon = datetime.now(timezone.utc) + timedelta(days=10)
        report = self.build(runner=FakeRunner(enddate=openssl_date(soon)))
        certificate = self.check(report, "certificate_disk")
        self.assertEqual(certificate["level"], "warning")
        self.assertTrue(certificate["critical"])
        self.assertEqual(report["status"], "warning")
        self.assertEqual(diagnostics.report_exit_code(report), 0)

    def test_served_certificate_near_expiry_is_critical_warning(self) -> None:
        soon = datetime.now(timezone.utc) + timedelta(days=10)
        self.h2.return_value["not_after"] = openssl_date(soon)
        report = self.build()
        served = self.check(report, "certificate_served")
        self.assertEqual(served["level"], "warning")
        self.assertTrue(served["critical"])
        self.assertEqual(report["status"], "warning")
        self.assertEqual(diagnostics.report_exit_code(report), 0)

    def test_served_certificate_expired_is_error(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=1)
        self.h2.return_value["not_after"] = openssl_date(past)
        report = self.build()
        served = self.check(report, "certificate_served")
        self.assertEqual(served["level"], "error")
        self.assertTrue(served["critical"])
        self.assertEqual(report["status"], "error")
        self.assertEqual(diagnostics.report_exit_code(report), 1)

    def test_served_certificate_expiry_unknown_is_not_ok(self) -> None:
        self.h2.return_value.pop("not_after", None)
        report = self.build()
        served = self.check(report, "certificate_served")
        self.assertEqual(served["level"], "unknown")
        self.assertTrue(served["critical"])
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(diagnostics.report_exit_code(report), 2)

    def test_expired_certificate_is_error(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(days=1)
        report = self.build(runner=FakeRunner(enddate=openssl_date(past)))
        self.assertEqual(self.check(report, "certificate_disk")["level"], "error")
        self.assertEqual(report["status"], "error")
        self.assertEqual(diagnostics.report_exit_code(report), 1)

    def test_leaf_certificate_mismatch_is_error(self) -> None:
        self.h2.return_value["fingerprint"] = "00" * 32
        report = self.build()
        self.assertEqual(self.check(report, "certificate_match")["level"], "error")
        self.assertEqual(report["status"], "error")
        self.assertEqual(diagnostics.report_exit_code(report), 1)

    def test_http_failure_is_error(self) -> None:
        self.http.return_value = {"status": 503, "alpn": "http/1.1"}
        report = self.build()
        self.assertEqual(self.check(report, "backend_http")["level"], "error")
        self.assertEqual(report["status"], "error")

    def test_loopback_access_log_is_info_and_does_not_leak_paths(self) -> None:
        access_log = self.paths.logs / "access.log"
        access_log.write_text(
            '127.0.0.1 - - [14/Nov/2026:00:00:00 +0000] "GET /secret-path HTTP/1.1" 200 1\n',
            encoding="utf-8",
        )
        report = self.build()
        access = self.check(report, "access_log")
        self.assertEqual(access["level"], "info")
        self.assertFalse(access["critical"])
        self.assertEqual(access["details"], {"loopback": 1, "other": 0})
        rendered = diagnostics.format_report(report)
        self.assertNotIn("secret-path", rendered)
        self.assertNotIn("GET /", rendered)

    def test_nonloopback_access_log_is_neutral_info_and_not_warning(self) -> None:
        access_log = self.paths.logs / "access.log"
        access_log.write_text(
            '192.0.2.55 - - [14/Nov/2026:00:00:00 +0000] "GET /acme-challenge/token HTTP/1.1" 200 1\n',
            encoding="utf-8",
        )
        report = self.build()
        access = self.check(report, "access_log")
        self.assertEqual(access["level"], "info")
        self.assertFalse(access["critical"])
        self.assertEqual(access["details"], {"loopback": 0, "other": 1})
        self.assertEqual(report["status"], "ok")
        self.assertEqual(diagnostics.report_exit_code(report), 0)
        rendered = diagnostics.format_report(report)
        self.assertNotIn("192.0.2.55", rendered)
        self.assertNotIn("acme-challenge", rendered)
        self.assertNotIn("GET /", rendered)

    def test_dns_not_requested_is_explicit_info(self) -> None:
        report = self.build()
        for identifier in ("dns_caa", "dns_dnssec"):
            check = self.check(report, identifier)
            self.assertEqual(check["level"], "info")
            self.assertEqual(check["details"], {"checked": False})
        self.assertEqual(report["status"], "ok")

    def test_optional_dns_failure_does_not_block(self) -> None:
        with mock.patch.object(diagnostics, "_fetch_dns_json", side_effect=OSError("no network")):
            report = self.build(dns_checks=True)
        for identifier in ("dns_caa", "dns_dnssec"):
            check = self.check(report, identifier)
            self.assertEqual(check["level"], "unknown")
            self.assertFalse(check["critical"])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(diagnostics.report_exit_code(report), 0)

    def test_dns_transport_uses_fixed_host_and_rejects_redirect(self) -> None:
        class FakeResponse:
            def __init__(self) -> None:
                self.status = 302
                self.read_size: int | None = None

            def read(self, size: int) -> bytes:
                self.read_size = size
                return b'{"Status": 0, "Answer": []}'

        class FakeConnection:
            def __init__(self, host: str, timeout: int = 0) -> None:
                self.host = host
                self.timeout = timeout
                self.requests: list[tuple[str, str, dict[str, str] | None]] = []
                self.response = FakeResponse()
                self.closed = False

            def request(self, method: str, target: str, headers: dict[str, str] | None = None) -> None:
                self.requests.append((method, target, headers))

            def getresponse(self) -> FakeResponse:
                return self.response

            def close(self) -> None:
                self.closed = True

        connections: list[FakeConnection] = []

        def connect(host: str, timeout: int = 0) -> FakeConnection:
            connection = FakeConnection(host, timeout)
            connections.append(connection)
            return connection

        with mock.patch.object(diagnostics.http.client, "HTTPSConnection", side_effect=connect):
            with self.assertRaises(ValueError):
                diagnostics._fetch_dns_json("docs.example.org", "CAA")

        self.assertEqual(len(connections), 1)
        connection = connections[0]
        self.assertEqual(connection.host, "dns.google")
        self.assertEqual(connection.timeout, 5)
        self.assertEqual(connection.requests[0][0], "GET")
        self.assertTrue(connection.requests[0][1].startswith("/resolve?"))
        self.assertIn("name=docs.example.org", connection.requests[0][1])
        self.assertEqual(len(connection.requests), 1)
        self.assertTrue(connection.closed)

    def test_dns_transport_rejects_oversized_response_and_closes(self) -> None:
        class FakeResponse:
            def __init__(self) -> None:
                self.status = 200
                self.read_size: int | None = None

            def read(self, size: int) -> bytes:
                self.read_size = size
                return b"x" * (diagnostics.DNS_RESPONSE_LIMIT + 1)

        class FakeConnection:
            def __init__(self, host: str, timeout: int = 0) -> None:
                self.host = host
                self.timeout = timeout
                self.response = FakeResponse()
                self.closed = False

            def request(self, method: str, target: str, headers: dict[str, str] | None = None) -> None:
                return None

            def getresponse(self) -> FakeResponse:
                return self.response

            def close(self) -> None:
                self.closed = True

        connections: list[FakeConnection] = []

        def connect(host: str, timeout: int = 0) -> FakeConnection:
            connection = FakeConnection(host, timeout)
            connections.append(connection)
            return connection

        with mock.patch.object(diagnostics.http.client, "HTTPSConnection", side_effect=connect):
            with self.assertRaises(ValueError):
                diagnostics._fetch_dns_json("docs.example.org", "A")

        self.assertEqual(len(connections), 1)
        connection = connections[0]
        self.assertEqual(connection.response.read_size, diagnostics.DNS_RESPONSE_LIMIT + 1)
        self.assertTrue(connection.closed)

    def test_caa_other_issuer_is_warning(self) -> None:
        def fake_dns(name: str, record_type: str) -> dict[str, object]:
            if record_type == "CAA":
                return {"Answer": [{"type": 257, "data": '0 issue "otherca.example"'}]}
            return {"AD": False}

        with mock.patch.object(diagnostics, "_fetch_dns_json", side_effect=fake_dns):
            report = self.build(dns_checks=True)
        caa = self.check(report, "dns_caa")
        self.assertEqual(caa["level"], "warning")
        self.assertFalse(caa["critical"])
        self.assertEqual(report["status"], "warning")
        self.assertEqual(diagnostics.report_exit_code(report), 0)

    def test_fd_budget_and_timer_failures_are_errors(self) -> None:
        service = "\n".join(
            [
                "ActiveState=active",
                "MainPID=" + str(MAIN_PID),
                "Restart=always",
                "StartLimitIntervalUSec=10s",
                "StartLimitBurst=5",
                "LimitNOFILESoft=1024",
                "UnitFileState=enabled",
            ]
        )
        report = self.build(runner=FakeRunner(service_output=service))
        self.assertEqual(self.check(report, "service_fd_limit")["level"], "error")

        timer = "ActiveState=inactive\nUnitFileState=disabled"
        report = self.build(runner=FakeRunner(timer_output=timer))
        self.assertEqual(self.check(report, "renewal_timer")["level"], "error")
        self.assertEqual(report["status"], "error")

    def test_loopback_classification_handles_mapped_ipv6(self) -> None:
        self.assertTrue(diagnostics._is_loopback_host("127.0.0.1"))
        self.assertTrue(diagnostics._is_loopback_host("::1"))
        self.assertTrue(diagnostics._is_loopback_host("::ffff:127.0.0.1"))
        self.assertFalse(diagnostics._is_loopback_host("0.0.0.0"))
        self.assertFalse(diagnostics._is_loopback_host("*"))
        self.assertFalse(diagnostics._is_loopback_host("192.0.2.1"))
        self.assertIsNone(diagnostics._is_loopback_host("not-an-address"))

    def test_warn_days_validation(self) -> None:
        for value in (0, 91, True):
            with self.assertRaises(ValueError):
                self.build(warn_cert_days=value)

    def test_http11_probe_sends_head_on_separate_alpn_connection(self) -> None:
        class FakeTLS:
            def __init__(self) -> None:
                self.sent = b""
                self.alpn: list[str] = []

            def __enter__(self) -> FakeTLS:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def selected_alpn_protocol(self) -> str:
                return "http/1.1"

            def sendall(self, data: bytes) -> None:
                self.sent += data

            def recv(self, size: int) -> bytes:
                return b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"

        class FakeRaw:
            def __enter__(self) -> FakeRaw:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        self.http_patch.stop()
        tls = FakeTLS()
        context = mock.Mock()
        context.wrap_socket.return_value = tls
        with (
            mock.patch.object(diagnostics.socket, "create_connection", return_value=FakeRaw()) as connect,
            mock.patch.object(diagnostics.ssl, "create_default_context", return_value=context),
        ):
            result = diagnostics._probe_http11(DOMAIN, PORT)
        self.assertEqual(result, {"status": 200, "alpn": "http/1.1"})
        connect.assert_called_once_with(("127.0.0.1", PORT), timeout=5)
        context.set_alpn_protocols.assert_called_once_with(["http/1.1"])
        self.assertTrue(tls.sent.startswith(b"HEAD / HTTP/1.1\r\n"))

    def test_http11_probe_does_not_send_on_unexpected_alpn(self) -> None:
        class FakeTLS:
            def __init__(self) -> None:
                self.sent = b""

            def __enter__(self) -> FakeTLS:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def selected_alpn_protocol(self) -> str:
                return "h2"

            def sendall(self, data: bytes) -> None:
                self.sent += data

            def recv(self, size: int) -> bytes:
                raise AssertionError("must not read an h2 connection here")

        class FakeRaw:
            def __enter__(self) -> FakeRaw:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        self.http_patch.stop()
        tls = FakeTLS()
        context = mock.Mock()
        context.wrap_socket.return_value = tls
        with (
            mock.patch.object(diagnostics.socket, "create_connection", return_value=FakeRaw()),
            mock.patch.object(diagnostics.ssl, "create_default_context", return_value=context),
        ):
            result = diagnostics._probe_http11(DOMAIN, PORT)
        self.assertEqual(result, {"status": None, "alpn": "h2"})
        self.assertEqual(tls.sent, b"")

    def test_duration_parser_accepts_systemd_spans(self) -> None:
        self.assertEqual(diagnostics._duration_usec("10s"), 10_000_000)
        self.assertEqual(diagnostics._duration_usec("1min 30s"), 90_000_000)
        self.assertEqual(diagnostics._duration_usec("0"), 0)
        self.assertEqual(diagnostics._duration_usec("infinity"), 0)
        self.assertIsNone(diagnostics._duration_usec("not-a-duration"))
