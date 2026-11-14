# Changelog

## 0.3.2 (unreleased)

- Extend read-only doctor with text/JSON output and distinct error/unknown exits, effective service policies, owned loopback listener inspection, bounded log summaries, certificate expiry and served/disk leaf matching, TLS/h2 plus separate HTTP/1.1 HEAD probes.
- Add explicitly requested CAA inheritance and DNSSEC AD-evidence checks through a fixed HTTPS resolver; no redirects, DNS mutation, API tokens or mandatory DNS policy.
- Add a default-off monitor enable/disable/status/check interface, independent oneshot/timer, configurable sampling/thresholds, certificate warnings, deduplicated failure/recovery events and throttled delivery retries.
- Default notifications to journal; allow only explicitly configured private root-owned executables with trusted ancestors, safe stdin JSON, no shell, bounded execution and discarded output. Do not configure external credentials or channels automatically.
- Keep monitoring separate from website lifecycle: probes never restart services, monitor configuration/recovery does not reload the website, and private runtime state is not overwritten by content rollback.
- Add mocked diagnostics/state-machine/CLI regressions and live Nginx diagnostic probes; isolated Ubuntu root/non-root CI passes, while real external notification delivery remains separate acceptance work.

- Generate bounded on-failure Nginx restarts, with start limits in the systemd Unit section and a bounded startup timeout; preserve administrator drop-ins.
- Add opt-in --package-upgrade-restart / --no-package-upgrade-restart (default off; preserved on reinstall) and a scoped package-refresh command.
- Schedule package refresh asynchronously from an owned APT hook; wait for package-manager locks and serialize with ToastWeb operations. Install needrestart only when opted in and invoke it strictly in list mode.
- Restart only an already active, unmasked ToastWeb reported stale by needrestart, after configuration validation and policy-rc.d checks; use try-restart to avoid starting a concurrently stopped service.
- Verify a new master, trusted loopback TLS 1.3 / HTTPS HEAD, and a fresh list-mode report after restart; report failures without rolling back packages or touching other services.
- Track the package hook and oneshot service in ownership checks and transaction recovery; explicit disable removes only those owned integration files.
- Document REALITY handshake dependencies, short restart windows, start-limit recovery, and reload versus binary/library updates.
- Validate crash recovery, start limiting and atomic binary replacement in disposable systemd CI fixtures; avoid assuming a single systemd Result string, bound failure diagnostics, and keep executed fixture binaries outside potentially noexec /run.
- Code publication does not deploy to a VPS or change production DNS, firewall, REALITY or subscriptions; release tags and deployment remain separate steps.

## 0.3.1

- Let systemd create RuntimeDirectory before the first manual Nginx configuration check; ExecStartPre still validates before starting the daemon.
- Add a transaction regression for the first-install PID-directory lifecycle, which mocked systemd and redirected integration-test PID paths previously missed.

## 0.3.0

- Use relative directory redirects, keeping internal HTTPS and SSH-forwarded ports out of Location headers.
- Reject HTTP requests reaching the TLS default server after a valid SNI but unknown Host/HTTP2 authority.
- Exercise live Nginx as both root and non-root, verifying worker identity without loosening private key permissions.
- Remove deployer branding/admin commands from starter pages; explicitly warn that shared template fingerprints remain.
- Relax only connection establishment failures for explicit non-local NAT IPs; local probes and HTTP/token errors remain fatal.
- Add an opt-in HTTP redirect command with explicit public-cutover confirmation, fixed-domain redirects and rollback.
- Track private release/backup ownership, preserve rollback and backup references, and safely prune registered history only after success.
- Add prune preview/--apply and bounded retention options; preserve unregistered legacy history and reject nested symlinks.
- Keep the default connection count at 512; add configurable budgets with matching Nginx/systemd FD limits and doctor checks.
- No VPS deployment, DNS, firewall, REALITY, subscription or production certificate changes.

## 0.2.0

- Add original docs, status, and api static templates, with all assets served locally.
- Infer the new-site theme from docs./status./api.; support explicit --theme selection.
- Preserve existing and legacy notes themes during reinstalls and routine updates.
- Track theme metadata across content switches, rollback, and failure recovery.
- Serve a real static /meta.json in the API theme; no pretend authentication or live metrics.
- Retain the v0.1 notes template for compatibility and custom static site support.

## 0.1.0

- Add an original static Toast theme with home, notes and about pages.
- Add isolated Nginx deployment for a loopback HTTPS backend.
- Add HTTP-01 webroot certificate issuance and a per-site renewal timer.
- Add ownership/conflict checks, explicit consent, atomic content releases and failure recovery.
- Add plan/status/doctor/update/rollback/renew-test commands.
- Add unit tests and opt-in Linux Nginx integration tests.
- No automated DNS, REALITY, subscription, firewall or kernel changes.

Initial version: real VPS/CA/REALITY pilot acceptance remains a separate step.
