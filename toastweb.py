#!/usr/bin/env python3
"""ToastWeb: an isolated Nginx static site with HTTP-01 certificate management."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import html
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Literal

VERSION = "0.1.0"
OWNER = "ToastWeb"
MARKER = "# Managed by ToastWeb; do not remove this ownership marker."
SERVICE = "toastweb-nginx.service"
RENEW_TIMER = "toastweb-renew.timer"
SUPPORTED = {("debian", "13"), ("ubuntu", "24.04")}
BUNDLE = Path(__file__).resolve().parent


class ToastError(RuntimeError):
    """An actionable error that does not contain credentials."""


@dataclass(frozen=True)
class Paths:
    """Production defaults; alternate roots are only used by isolated tests."""
    root: Path = Path("/")

    def at(self, value: str) -> Path:
        return self.root / value.lstrip("/")

    @property
    def config(self) -> Path:
        return self.at("/etc/toastweb/nginx.conf")

    @property
    def state(self) -> Path:
        return self.at("/etc/toastweb/site.json")

    @property
    def unit(self) -> Path:
        return self.at("/etc/systemd/system/" + SERVICE)

    @property
    def renew_unit(self) -> Path:
        return self.at("/etc/systemd/system/toastweb-renew.service")

    @property
    def renew_timer(self) -> Path:
        return self.at("/etc/systemd/system/" + RENEW_TIMER)

    @property
    def app(self) -> Path:
        return self.at("/usr/local/lib/toastweb")

    @property
    def executable(self) -> Path:
        return self.at("/usr/local/bin/toastweb")

    @property
    def hook(self) -> Path:
        return self.at("/etc/letsencrypt/renewal-hooks/deploy/toastweb.sh")

    @property
    def current(self) -> Path:
        return self.at("/srv/toastweb/current")

    @property
    def releases(self) -> Path:
        return self.at("/srv/toastweb/releases")

    @property
    def acme(self) -> Path:
        return self.at("/var/lib/toastweb/acme")

    @property
    def logs(self) -> Path:
        return self.at("/var/log/toastweb")

    @property
    def logrotate(self) -> Path:
        return self.at("/etc/logrotate.d/toastweb")

    def lineage(self, domain: str) -> Path:
        return self.at("/etc/letsencrypt/live/toastweb-" + domain)

    def managed_files(self) -> list[Path]:
        return [self.config, self.state, self.unit, self.renew_unit, self.renew_timer,
                self.executable, self.hook, self.logrotate]


def run(args: list[str], *, check: bool = True, timeout: int = 120,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Never invoke a shell or print subprocess output containing arbitrary config."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToastError(f"Cannot run {args[0]} ({type(exc).__name__}).") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1600:]
        raise ToastError(f"{args[0]} failed (exit {result.returncode}):\n{detail}")
    return result


def normalize_domain(value: str) -> str:
    value = value.strip().rstrip(".")
    try:
        domain = value.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ToastError("Invalid domain name.") from exc
    if len(domain) > 253 or "." not in domain:
        raise ToastError("Use a full domain such as notes.example.org, not a URL or wildcard.")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in domain.split(".")):
        raise ToastError("Invalid domain labels; URLs, ports and wildcards are not accepted.")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise ToastError("An IP address is not a website domain.")


def validate_email(value: str) -> str:
    if len(value) > 254 or not re.fullmatch(r"[^\s@\x00-\x1f]+@[^\s@\x00-\x1f]+\.[^\s@\x00-\x1f]+", value):
        raise ToastError("Enter a valid certificate contact email.")
    return value


def validate_port(value: int) -> int:
    if not 1024 <= value <= 65535:
        raise ToastError("The loopback HTTPS port must be between 1024 and 65535.")
    return value


def validate_text(value: str, maximum: int = 180) -> str:
    if not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ToastError(f"Site text must contain 1–{maximum} printable characters.")
    return value.strip()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToastError(f"Cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ToastError(f"Expected a JSON object: {path}")
    return value


def write_atomic(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ToastError(f"Refusing to overwrite a symlink: {path}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def nginx_quote(path: Path) -> str:
    value = str(path)
    if any(c in value for c in ("\n", "\r", "\x00", "$")):
        raise ToastError("Unsafe path for Nginx configuration.")
    return '"' + value.replace("\\", "/").replace('"', '\\"') + '"'


def render_nginx(paths: Paths, domain: str, port: int, *, tls: bool, modern_http2: bool, ipv6: bool) -> str:
    domain = normalize_domain(domain)
    validate_port(port)
    ipv6_listen = "\n        listen [::]:80;" if ipv6 else ""
    text = f"""{MARKER}
user www-data;
worker_processes 1;
pid /run/toastweb-nginx/nginx.pid;
error_log {nginx_quote(paths.logs / 'error.log')} warn;
events {{ worker_connections 512; }}
http {{
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    server_tokens off;
    sendfile on;
    keepalive_timeout 30;
    client_max_body_size 32k;
    access_log {nginx_quote(paths.logs / 'access.log')};
    server {{
        listen 80 default_server;{ipv6_listen.replace('[::]:80;', '[::]:80 default_server;')}
        server_name _;
        return 444;
    }}
    server {{
        listen 80;{ipv6_listen}
        server_name {domain};
        location ^~ /.well-known/acme-challenge/ {{
            root {nginx_quote(paths.acme)};
            default_type text/plain;
            try_files $uri =404;
            limit_except GET HEAD {{ deny all; }}
        }}
        location / {{ return 404; }}
    }}
"""
    if tls:
        listen = f"listen 127.0.0.1:{port} ssl" + (";" if modern_http2 else " http2;")
        http2 = "\n        http2 on;" if modern_http2 else ""
        lineage = paths.lineage(domain)
        text += f"""    server {{
        listen 127.0.0.1:{port} ssl default_server;
        ssl_reject_handshake on;
        ssl_protocols TLSv1.2 TLSv1.3;
    }}
    server {{
        {listen}{http2}
        server_name {domain};
        ssl_certificate {nginx_quote(lineage / 'fullchain.pem')};
        ssl_certificate_key {nginx_quote(lineage / 'privkey.pem')};
        ssl_protocols TLSv1.2 TLSv1.3;
        # Keep OpenSSL default groups; do not accidentally remove MLKEM support.
        ssl_session_cache shared:ToastWebTLS:1m;
        ssl_session_timeout 1h;
        ssl_session_tickets off;
        root {nginx_quote(paths.current)};
        index index.html;
        autoindex off;
        add_header X-Content-Type-Options nosniff always;
        add_header Referrer-Policy strict-origin-when-cross-origin always;
        if ($host != "{domain}") {{ return 444; }}
        location / {{
            try_files $uri $uri/ =404;
            limit_except GET HEAD {{ deny all; }}
        }}
        location ~ /\\. {{ deny all; }}
    }}
"""
    return text + "}\n"


def render_unit() -> str:
    return f"""{MARKER}
[Unit]
Description=ToastWeb isolated Nginx website
After=network.target

[Service]
Type=forking
PIDFile=/run/toastweb-nginx/nginx.pid
RuntimeDirectory=toastweb-nginx
ExecStartPre=/usr/sbin/nginx -t -c /etc/toastweb/nginx.conf
ExecStart=/usr/sbin/nginx -c /etc/toastweb/nginx.conf
ExecReload=/usr/sbin/nginx -s reload -c /etc/toastweb/nginx.conf
KillSignal=SIGQUIT
TimeoutStopSec=15
PrivateTmp=true
ProtectHome=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
"""


def render_renew_unit(domain: str) -> str:
    domain = normalize_domain(domain)
    return f"""{MARKER}
[Unit]
Description=Renew only the ToastWeb certificate
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/certbot renew --cert-name toastweb-{domain} --quiet --non-interactive
TimeoutStartSec=15min
PrivateTmp=true
"""


def render_renew_timer() -> str:
    return f"""{MARKER}
[Unit]
Description=Check ToastWeb certificate renewal twice daily

[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=1h
Persistent=true
Unit=toastweb-renew.service

[Install]
WantedBy=timers.target
"""


def render_hook(domain: str) -> str:
    domain = normalize_domain(domain)
    return f"""#!/bin/sh
{MARKER}
set -eu
# Certbot runs deploy hooks for all lineages: only reload our own site.
[ "${{RENEWED_LINEAGE:-}}" = "/etc/letsencrypt/live/toastweb-{domain}" ] || exit 0
/usr/sbin/nginx -t -c /etc/toastweb/nginx.conf
/bin/systemctl reload {SERVICE}
"""


def render_logrotate() -> str:
    return f"""{MARKER}
/var/log/toastweb/*.log {{
    daily
    maxsize 10M
    rotate 7
    missingok
    notifempty
    compress
    delaycompress
    sharedscripts
    postrotate
        /bin/systemctl kill --kill-who=main --signal=USR1 {SERVICE} >/dev/null 2>&1 || true
    endscript
}}
"""


def load_state(paths: Paths) -> dict[str, Any] | None:
    if not paths.state.exists():
        return None
    state = read_json(paths.state)
    if state.get("managed_by") != OWNER:
        raise ToastError("Existing site.json is not owned by ToastWeb.")
    state["domain"] = normalize_domain(str(state.get("domain", "")))
    validate_port(int(state.get("port", 0)))
    for key in ("active_release", "previous_release"):
        value = state.get(key)
        if value and not re.fullmatch(r"[A-Za-z0-9_-]+", str(value)):
            raise ToastError("Invalid release identifier in site state.")
    return state


def assert_owned(paths: Paths) -> None:
    for directory in (paths.config.parent, paths.app, paths.current.parent, paths.releases,
                      paths.acme.parent, paths.acme, paths.logs):
        if directory.is_symlink():
            raise ToastError(f"Refusing a symlink at a managed directory: {directory}")
    for path in paths.managed_files():
        if path.is_symlink():
            raise ToastError(f"Refusing a symlink at a managed file: {path}")
        if not path.exists():
            continue
        if path == paths.state:
            load_state(paths)
        elif MARKER not in path.read_text(encoding="utf-8")[:500]:
            raise ToastError(f"Refusing to replace a file not owned by ToastWeb: {path}")
    if paths.current.exists() and not paths.current.is_symlink():
        raise ToastError("The current site path already exists and is not a ToastWeb release symlink.")
    if paths.current.is_symlink() and not paths.current.resolve().is_relative_to(paths.releases.resolve()):
        raise ToastError("Current site symlink points outside ToastWeb releases.")
    owner_file = paths.app / ".toastweb-managed"
    if paths.app.exists() and (not owner_file.is_file() or owner_file.read_text(encoding="utf-8").strip() != OWNER):
        raise ToastError("Existing application directory is not a ToastWeb installation.")


def prepare_public_paths(paths: Paths) -> None:
    # Static files must remain traversable by Nginx workers even with root umask 077.
    # Backups/state/private keys are not included and keep restrictive permissions.
    directories = [paths.current.parent, paths.releases, paths.acme.parent, paths.acme,
                   paths.acme / ".well-known", paths.acme / ".well-known/acme-challenge"]
    for directory in directories:
        if directory.is_symlink():
            raise ToastError(f"Refusing a symlink in public data directories: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)


def create_release(paths: Paths, source: Path, title: str, description: str, domain: str, *, builtin: bool) -> str:
    source = source.resolve()
    if not (source / "index.html").is_file():
        raise ToastError("Site content must contain index.html.")
    entries = list(source.rglob("*"))
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in entries):
        raise ToastError("Site content cannot contain symlinks or special files.")
    if len(entries) > 20000 or sum(p.stat().st_size for p in entries if p.is_file()) > 100 * 1024 * 1024:
        raise ToastError("Site content exceeds the 20,000-entry / 100 MiB safety limit.")
    if any(part.startswith(".") for p in entries for part in p.relative_to(source).parts):
        raise ToastError("Hidden files/directories are not accepted in public site content.")
    release = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    destination = paths.releases / release
    prepare_public_paths(paths)
    shutil.copytree(source, destination)
    substitutions = {"{{SITE_TITLE}}": html.escape(title, quote=True),
                     "{{SITE_DESCRIPTION}}": html.escape(description, quote=True),
                     "{{DOMAIN}}": html.escape(domain, quote=True),
                     "{{YEAR}}": str(dt.datetime.now(dt.timezone.utc).year)}
    for path in destination.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
        if builtin and path.suffix in {".html", ".xml", ".txt"}:
            text = path.read_text(encoding="utf-8")
            for key, value in substitutions.items():
                text = text.replace(key, value)
            path.write_text(text, encoding="utf-8", newline="\n")
    destination.chmod(0o755)
    return release


def activate_release(paths: Paths, release: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", release):
        raise ToastError("Invalid release identifier.")
    target = paths.releases / release
    if target.is_symlink() or not (target / "index.html").is_file():
        raise ToastError("Release is missing or unsafe.")
    temporary = paths.current.with_name(".current-" + uuid.uuid4().hex[:8])
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, paths.current)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


class Transaction:
    """Recover project-owned config/content pointers, not APT or CA side effects."""
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.backup = paths.at("/var/lib/toastweb/backups") / (
            dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
        self.files: list[tuple[Path, Path | None]] = []
        self.old_link = os.readlink(paths.current) if paths.current.is_symlink() else None
        self.had_app = paths.app.exists()
        self.was_active = False
        self.was_enabled = False
        self.timer_was_active = False
        self.timer_was_enabled = False

    def __enter__(self) -> Transaction:
        self.backup.mkdir(parents=True, mode=0o700)
        self.backup.chmod(0o700)
        self.was_active = run(["systemctl", "is-active", "--quiet", SERVICE], check=False).returncode == 0
        self.was_enabled = run(["systemctl", "is-enabled", "--quiet", SERVICE], check=False).returncode == 0
        self.timer_was_active = run(["systemctl", "is-active", "--quiet", RENEW_TIMER], check=False).returncode == 0
        self.timer_was_enabled = run(["systemctl", "is-enabled", "--quiet", RENEW_TIMER], check=False).returncode == 0
        for number, path in enumerate(self.paths.managed_files()):
            saved = self.backup / str(number) if path.exists() else None
            if saved:
                shutil.copy2(path, saved)
            self.files.append((path, saved))
        if self.had_app:
            shutil.copytree(self.paths.app, self.backup / "app")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Literal[False]:
        if exc_type is None:
            print(f"Configuration backup: {self.backup}")
            return False
        print(f"Operation failed; restoring ToastWeb-owned files from {self.backup}", file=sys.stderr)
        try:
            if not self.timer_was_active:
                run(["systemctl", "stop", RENEW_TIMER], check=False)
            if not self.timer_was_enabled:
                run(["systemctl", "disable", RENEW_TIMER], check=False)
            if not self.was_active:
                run(["systemctl", "stop", SERVICE], check=False)
            if not self.was_enabled:
                run(["systemctl", "disable", SERVICE], check=False)
            for path, saved in self.files:
                if saved:
                    shutil.copy2(saved, path)
                elif path.exists():
                    path.unlink()
            if self.paths.current.is_symlink():
                self.paths.current.unlink()
            if self.old_link is not None:
                self.paths.current.symlink_to(self.old_link, target_is_directory=True)
            if self.paths.app.exists():
                shutil.rmtree(self.paths.app)
            if self.had_app:
                shutil.copytree(self.backup / "app", self.paths.app)
            run(["systemctl", "daemon-reload"], check=False)
            if self.was_active:
                run(["/usr/sbin/nginx", "-t", "-c", str(self.paths.config)])
                active = run(["systemctl", "is-active", "--quiet", SERVICE], check=False).returncode == 0
                run(["systemctl", "reload" if active else "start", SERVICE])
            if self.was_enabled:
                run(["systemctl", "enable", SERVICE], check=False)
            if self.timer_was_enabled:
                run(["systemctl", "enable", RENEW_TIMER], check=False)
            if self.timer_was_active:
                run(["systemctl", "start", RENEW_TIMER], check=False)
        except (OSError, ToastError) as rollback_error:
            print(f"Automatic recovery needs attention: {rollback_error}. Backup retained.", file=sys.stderr)
        return False


@contextlib.contextmanager
def operation_lock(paths: Paths) -> Iterator[None]:
    fcntl: Any = import_module("fcntl")  # Linux-only; rendering/tests can run elsewhere.
    path = paths.at("/run/lock/toastweb.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ToastError("Another ToastWeb operation is already running.") from exc
        yield


def require_host(paths: Paths) -> None:
    if sys.platform != "linux" or getattr(os, "geteuid", lambda: 1)() != 0:
        raise ToastError("Deployment commands require root on a supported Linux VPS. Use plan for a safe preview.")
    values: dict[str, str] = {}
    for line in paths.at("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip('"')
    if (values.get("ID"), values.get("VERSION_ID")) not in SUPPORTED:
        raise ToastError("V1 supports Debian 13 and Ubuntu 24.04 only; no changes made.")
    if not paths.at("/run/systemd/system").exists():
        raise ToastError("A running systemd host is required; containers without systemd are not supported.")


def local_addresses() -> set[str]:
    result = run(["ip", "-j", "address", "show"])
    try:
        records = json.loads(result.stdout)
        return {str(ipaddress.ip_address(info["local"])) for entry in records for info in entry.get("addr_info", [])}
    except (ValueError, KeyError, TypeError) as exc:
        raise ToastError("Cannot inspect local interface addresses.") from exc


def resolve_domain(domain: str) -> set[str]:
    try:
        return {str(ipaddress.ip_address(item[4][0])) for item in socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)}
    except (OSError, ValueError) as exc:
        raise ToastError("Domain resolution failed; configure gray-cloud DNS before installing.") from exc


def check_dns(domain: str, expected: list[str]) -> set[str]:
    addresses = resolve_domain(domain)
    allowed = local_addresses()
    for value in expected:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ToastError("--expected-ip must be a valid IP address.") from exc
        if not address.is_global:
            raise ToastError("--expected-ip must be a public address.")
        allowed.add(str(address))
    if not addresses or any(not ipaddress.ip_address(a).is_global or a not in allowed for a in addresses):
        raise ToastError("All resolved A/AAAA addresses must point to this VPS. Check DNS-only mode, stale AAAA records, or specify --expected-ip for NAT.")
    return addresses


def check_ports(port: int) -> None:
    main = run(["systemctl", "show", SERVICE, "--property=MainPID", "--value"], check=False).stdout.strip()
    for line in run(["ss", "-H", "-lntp"]).stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        try:
            listening = int(fields[3].rsplit(":", 1)[1])
        except (ValueError, IndexError):
            continue
        if listening in {80, port} and not (main.isdecimal() and main != "0" and f"pid={main}," in line):
            raise ToastError(f"TCP {listening} is already used by another service; ToastWeb will not stop or replace it.")


def check_other_acme(paths: Paths, allowed: bool) -> None:
    standalone = False
    directory = paths.at("/etc/letsencrypt/renewal")
    if directory.exists():
        standalone = any("authenticator = standalone" in p.read_text(encoding="utf-8")
                         for p in directory.glob("*.conf") if not p.name.startswith("toastweb-"))
    cron = run(["crontab", "-l"], check=False).stdout if shutil.which("crontab") else ""
    legacy = paths.at("/root/.acme.sh/acme.sh").exists() or any(
        "acme.sh" in line for line in cron.splitlines() if not line.lstrip().startswith("#"))
    if (legacy or standalone) and not allowed:
        raise ToastError("Existing ACME/standalone certificate automation detected. Review possible port-80 conflicts first, then explicitly pass --allow-existing-acme if safe. No existing jobs were changed.")


def ensure_dependencies(paths: Paths) -> None:
    packages = ["nginx", "certbot", "openssl", "ca-certificates", "iproute2", "logrotate"]
    missing = []
    for package in packages:
        status = run(["dpkg-query", "-W", "-f=${db:Status-Abbrev}", package], check=False)
        if status.returncode or not status.stdout.startswith("ii"):
            missing.append(package)
    if not missing:
        return
    nginx_unit = run(["systemctl", "show", "nginx.service", "--property=LoadState", "--value"], check=False)
    fresh_nginx = not paths.at("/usr/sbin/nginx").exists() and nginx_unit.stdout.strip() != "loaded"
    certbot_timer = run(["systemctl", "show", "certbot.timer", "--property=LoadState", "--value"], check=False)
    fresh_certbot = not paths.at("/usr/bin/certbot").exists() and certbot_timer.stdout.strip() != "loaded"
    policy = paths.at("/usr/sbin/policy-rc.d")
    if policy.exists() or policy.is_symlink():
        raise ToastError("An existing policy-rc.d will not be replaced. Install nginx/certbot/openssl/ca-certificates/iproute2 with service auto-start controlled by your administrator, then rerun.")
    print("Installing required system packages; suppressing package-triggered service starts/restarts.")
    apt_env = dict(os.environ, DEBIAN_FRONTEND="noninteractive", NEEDRESTART_MODE="l")
    run(["apt-get", "update"], timeout=600, env=apt_env)
    body = "#!/bin/sh\n# Temporary ToastWeb package-install guard\nexit 101\n"
    try:
        write_atomic(policy, body, 0o755)
        run(["apt-get", "install", "-y", "--no-install-recommends",
             "-o", "Dpkg::Options::=--force-confold", *missing], timeout=900, env=apt_env)
    finally:
        if policy.is_file() and policy.read_text(encoding="utf-8") == body:
            policy.unlink()
        if fresh_nginx and paths.at("/usr/sbin/nginx").exists():
            # Never disable a distribution service that existed before this operation.
            run(["systemctl", "disable", "nginx.service"])
        if fresh_certbot and paths.at("/usr/bin/certbot").exists():
            run(["systemctl", "disable", "--now", "certbot.timer"])


def modern_nginx() -> bool:
    result = run(["/usr/sbin/nginx", "-v"])
    match = re.search(r"nginx/(\d+)\.(\d+)\.(\d+)", result.stdout + result.stderr)
    if not match:
        raise ToastError("Cannot determine the installed Nginx version.")
    version = tuple(map(int, match.groups()))
    if version < (1, 19, 4):
        raise ToastError("Nginx 1.19.4+ is required for unknown-SNI rejection.")
    return version >= (1, 25, 1)


def ipv6_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::", 0))
        return True
    except OSError:
        return False


def install_bundle(paths: Paths) -> None:
    if paths.app.resolve() == BUNDLE:
        return
    try:
        paths.app.mkdir(parents=True, exist_ok=True)
        write_atomic(paths.app / ".toastweb-managed", OWNER + "\n")
        for filename in ("toastweb.py", "VERSION"):
            shutil.copy2(BUNDLE / filename, paths.app / filename)
        destination = paths.app / "site"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(BUNDLE / "site", destination)
    except OSError as exc:
        raise ToastError("Cannot install the ToastWeb application bundle.") from exc


def http_probe(paths: Paths, domain: str, addresses: set[str]) -> None:
    relative = Path(".well-known/acme-challenge") / ("toastweb-probe-" + uuid.uuid4().hex)
    token = uuid.uuid4().hex
    challenge = paths.acme / relative
    write_atomic(challenge, token)
    try:
        for address in {"127.0.0.1", *addresses}:
            connection = http.client.HTTPConnection(address, 80, timeout=8)
            try:
                connection.request("GET", "/" + relative.as_posix(), headers={"Host": domain})
                response = connection.getresponse()
                if response.status != 200 or response.read(128).decode("ascii") != token:
                    raise ToastError("HTTP-01 preflight returned an unexpected response.")
            except (OSError, http.client.HTTPException, UnicodeError) as exc:
                raise ToastError("HTTP-01 preflight failed; check port 80, DNS and routing.") from exc
            finally:
                connection.close()
    finally:
        challenge.unlink(missing_ok=True)


def certbot_command(paths: Paths, domain: str, email: str) -> list[str]:
    return ["certbot", "certonly", "--non-interactive", "--agree-tos", "--email", validate_email(email),
            "--webroot", "--webroot-path", str(paths.acme), "--preferred-challenges", "http",
            "--cert-name", "toastweb-" + normalize_domain(domain), "--domain", normalize_domain(domain),
            "--keep-until-expiring", "--key-type", "ecdsa"]


def confirm(args: argparse.Namespace) -> None:
    for flag, prompt in (("accept_tos", "Agree to the Let's Encrypt subscriber agreement (https://letsencrypt.org/repository/)?"),
                         ("confirm_port_80", "Confirm public TCP 80 is allowed and may remain available for automatic renewal? ToastWeb will NOT change your firewall.")):
        if getattr(args, flag):
            continue
        if args.non_interactive or not sys.stdin.isatty():
            raise ToastError("Explicit --accept-tos and --confirm-port-80 are required in non-interactive mode.")
        if input(prompt + " [y/N] ").strip().lower() not in {"y", "yes"}:
            raise ToastError("Cancelled before system changes.")


def install(args: argparse.Namespace, paths: Paths) -> None:
    require_host(paths)
    domain = normalize_domain(args.domain)
    email = validate_email(args.email)
    port = validate_port(args.port)
    title = validate_text(args.title)
    description = validate_text(args.description)
    with operation_lock(paths):
        assert_owned(paths)
        old = load_state(paths)
        if old and (old["domain"] != domain or old["port"] != port):
            raise ToastError("V1 manages one site. Domain/port migration requires an explicit manual plan; existing service was not changed.")
        check_ports(port)
        addresses = check_dns(domain, args.expected_ip)
        check_other_acme(paths, args.allow_existing_acme)
        confirm(args)
        source = Path(args.content_dir).resolve() if args.content_dir else BUNDLE / "site"
        if old and old.get("content_type") == "custom" and not args.content_dir:
            raise ToastError("This site uses custom content; supply --content-dir to avoid replacing it with the default template.")
        if not (source / "index.html").is_file():
            raise ToastError("Content source must contain index.html.")
        ensure_dependencies(paths)
        modern = modern_nginx()
        ipv6 = ipv6_available()
        with Transaction(paths):
            install_bundle(paths)
            paths.logs.mkdir(parents=True, exist_ok=True)
            paths.acme.mkdir(parents=True, exist_ok=True)
            release = create_release(paths, source, title, description, domain, builtin=not args.content_dir)
            activate_release(paths, release)
            lineage = paths.lineage(domain)
            bootstrap_tls = bool(old and (lineage / "fullchain.pem").exists() and (lineage / "privkey.pem").exists())
            write_atomic(paths.config, render_nginx(paths, domain, port, tls=bootstrap_tls, modern_http2=modern, ipv6=ipv6))
            write_atomic(paths.unit, render_unit())
            write_atomic(paths.renew_unit, render_renew_unit(domain))
            write_atomic(paths.renew_timer, render_renew_timer())
            write_atomic(paths.executable, f"#!/bin/sh\n{MARKER}\nexec /usr/bin/python3 /usr/local/lib/toastweb/toastweb.py \"$@\"\n", 0o755)
            run(["systemctl", "daemon-reload"])
            run(["/usr/sbin/nginx", "-t", "-c", str(paths.config)])
            run(["systemctl", "enable", "--now", SERVICE])
            run(["systemctl", "reload", SERVICE])
            http_probe(paths, domain, addresses)
            print("HTTP preflight passed locally; only the CA can verify Internet-wide port-80 reachability.")
            run(certbot_command(paths, domain, email), timeout=600)
            if not (lineage / "fullchain.pem").is_file() or not (lineage / "privkey.pem").is_file():
                raise ToastError("Certbot did not create the expected certificate lineage.")
            write_atomic(paths.config, render_nginx(paths, domain, port, tls=True, modern_http2=modern, ipv6=ipv6))
            write_atomic(paths.hook, render_hook(domain), 0o755)
            write_atomic(paths.logrotate, render_logrotate())
            run(["/usr/sbin/nginx", "-t", "-c", str(paths.config)])
            run(["systemctl", "reload", SERVICE])
            run(["systemctl", "enable", "--now", RENEW_TIMER])
            state = {"managed_by": OWNER, "version": VERSION, "domain": domain, "email": email,
                     "port": port, "title": title, "description": description,
                     "content_type": "custom" if args.content_dir else "builtin",
                     "active_release": release, "previous_release": old.get("active_release") if old else None,
                     "previous_content_type": old.get("content_type") if old else None,
                     "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            write_atomic(paths.state, json.dumps(state, ensure_ascii=False, indent=2) + "\n", 0o600)
        print(f"Ready: HTTPS backend 127.0.0.1:{port}, SNI {domain}.")
        print("REALITY/DNS/firewall were NOT modified. Coordinate client SNI and Xray changes separately.")
        print("Next: sudo toastweb doctor && sudo toastweb renew-test")


def update(args: argparse.Namespace, paths: Paths, *, rollback: bool = False) -> None:
    require_host(paths)
    with operation_lock(paths):
        assert_owned(paths)
        state = load_state(paths)
        if not state:
            raise ToastError("ToastWeb is not installed.")
        with Transaction(paths):
            previous = state["active_release"]
            previous_type = state.get("content_type", "builtin")
            if rollback:
                release = state.get("previous_release")
                if not release:
                    raise ToastError("No previous content release is available.")
                state["content_type"] = state.get("previous_content_type") or previous_type
            else:
                if state.get("content_type") == "custom" and not args.content_dir:
                    raise ToastError("Supply --content-dir for a custom website.")
                source = Path(args.content_dir).resolve() if args.content_dir else BUNDLE / "site"
                release = create_release(paths, source, state["title"], state["description"], state["domain"], builtin=not args.content_dir)
                state["content_type"] = "custom" if args.content_dir else "builtin"
            activate_release(paths, release)
            run(["/usr/sbin/nginx", "-t", "-c", str(paths.config)])
            run(["systemctl", "reload", SERVICE])
            state.update(active_release=release, previous_release=previous, previous_content_type=previous_type,
                         updated_at=dt.datetime.now(dt.timezone.utc).isoformat())
            write_atomic(paths.state, json.dumps(state, ensure_ascii=False, indent=2) + "\n", 0o600)
        print(f"Active content release: {release}")


def status(paths: Paths) -> None:
    state = load_state(paths)
    if not state:
        raise ToastError("ToastWeb is not installed.")
    visible = {k: state.get(k) for k in ("version", "domain", "port", "content_type", "active_release", "previous_release", "updated_at")}
    visible["service"] = run(["systemctl", "is-active", SERVICE], check=False).stdout.strip()
    visible["renewal_timer"] = run(["systemctl", "is-active", RENEW_TIMER], check=False).stdout.strip()
    print(json.dumps(visible, ensure_ascii=False, indent=2))


def doctor(paths: Paths) -> None:
    state = load_state(paths)
    if not state:
        raise ToastError("ToastWeb is not installed.")
    assert_owned(paths)
    run(["/usr/sbin/nginx", "-t", "-c", str(paths.config)])
    run(["systemctl", "is-active", "--quiet", SERVICE])
    run(["systemctl", "is-enabled", "--quiet", RENEW_TIMER])
    run(["systemctl", "is-active", "--quiet", RENEW_TIMER])
    certificate = paths.lineage(state["domain"]) / "fullchain.pem"
    run(["openssl", "x509", "-in", str(certificate), "-noout", "-checkend", "0"])
    run(["openssl", "x509", "-in", str(certificate), "-noout", "-checkhost", state["domain"]])
    context = ssl.create_default_context()
    context.set_alpn_protocols(["h2", "http/1.1"])
    try:
        with socket.create_connection(("127.0.0.1", state["port"]), timeout=8) as tcp, context.wrap_socket(tcp, server_hostname=state["domain"]) as tls:
            if tls.version() != "TLSv1.3" or tls.selected_alpn_protocol() != "h2":
                raise ToastError("Backend did not negotiate TLS 1.3 + h2.")
            print(f"Verified backend: {tls.version()}, {tls.selected_alpn_protocol()}, trusted certificate.")
    except (OSError, ssl.SSLError) as exc:
        raise ToastError(f"Backend TLS verification failed ({type(exc).__name__}).") from exc
    print("Nginx configuration, certificate and renewal timer checks passed.")
    print("This does not test public port-80 reachability, CA renewal, MLKEM, or the REALITY client. Run renew-test and validate REALITY separately.")


def renew_test(paths: Paths) -> None:
    require_host(paths)
    state = load_state(paths)
    if not state:
        raise ToastError("ToastWeb is not installed.")
    run(["certbot", "renew", "--cert-name", "toastweb-" + state["domain"], "--dry-run", "--non-interactive"], timeout=600)
    run(["/usr/sbin/nginx", "-t", "-c", str(paths.config)])
    print("CA renewal dry-run passed. Existing production certificates were not replaced; deploy hooks are not exercised by this test.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--version", action="version", version=VERSION)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("install", "plan"):
        sub = commands.add_parser(name, help="Install the site" if name == "install" else "Print a no-change deployment plan")
        sub.add_argument("--domain")
        sub.add_argument("--email")
        sub.add_argument("--port", type=int, default=8443)
        sub.add_argument("--title", default="Toast Notes")
        sub.add_argument("--description", default="A small place for notes, ideas, and useful things.")
        sub.add_argument("--content-dir", help="A prebuilt static website containing index.html")
        sub.add_argument("--expected-ip", action="append", default=[], help="Explicit public address for NAT; repeat if needed")
        sub.add_argument("--accept-tos", action="store_true")
        sub.add_argument("--confirm-port-80", action="store_true")
        sub.add_argument("--allow-existing-acme", action="store_true")
        sub.add_argument("--non-interactive", action="store_true")
    sub = commands.add_parser("update", help="Publish static content without changing certificate or domain")
    sub.add_argument("--content-dir")
    for name in ("rollback", "status", "doctor", "renew-test"):
        commands.add_parser(name)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    paths = Paths()
    try:
        if args.command in {"install", "plan"}:
            for name, prompt in (("domain", "Website domain"), ("email", "Certificate contact email")):
                if not getattr(args, name):
                    if args.non_interactive or not sys.stdin.isatty():
                        raise ToastError(f"--{name} is required without an interactive terminal.")
                    setattr(args, name, input(prompt + ": ").strip())
            args.domain = normalize_domain(args.domain)
            args.email = validate_email(args.email)
            validate_port(args.port)
            validate_text(args.title)
            validate_text(args.description)
        if args.command == "plan":
            print(json.dumps({"version": VERSION, "domain": args.domain, "https_backend": f"127.0.0.1:{args.port}",
                              "http_validation_port": 80, "certificate_method": "HTTP-01 webroot",
                              "service": SERVICE, "changes": ["APT dependencies", "ToastWeb-owned files/service", "ToastWeb certificate", "ToastWeb renewal timer"],
                              "never_changes": ["DNS", "Xray/x-ui", "subscriptions", "firewall", "kernel parameters"],
                              "executed": False}, ensure_ascii=False, indent=2))
        elif args.command == "install":
            install(args, paths)
        elif args.command in {"update", "rollback"}:
            update(args, paths, rollback=args.command == "rollback")
        elif args.command == "status":
            status(paths)
        elif args.command == "doctor":
            doctor(paths)
        elif args.command == "renew-test":
            renew_test(paths)
        return 0
    except (ToastError, OSError, ValueError) as exc:
        print(f"ToastWeb: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ToastWeb: cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
