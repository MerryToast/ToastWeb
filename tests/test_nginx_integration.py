"""Opt-in Linux integration tests; self-signed test certificate, high local ports.

No real CA issuance, DNS mutation, systemd change, or production listener is used.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import toastweb as tw

LIVE = os.name == "posix" and os.environ.get("TOASTWEB_LIVE_NGINX") == "1"


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@unittest.skipUnless(LIVE, "Set TOASTWEB_LIVE_NGINX=1 on Linux with Nginx/OpenSSL installed")
class NginxIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        executable = shutil.which("nginx")
        if not executable or not shutil.which("openssl"):
            raise unittest.SkipTest("Nginx and OpenSSL are required")
        temporary = tempfile.TemporaryDirectory(prefix="toastweb-nginx-test-")
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        cls.paths = tw.Paths(cls.root)
        cls.paths.logs.mkdir(parents=True)
        cls.paths.acme.mkdir(parents=True)
        cls.http_port = free_port()
        cls.https_port = free_port()
        while cls.http_port == cls.https_port:
            cls.https_port = free_port()
        lineage = cls.paths.lineage("notes.example.org")
        lineage.mkdir(parents=True)
        generated = subprocess.run(
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
                str(lineage / "fullchain.pem"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if generated.returncode:
            raise RuntimeError("Test certificate generation failed: " + generated.stderr)
        cls.certificate = lineage / "fullchain.pem"
        release = tw.create_release(
            cls.paths, tw.BUNDLE / "site", "Test Notes", "Integration fixture", "notes.example.org", builtin=True
        )
        cls.initial_release = release
        tw.activate_release(cls.paths, release)
        tw.write_atomic(cls.paths.acme / ".well-known/acme-challenge/test-token", "HTTP01_OK")
        config = tw.render_nginx(
            cls.paths, "notes.example.org", cls.https_port, tls=True, modern_http2=tw.modern_nginx(), ipv6=False
        )
        config = config.replace("listen 80", f"listen 127.0.0.1:{cls.http_port}")
        config = config.replace("pid /run/toastweb-nginx/nginx.pid;", f"pid {tw.nginx_quote(cls.root / 'nginx.pid')};")
        temporary_paths = "\n".join(
            f"    {name}_temp_path {tw.nginx_quote(cls.root / name)};"
            for name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi")
        )
        config = config.replace("http {\n", "http {\n" + temporary_paths + "\n")
        tw.write_atomic(cls.paths.config, config)
        tested = subprocess.run(
            [executable, "-p", str(cls.root) + "/", "-t", "-c", str(cls.paths.config)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if tested.returncode:
            raise RuntimeError("Generated Nginx configuration failed: " + tested.stderr)
        cls.log = (cls.root / "process.log").open("w", encoding="utf-8")
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen(
            [executable, "-p", str(cls.root) + "/", "-c", str(cls.paths.config), "-g", "daemon off;"],
            stdout=cls.log,
            stderr=cls.log,
        )
        cls.addClassCleanup(cls.stop_process)
        for _ in range(100):
            if cls.process.poll() is not None:
                raise RuntimeError("Test Nginx exited: " + (cls.root / "process.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", cls.https_port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("Test Nginx did not become ready")

    @classmethod
    def stop_process(cls) -> None:
        if cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=5)

    def test_http01_path_and_normal_http_404(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=5)
        try:
            connection.request("GET", "/.well-known/acme-challenge/test-token", headers={"Host": "notes.example.org"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"HTTP01_OK")
            connection.request("GET", "/", headers={"Host": "notes.example.org"})
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
        finally:
            connection.close()

    def test_tls13_and_h2(self) -> None:
        context = ssl.create_default_context(cafile=str(self.certificate))
        context.set_alpn_protocols(["h2", "http/1.1"])
        with (
            socket.create_connection(("127.0.0.1", self.https_port), timeout=5) as tcp,
            context.wrap_socket(tcp, server_hostname="notes.example.org") as tls,
        ):
            self.assertEqual(tls.version(), "TLSv1.3")
            self.assertEqual(tls.selected_alpn_protocol(), "h2")

    def test_static_website_404_and_post(self) -> None:
        context = ssl.create_default_context(cafile=str(self.certificate))
        context.set_alpn_protocols(["http/1.1"])
        for method, path, expected in (
            ("GET", "/", 200),
            ("GET", "/about/", 200),
            ("GET", "/does-not-exist", 404),
            ("POST", "/", 403),
        ):
            with (
                self.subTest(method=method, path=path),
                socket.create_connection(("127.0.0.1", self.https_port), timeout=5) as tcp,
                context.wrap_socket(tcp, server_hostname="notes.example.org") as tls,
            ):
                request = f"{method} {path} HTTP/1.1\r\nHost: notes.example.org\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                tls.sendall(request.encode("ascii"))
                response = http.client.HTTPResponse(tls)
                response.begin()
                self.assertEqual(response.status, expected)
                response.read()

    def request_backend(self, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        context = ssl.create_default_context(cafile=str(self.certificate))
        context.set_alpn_protocols(["http/1.1"])
        with (
            socket.create_connection(("127.0.0.1", self.https_port), timeout=5) as tcp,
            context.wrap_socket(tcp, server_hostname="notes.example.org") as tls,
        ):
            request = f"{method} {path} HTTP/1.1\r\nHost: notes.example.org\r\nConnection: close\r\n\r\n"
            tls.sendall(request.encode("ascii"))
            response = http.client.HTTPResponse(tls, method=method)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()

    def test_three_theme_sites_and_metadata(self) -> None:
        try:
            for theme in ("docs", "status", "api"):
                with self.subTest(theme=theme):
                    source, _ = tw.select_content("notes.example.org", theme, None)
                    release = tw.create_release(
                        self.paths, source, "Test Site", "Test description", "notes.example.org", builtin=True
                    )
                    tw.activate_release(self.paths, release)
                    status, _, body = self.request_backend("/")
                    self.assertEqual(status, 200)
                    self.assertIn(('data-theme="' + theme + '"').encode(), body)
                    status, headers, _ = self.request_backend("/assets/theme.css")
                    self.assertEqual(status, 200)
                    self.assertIn("text/css", headers.get("Content-Type", ""))
                    self.assertEqual(self.request_backend("/assets/mark.svg")[0], 200)
                    if theme == "api":
                        status, headers, body = self.request_backend("/meta.json")
                        self.assertEqual(status, 200)
                        self.assertIn("application/json", headers.get("Content-Type", ""))
                        self.assertFalse(json.loads(body)["live_monitoring"])
                        self.assertEqual(self.request_backend("/meta.json", "HEAD")[2], b"")
        finally:
            tw.activate_release(self.paths, self.initial_release)

    def test_unknown_sni_rejected(self) -> None:
        context = ssl.create_default_context(cafile=str(self.certificate))
        with socket.create_connection(("127.0.0.1", self.https_port), timeout=5) as tcp:
            with self.assertRaises(ssl.SSLError):
                context.wrap_socket(tcp, server_hostname="wrong.example.org")

    def test_unknown_http_host_rejected(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=5)
        try:
            connection.request("GET", "/.well-known/acme-challenge/test-token", headers={"Host": "wrong.example.org"})
            with self.assertRaises(http.client.RemoteDisconnected):
                connection.getresponse()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
