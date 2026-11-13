# Changelog

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
