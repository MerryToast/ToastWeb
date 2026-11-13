# Changelog

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
