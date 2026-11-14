"""Read-only, local diagnostics for a ToastWeb backend.

This module deliberately does not import ``toastweb``: the CLI is also executed as a
script, and importing it here could run module-level code twice or create a cycle.
Local checks use fixed command arguments; explicitly requested DNS queries use a
fixed HTTPS resolver. Reports never print raw subprocess output, access-log lines,
private keys, or certificate material.
"""

from __future__ import annotations

import email.utils
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SCOPE = "local_backend"
NOT_TESTED = [
    "public_tcp_80",
    "public_https_443",
    "ca_renewal",
    "mlkem",
    "reality_authentication",
]
CERTIFICATE_READ_LIMIT = 2 * 1024 * 1024
ACCESS_LOG_READ_LIMIT = 64 * 1024
ACCESS_LOG_LINE_LIMIT = 100
DNS_RESPONSE_LIMIT = 64 * 1024
CAA_LOOKUP_LIMIT = 8
ENABLED_UNIT_STATES = {"enabled", "enabled-runtime", "static", "indirect"}
PEM_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)
SAFE_CAA_MESSAGE = "CAA records appear to authorize Let's Encrypt (parameters are not fully validated)."


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp() -> str:
    return _utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def _invoke(run_command: Callable[..., Any], args: list[str], timeout: int) -> Any | None:
    try:
        return run_command(args, check=False, timeout=timeout)
    except Exception:
        return None


def _returncode(result: Any) -> int | None:
    value = getattr(result, "returncode", None)
    return value if isinstance(value, int) else None


def _property_output(result: Any) -> str | None:
    value = getattr(result, "stdout", None)
    return value if isinstance(value, str) else None


def _parse_properties(output: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip():
            properties[key.strip()] = value.strip()
    return properties


def _show_properties(
    run_command: Callable[..., Any], unit: str, names: str
) -> tuple[dict[str, str] | None, str, int | None]:
    """Run one ``systemctl show`` and return (properties, outcome, exit code)."""
    result = _invoke(
        run_command,
        ["systemctl", "show", unit, "--property=" + names],
        8,
    )
    if result is None:
        return None, "exception", None
    code = _returncode(result)
    if code is None:
        return None, "invalid", None
    if code != 0:
        return None, "failed", code
    output = _property_output(result)
    if output is None:
        return None, "invalid", None
    return _parse_properties(output), "ok", 0


def _int_value(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _duration_usec(value: str | None) -> int | None:
    """Parse systemd's USec properties, which may render as ``10s`` or ``1min 30s``."""
    if value is None:
        return None
    if value.strip() == "infinity":
        return 0
    units = {
        "us": 1,
        "ms": 1_000,
        "s": 1_000_000,
        "min": 60_000_000,
        "h": 3_600_000_000,
        "d": 86_400_000_000,
        "w": 604_800_000_000,
    }
    position = 0
    total = 0.0
    found = False
    while position < len(value):
        match = re.match(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?", value[position:])
        if match is None:
            return None
        amount = float(match.group(1))
        unit = (match.group(2) or "us").lower()
        multiplier = units.get(unit)
        if multiplier is None:
            return None
        total += amount * multiplier
        found = True
        position += match.end()
    return int(total) if found else None


def _is_loopback_host(host: str) -> bool | None:
    """Classify a listener host; ``None`` means the address was not understood."""
    if host in {"", "*"}:
        return False
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    host = host.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return bool(address.ipv4_mapped.is_loopback)
    return bool(address.is_loopback)


def _endpoint(token: str) -> tuple[str, int] | None:
    """Parse an ``ss`` local endpoint such as ``127.0.0.1:8443`` or ``[::1]:8443``."""
    if token.startswith("["):
        closing = token.rfind("]")
        if closing < 0 or closing + 1 >= len(token) or token[closing + 1] != ":":
            return None
        host, port = token[1:closing], token[closing + 2 :]
    else:
        host, separator, port = token.rpartition(":")
        if not separator:
            return None
    try:
        return host, int(port)
    except ValueError:
        return None


def _listener_assessment(output: str, port: int, main_pid: int | None) -> dict[str, Any]:
    own: list[str] = []
    foreign: list[str] = []
    unknown = main_pid is None or main_pid <= 1
    target_lines = 0
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "LISTEN":
            continue
        endpoint = _endpoint(parts[3])
        if endpoint is None:
            if re.search(r":" + str(port) + r"(?:\s|$)", line):
                target_lines += 1
                unknown = True
            continue
        host, found_port = endpoint
        if found_port != port:
            continue
        target_lines += 1
        pids = {int(value) for value in re.findall(r"\bpid=(\d+)\b", line)}
        if not pids:
            unknown = True
        elif main_pid is not None and main_pid in pids:
            own.append(host)
        else:
            foreign.append(host)
    return {"own": own, "foreign": foreign, "unknown": unknown, "target_lines": target_lines}


def _read_limited(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("diagnostic input exceeds its size limit")
    return data


def _first_pem_certificate(data: bytes) -> bytes | None:
    match = PEM_CERTIFICATE.search(data)
    return match.group(0) if match else None


def _certificate_fingerprint(pem: bytes) -> str:
    der = ssl.PEM_cert_to_DER_cert(pem.decode("ascii"))
    return hashlib.sha256(der).hexdigest()


def _parse_expiry(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _probe_h2(domain: str, port: int) -> dict[str, Any]:
    """Perform a verified TLS 1.3 + h2 handshake without sending HTTP data."""
    context = ssl.create_default_context()
    context.set_alpn_protocols(["h2"])
    with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
        with context.wrap_socket(raw, server_hostname=domain) as tls:
            peer = tls.getpeercert()
            not_after = peer.get("notAfter") if isinstance(peer, dict) else None
            return {
                "version": tls.version(),
                "alpn": tls.selected_alpn_protocol(),
                "fingerprint": hashlib.sha256(tls.getpeercert(binary_form=True) or b"").hexdigest(),
                "not_after": not_after,
            }


def _probe_http11(domain: str, port: int) -> dict[str, Any]:
    """Make one HTTP/1.1 HEAD request on a separately negotiated connection."""
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    request = ("HEAD / HTTP/1.1\r\nHost: " + domain + "\r\nConnection: close\r\n\r\n").encode("ascii")
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=domain) as tls:
            alpn = tls.selected_alpn_protocol()
            if alpn != "http/1.1":
                return {"status": None, "alpn": alpn}
            tls.sendall(request)
            response = b""
            while len(response) < 8192:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                response += chunk
                if b"\r\n\r\n" in response:
                    break
    first_line = response.split(b"\r\n", 1)[0]
    match = re.fullmatch(rb"HTTP/(?:1\.[01]) ([0-9]{3})(?: .*)?", first_line)
    status = int(match.group(1)) if match else None
    return {"status": status, "alpn": alpn}


def _read_access_log(path: Path) -> list[str] | None:
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        offset = max(0, size - ACCESS_LOG_READ_LIMIT)
        handle.seek(offset)
        data = handle.read(ACCESS_LOG_READ_LIMIT)
    text = data.decode("utf-8", "replace")
    if offset and "\n" in text:
        text = text.split("\n", 1)[1]
    return text.splitlines()[-ACCESS_LOG_LINE_LIMIT:]


def _source_is_loopback(source: str) -> bool | None:
    try:
        address = ipaddress.ip_address(source)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return bool(address.ipv4_mapped.is_loopback)
    return bool(address.is_loopback)


def _fetch_dns_json(name: str, record_type: str) -> dict[str, Any]:
    """Fetch a bounded DNS-over-HTTPS answer from the fixed dns.google endpoint."""
    query = urllib.parse.urlencode({"name": name, "type": record_type, "do": "1"})
    connection = http.client.HTTPSConnection("dns.google", timeout=5)
    try:
        connection.request("GET", "/resolve?" + query, headers={"Accept": "application/dns-json"})
        response = connection.getresponse()
        # Do not follow redirects to another origin or a private-network endpoint.
        if response.status != 200:
            raise ValueError("DNS resolver returned a non-200 response")
        payload = response.read(DNS_RESPONSE_LIMIT + 1)
    finally:
        connection.close()
    if len(payload) > DNS_RESPONSE_LIMIT:
        raise ValueError("DNS response exceeds the diagnostic size limit")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("DNS response was not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("DNS response was not an object")
    return data


def _caa_answers(domain: str) -> tuple[str, list[str], int]:
    """Walk up to the parent domains and return the first CAA record set found."""
    labels = [label for label in domain.split(".") if label]
    queries = 0
    for offset in range(min(len(labels), CAA_LOOKUP_LIMIT)):
        name = ".".join(labels[offset:])
        queries += 1
        try:
            data = _fetch_dns_json(name, "CAA")
        except Exception:
            return "unknown", [], queries
        status = data.get("Status")
        if status not in {None, 0}:
            return "unknown", [], queries
        answers = data.get("Answer")
        if answers is None:
            return "none", [], queries
        if not isinstance(answers, list):
            return "unknown", [], queries
        records: list[str] = []
        for answer in answers:
            if not isinstance(answer, dict) or answer.get("type") != 257:
                continue
            value = answer.get("data")
            if isinstance(value, str):
                records.append(value)
        if records:
            return "found", records, queries
    return "none", [], queries


def _caa_assessment(records: list[str]) -> dict[str, Any]:
    issue_records: list[str] = []
    malformed = 0
    for record in records:
        parts = record.split(None, 2)
        if len(parts) != 3:
            malformed += 1
            continue
        tag = parts[1].lower()
        if tag != "issue":
            continue
        value = parts[2].strip().strip('"')
        issue_records.append(value.split(";", 1)[0].strip().lower())
    letsencrypt = any(value == "letsencrypt.org" or value.startswith("letsencrypt.org;") for value in issue_records)
    restricting = bool(issue_records) and not letsencrypt
    return {
        "issue_records": len(issue_records),
        "malformed": malformed,
        "letsencrypt": letsencrypt,
        "restricting": restricting,
    }


def _dns_checks(domain: str, add: Callable[..., None]) -> None:
    if not domain:
        add("dns_caa", "unknown", "CAA status could not be checked without a domain.", False)
        add("dns_dnssec", "unknown", "DNSSEC status could not be checked without a domain.", False)
        return

    caa_state, records, queries = _caa_answers(domain)
    if caa_state == "unknown":
        add(
            "dns_caa",
            "unknown",
            "CAA lookup did not complete; this does not change local backend safety.",
            False,
            {"queries": queries},
        )
    elif caa_state == "none":
        add(
            "dns_caa",
            "info",
            "No CAA records were found while walking parent domains.",
            False,
            {"queries": queries},
        )
    else:
        assessment = _caa_assessment(records)
        if assessment["issue_records"] == 0:
            add(
                "dns_caa",
                "info",
                "CAA records were found, but no issue records were interpreted.",
                False,
                {"records": len(records), "malformed": assessment["malformed"]},
            )
        elif assessment["letsencrypt"]:
            add(
                "dns_caa",
                "info",
                SAFE_CAA_MESSAGE,
                False,
                {"records": len(records), "issue_records": assessment["issue_records"]},
            )
        elif assessment["restricting"]:
            add(
                "dns_caa",
                "warning",
                "CAA issue records do not appear to authorize Let's Encrypt.",
                False,
                {"records": len(records), "issue_records": assessment["issue_records"]},
            )
        else:
            add(
                "dns_caa",
                "unknown",
                "CAA records could not be interpreted safely.",
                False,
                {"records": len(records), "malformed": assessment["malformed"]},
            )

    try:
        data = _fetch_dns_json(domain, "A")
    except Exception:
        add(
            "dns_dnssec",
            "unknown",
            "DNSSEC status was not confirmed; the DNS lookup did not complete.",
            False,
            {"queries": 1},
        )
        return
    if data.get("Status") not in {None, 0}:
        add(
            "dns_dnssec",
            "unknown",
            "DNSSEC status was not confirmed; the DNS response was not successful.",
            False,
            {"queries": 1},
        )
        return
    ad_bit = data.get("AD")
    if ad_bit is True:
        add(
            "dns_dnssec",
            "info",
            "The validating resolver set the AD bit; this is not a full DNSSEC policy audit.",
            False,
            {"ad": True},
        )
    elif ad_bit is False:
        add(
            "dns_dnssec",
            "info",
            "DNSSEC was not confirmed (AD=false); the name may be unsigned.",
            False,
            {"ad": False},
        )
    else:
        add(
            "dns_dnssec",
            "unknown",
            "The DNS response did not contain a usable AD bit.",
            False,
            {"queries": 1},
        )


def _check_nginx(paths: Any, run_command: Callable[..., Any], add: Callable[..., None]) -> None:
    nginx_result = _invoke(
        run_command,
        ["/usr/sbin/nginx", "-t", "-c", str(paths.config)],
        8,
    )
    nginx_code = _returncode(nginx_result) if nginx_result is not None else None
    if nginx_result is None or nginx_code is None:
        add(
            "nginx_config",
            "unknown",
            "Nginx configuration check could not be completed.",
            True,
        )
    elif nginx_code == 0:
        add("nginx_config", "ok", "Nginx accepted the configured configuration.", True)
    else:
        add(
            "nginx_config",
            "error",
            "Nginx rejected the configured configuration.",
            True,
            {"exit_code": nginx_code},
        )


def _check_service_state(properties: dict[str, str], main_pid: int | None, add: Callable[..., None]) -> None:
    active_state = properties.get("ActiveState")
    if active_state is None:
        add("service_state", "unknown", "Backend service state was incomplete.", True)
    elif active_state != "active":
        add("service_state", "error", "Backend service is not active.", True)
    elif main_pid is None or main_pid <= 1:
        add("service_state", "error", "Backend service has no usable main process.", True)
    else:
        add("service_state", "ok", "Backend service is active with a main process.", True)


def _check_restart_policy(properties: dict[str, str], add: Callable[..., None]) -> None:
    restart = properties.get("Restart")
    if restart is None:
        add("service_restart_policy", "unknown", "Backend restart policy was missing.", False)
    elif restart in {"on-failure", "always"}:
        add("service_restart_policy", "ok", "Backend restart policy covers failures.", False)
    else:
        add(
            "service_restart_policy",
            "warning",
            "Backend restart policy does not cover all failure exits.",
            False,
            {"restart_on_failure": restart == "on-failure"},
        )


def _check_start_limit(properties: dict[str, str], add: Callable[..., None]) -> None:
    interval = _duration_usec(properties.get("StartLimitIntervalUSec"))
    burst = _int_value(properties.get("StartLimitBurst"))
    if interval is None or burst is None:
        add("service_start_limit", "unknown", "Backend start rate limit was not interpretable.", False)
    elif interval <= 0 or burst <= 0:
        add(
            "service_start_limit",
            "warning",
            "Backend start rate limiting appears to be disabled.",
            False,
            {"interval_seconds": 0, "burst": max(0, burst)},
        )
    else:
        add(
            "service_start_limit",
            "ok",
            "Backend start rate limiting is configured.",
            False,
            {"interval_seconds": interval / 1_000_000, "burst": burst},
        )


def _check_fd_limit(
    properties: dict[str, str],
    worker_connections: int | None,
    add: Callable[..., None],
) -> None:
    descriptor_limit = properties.get("LimitNOFILESoft")
    if descriptor_limit is None:
        add("service_fd_limit", "unknown", "Backend descriptor budget was missing.", True)
    elif descriptor_limit == "infinity":
        add(
            "service_fd_limit",
            "ok",
            "Backend descriptor budget is unlimited.",
            True,
            {"limit": "infinity"},
        )
    elif worker_connections is None or worker_connections < 1:
        add("service_fd_limit", "unknown", "Backend connection budget was not interpretable.", True)
    else:
        limit = _int_value(descriptor_limit)
        budget = max(4096, 4 * worker_connections)
        if limit is None:
            add("service_fd_limit", "unknown", "Backend descriptor budget was not interpretable.", True)
        elif limit < budget:
            add(
                "service_fd_limit",
                "error",
                "Backend descriptor budget is below the configured connection budget.",
                True,
                {"limit": limit, "budget": budget},
            )
        else:
            add(
                "service_fd_limit",
                "ok",
                "Backend descriptor budget covers the configured connection budget.",
                True,
                {"limit": limit, "budget": budget},
            )


def _check_service(
    run_command: Callable[..., Any],
    service: str,
    worker_connections: int | None,
    add: Callable[..., None],
) -> int | None:
    properties, outcome, _service_code = _show_properties(
        run_command,
        service,
        "ActiveState,MainPID,Restart,StartLimitIntervalUSec,StartLimitBurst,LimitNOFILESoft,UnitFileState",
    )
    if properties is None:
        if outcome == "exception":
            message = "Backend service state could not be read."
        elif outcome == "failed":
            message = "Backend service is unavailable to systemd."
        else:
            message = "Backend service state was incomplete."
        add("service_state", "unknown", message, True)
        add("service_restart_policy", "unknown", "Backend restart policy could not be read.", False)
        add("service_start_limit", "unknown", "Backend start rate limit could not be read.", False)
        add("service_fd_limit", "unknown", "Backend descriptor budget could not be read.", True)
        return None
    main_pid = _int_value(properties.get("MainPID"))
    _check_service_state(properties, main_pid, add)
    _check_restart_policy(properties, add)
    _check_start_limit(properties, add)
    _check_fd_limit(properties, worker_connections, add)
    return main_pid


def _check_renewal_timer(
    run_command: Callable[..., Any],
    renewal_timer: str,
    add: Callable[..., None],
) -> None:
    timer_properties, timer_outcome, _timer_code = _show_properties(
        run_command,
        renewal_timer,
        "ActiveState,UnitFileState",
    )
    if timer_properties is None:
        message = (
            "Renewal timer state could not be read."
            if timer_outcome == "exception"
            else "Renewal timer is unavailable or its state was incomplete."
        )
        add("renewal_timer", "unknown", message, True)
        return
    timer_active = timer_properties.get("ActiveState") == "active"
    timer_state = timer_properties.get("UnitFileState")
    if timer_active and timer_state in ENABLED_UNIT_STATES:
        add(
            "renewal_timer",
            "ok",
            "Renewal timer is active and enabled.",
            True,
            {"unit_enabled": True},
        )
    else:
        add(
            "renewal_timer",
            "error",
            "Renewal timer is not both active and enabled.",
            True,
            {"active": timer_active, "unit_enabled": timer_state in ENABLED_UNIT_STATES},
        )


def _check_listeners(
    run_command: Callable[..., Any],
    port: int,
    main_pid: int | None,
    add: Callable[..., None],
) -> None:
    ss_result = _invoke(run_command, ["ss", "-H", "-lntp"], 8)
    ss_code = _returncode(ss_result) if ss_result is not None else None
    ss_output = _property_output(ss_result) if ss_result is not None else None
    if ss_result is None or ss_code is None or ss_output is None:
        add("local_listeners", "unknown", "Local listener inspection could not be completed.", True)
        return
    if ss_code != 0:
        add(
            "local_listeners",
            "unknown",
            "Local listener inspection did not complete.",
            True,
            {"exit_code": ss_code},
        )
        return
    assessment = _listener_assessment(ss_output, port, main_pid)
    classification = [_is_loopback_host(host) for host in assessment["own"]]
    if any(value is False for value in classification):
        add(
            "local_listeners",
            "error",
            "Backend listens on a non-loopback address.",
            True,
            {"own_listeners": len(assessment["own"])},
        )
    elif any(value is None for value in classification):
        add("local_listeners", "unknown", "Backend listener address was not interpretable.", True)
    elif assessment["unknown"]:
        add(
            "local_listeners",
            "unknown",
            "Listener ownership could not be confirmed.",
            True,
            {"target_listeners": assessment["target_lines"]},
        )
    elif not assessment["own"]:
        add(
            "local_listeners",
            "unknown",
            "No listener could be attributed to the backend process.",
            True,
            {"target_listeners": assessment["target_lines"]},
        )
    else:
        add(
            "local_listeners",
            "ok",
            "Backend listeners on the configured port are loopback-only.",
            True,
            {"own_listeners": len(assessment["own"])},
        )

    foreign_public = [host for host in assessment["foreign"] if _is_loopback_host(host) is not True]
    if foreign_public:
        add(
            "port_conflict",
            "warning",
            "Another process listens publicly on the configured port.",
            False,
            {"public_foreign_listeners": len(foreign_public)},
        )


def _check_certificate_disk(
    paths: Any,
    domain: str,
    run_command: Callable[..., Any],
    warn_cert_days: int,
    now: datetime,
    add: Callable[..., None],
) -> str | None:
    certificate_path: Path | None = None
    disk_fingerprint: str | None = None
    try:
        candidate_path = paths.lineage(domain) / "fullchain.pem"
        certificate_path = candidate_path
        certificate_data = _read_limited(candidate_path, CERTIFICATE_READ_LIMIT)
    except FileNotFoundError:
        add("certificate_disk", "error", "Certificate file is unavailable.", True)
        certificate_data = None
    except (OSError, ValueError):
        add("certificate_disk", "error", "Certificate file could not be read safely.", True)
        certificate_data = None
    except Exception:
        add("certificate_disk", "unknown", "Certificate file inspection raised an internal error.", True)
        certificate_data = None

    if certificate_data is not None:
        pem = _first_pem_certificate(certificate_data)
        if pem is None:
            add("certificate_disk", "error", "Certificate file did not contain a PEM certificate.", True)
        else:
            try:
                disk_fingerprint = _certificate_fingerprint(pem)
            except (UnicodeError, ValueError):
                add("certificate_disk", "error", "Certificate file did not contain a valid certificate.", True)
            if disk_fingerprint is not None and certificate_path is not None:
                openssl_result = _invoke(
                    run_command,
                    ["openssl", "x509", "-in", str(certificate_path), "-noout", "-enddate"],
                    8,
                )
                openssl_code = _returncode(openssl_result) if openssl_result is not None else None
                if openssl_result is None or openssl_code is None:
                    add("certificate_disk", "unknown", "Certificate expiry could not be checked.", True)
                elif openssl_code != 0:
                    add("certificate_disk", "error", "Certificate expiry could not be parsed.", True)
                else:
                    output = _property_output(openssl_result) or ""
                    match = re.search(r"notAfter=(.+)", output)
                    not_after = _parse_expiry(match.group(1).strip()) if match else None
                    if not_after is None:
                        add("certificate_disk", "unknown", "Certificate expiry was not interpretable.", True)
                    else:
                        remaining_days = (not_after - now).total_seconds() / 86400
                        if remaining_days <= 0:
                            add(
                                "certificate_disk",
                                "error",
                                "Certificate on disk has expired.",
                                True,
                                {"expires_in_days": int(remaining_days)},
                            )
                        elif remaining_days < warn_cert_days:
                            add(
                                "certificate_disk",
                                "warning",
                                "Certificate on disk expires soon.",
                                True,
                                {"expires_in_days": int(remaining_days)},
                            )
                        else:
                            add(
                                "certificate_disk",
                                "ok",
                                "Certificate on disk is valid for the near term.",
                                True,
                                {"expires_in_days": int(remaining_days)},
                            )
    return disk_fingerprint


def _check_tls(
    domain: str,
    port: int,
    disk_fingerprint: str | None,
    warn_cert_days: int,
    now: datetime,
    add: Callable[..., None],
) -> None:
    h2_result: dict[str, Any] | None = None
    if not domain or port <= 0:
        add("backend_tls", "unknown", "Backend TLS probe requires a domain and port.", True)
    else:
        try:
            h2_result = _probe_h2(domain, port)
        except ssl.SSLCertVerificationError:
            add("backend_tls", "error", "Backend TLS certificate verification failed.", True)
        except (OSError, ssl.SSLError):
            add("backend_tls", "error", "Backend TLS probe did not complete a verified handshake.", True)
        except Exception:
            add("backend_tls", "unknown", "Backend TLS probe raised an internal error.", True)
        else:
            if not isinstance(h2_result, dict):
                add("backend_tls", "unknown", "Backend TLS probe returned an invalid result.", True)
                h2_result = None
            else:
                version = h2_result.get("version")
                alpn = h2_result.get("alpn")
                tls_expiry = _parse_expiry(h2_result.get("not_after"))
                details: dict[str, Any] = {"version": version, "alpn": alpn}
                if tls_expiry is not None:
                    details["expires_in_days"] = int((tls_expiry - now).total_seconds() / 86400)
                if version != "TLSv1.3" or alpn != "h2":
                    add("backend_tls", "error", "Backend did not negotiate TLS 1.3 with h2.", True, details)
                elif tls_expiry is not None and tls_expiry <= now:
                    add("backend_tls", "error", "Backend served an expired certificate.", True, details)
                else:
                    add("backend_tls", "ok", "Backend negotiated TLS 1.3 with h2.", True, details)

    served_expiry = _parse_expiry(h2_result.get("not_after")) if isinstance(h2_result, dict) else None
    if served_expiry is None:
        add(
            "certificate_served",
            "unknown",
            "The served certificate expiry could not be determined.",
            True,
        )
    else:
        served_remaining_days = (served_expiry - now).total_seconds() / 86400
        if served_remaining_days <= 0:
            add(
                "certificate_served",
                "error",
                "The served certificate has expired.",
                True,
                {"expires_in_days": int(served_remaining_days)},
            )
        elif served_remaining_days < warn_cert_days:
            add(
                "certificate_served",
                "warning",
                "The served certificate expires soon.",
                True,
                {"expires_in_days": int(served_remaining_days)},
            )
        else:
            add(
                "certificate_served",
                "ok",
                "The served certificate is valid for the near term.",
                True,
                {"expires_in_days": int(served_remaining_days)},
            )

    if h2_result is None or not isinstance(h2_result.get("fingerprint"), str):
        add(
            "certificate_match",
            "unknown",
            "The served leaf certificate could not be compared with the disk certificate.",
            True,
        )
    elif disk_fingerprint is None:
        add(
            "certificate_match",
            "unknown",
            "The disk leaf certificate could not be used for comparison.",
            True,
        )
    elif h2_result["fingerprint"].lower() == disk_fingerprint.lower():
        add("certificate_match", "ok", "The served leaf certificate matches the disk certificate.", True)
    else:
        add("certificate_match", "error", "The served leaf certificate differs from the disk certificate.", True)


def _check_http(domain: str, port: int, add: Callable[..., None]) -> None:
    if not domain or port <= 0:
        add("backend_http", "unknown", "Backend HTTP probe requires a domain and port.", True)
        return
    try:
        http_result = _probe_http11(domain, port)
    except ssl.SSLCertVerificationError:
        add("backend_http", "error", "Backend HTTP probe certificate verification failed.", True)
    except (OSError, ssl.SSLError):
        add("backend_http", "error", "Backend HTTP probe did not return a usable response.", True)
    except Exception:
        add("backend_http", "unknown", "Backend HTTP probe raised an internal error.", True)
    else:
        if not isinstance(http_result, dict):
            add("backend_http", "unknown", "Backend HTTP probe returned an invalid result.", True)
        elif http_result.get("alpn") != "http/1.1":
            add("backend_http", "error", "Backend HTTP probe negotiated an unexpected protocol.", True)
        elif http_result.get("status") != 200:
            add(
                "backend_http",
                "error",
                "Backend HEAD request did not return status 200.",
                True,
                {"status": http_result.get("status")},
            )
        else:
            add("backend_http", "ok", "Backend HEAD request returned status 200.", True)


def _check_access_log(paths: Any, add: Callable[..., None]) -> None:
    try:
        log_lines = _read_access_log(paths.logs / "access.log")
    except OSError:
        add("access_log", "unknown", "Access log could not be read safely.", False)
    except Exception:
        add("access_log", "unknown", "Access log inspection raised an internal error.", False)
    else:
        if not log_lines:
            add("access_log", "info", "No recent access log samples were available.", False)
            return
        counts = {"loopback": 0, "other": 0}
        for line in log_lines:
            fields = line.split(None, 1)
            if not fields:
                continue
            loopback = _source_is_loopback(fields[0])
            if loopback is None:
                continue
            counts["loopback" if loopback else "other"] += 1
        if counts["other"] == 0 and counts["loopback"] > 0:
            add(
                "access_log",
                "info",
                "Recent requests came only from loopback (expected with xver=0).",
                False,
                counts,
            )
        elif counts["other"] > 0:
            add(
                "access_log",
                "info",
                "Recent non-loopback sources may reflect public HTTP or HTTP-01 traffic; "
                "HTTPS visitor origin is not inferred.",
                False,
                counts,
            )
        else:
            add("access_log", "info", "No recent access log samples were parseable.", False)


def _check_dns_policy(domain: str, dns_checks: bool, add: Callable[..., None]) -> None:
    if dns_checks:
        _dns_checks(domain, add)
        return
    add(
        "dns_caa",
        "info",
        "CAA checks were not requested.",
        False,
        {"checked": False},
    )
    add(
        "dns_dnssec",
        "info",
        "DNSSEC checks were not requested.",
        False,
        {"checked": False},
    )


def build_report(
    paths: Any,
    state: dict[str, Any],
    *,
    run_command: Callable[..., Any],
    warn_cert_days: int = 20,
    dns_checks: bool = False,
    service: str = "toastweb-nginx.service",
    renewal_timer: str = "toastweb-renew.timer",
) -> dict[str, Any]:
    """Build a deterministic local-backend report. Public reachability is out of scope."""
    if isinstance(warn_cert_days, bool) or not isinstance(warn_cert_days, int) or not 1 <= warn_cert_days <= 90:
        raise ValueError("warn_cert_days must be an integer between 1 and 90")
    if not isinstance(dns_checks, bool):
        raise ValueError("dns_checks must be a boolean")
    if not isinstance(service, str) or not service:
        raise ValueError("service must be a non-empty unit name")
    if not isinstance(renewal_timer, str) or not renewal_timer:
        raise ValueError("renewal_timer must be a non-empty unit name")

    domain_value = state.get("domain") if isinstance(state, dict) else None
    domain = domain_value if isinstance(domain_value, str) else ""
    port_value = state.get("port", 0) if isinstance(state, dict) else 0
    try:
        port = int(port_value)
    except (TypeError, ValueError):
        port = 0
    worker_value = state.get("worker_connections") if isinstance(state, dict) else None
    if isinstance(worker_value, int) and not isinstance(worker_value, bool):
        worker_connections = worker_value
    else:
        worker_connections = None

    checked_at = _timestamp()
    now = _utc_now()
    checks: list[dict[str, Any]] = []

    def add(
        check_id: str,
        level: str,
        message: str,
        critical: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        checks.append(
            {
                "id": check_id,
                "level": level,
                "message": message,
                "critical": bool(critical),
                "details": details or {},
            }
        )

    _check_nginx(paths, run_command, add)
    main_pid = _check_service(run_command, service, worker_connections, add)
    _check_renewal_timer(run_command, renewal_timer, add)
    _check_listeners(run_command, port, main_pid, add)
    disk_fingerprint = _check_certificate_disk(paths, domain, run_command, warn_cert_days, now, add)
    _check_tls(domain, port, disk_fingerprint, warn_cert_days, now, add)
    _check_http(domain, port, add)
    _check_access_log(paths, add)
    _check_dns_policy(domain, dns_checks, add)

    critical_errors = any(c["critical"] and c["level"] == "error" for c in checks)
    critical_unknowns = any(c["critical"] and c["level"] == "unknown" for c in checks)
    warnings = any(c["level"] == "warning" for c in checks)
    if critical_errors:
        status = "error"
    elif critical_unknowns:
        status = "unknown"
    elif warnings:
        status = "warning"
    else:
        status = "ok"

    return {
        "schema_version": SCHEMA_VERSION,
        "domain": domain,
        "port": port,
        "checked_at": checked_at,
        "status": status,
        "checks": checks,
        "scope": SCOPE,
        "not_tested": list(NOT_TESTED),
    }


def report_exit_code(report: dict[str, Any]) -> int:
    """Map report status to a shell exit code; unknown is distinct from failure."""
    if not isinstance(report, dict):
        return 2
    status = report.get("status")
    if status == "error":
        return 1
    if status == "unknown":
        return 2
    if status in {"ok", "warning"}:
        return 0
    return 2


def format_report(report: dict[str, Any]) -> str:
    """Render a stable, human-readable summary containing only safe diagnostics."""
    if not isinstance(report, dict):
        return "ToastWeb diagnostics: unknown\n"
    lines = [
        "ToastWeb diagnostics: " + str(report.get("status", "unknown")),
        "Domain: " + str(report.get("domain", "")),
        "Port: " + str(report.get("port", "")),
        "Checked: " + str(report.get("checked_at", "")),
        "Scope: " + str(report.get("scope", "")),
        "",
    ]
    checks = report.get("checks")
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict):
                continue
            level = str(check.get("level", "unknown"))
            identifier = str(check.get("id", "unknown"))
            critical = " critical" if check.get("critical") else ""
            lines.append(f"[{level}] {identifier}{critical}: {check.get('message', '')}")
            details = check.get("details")
            if isinstance(details, dict) and details:
                try:
                    rendered = json.dumps(details, sort_keys=True, ensure_ascii=True)
                except (TypeError, ValueError):
                    rendered = "{}"
                lines.append("  details: " + rendered)
    not_tested = report.get("not_tested")
    if isinstance(not_tested, list):
        lines.extend(["", "Not tested: " + ", ".join(str(item) for item in not_tested)])
    lines.extend(["", "Exit code: " + str(report_exit_code(report))])
    return "\n".join(lines) + "\n"
