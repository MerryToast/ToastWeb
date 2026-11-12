# Security

ToastWeb is a root-level deployment tool. Review code and pin a version before running it.

- Use a domain you control and expose only the documented listeners.
- Never put credentials, private keys, production state or unredacted logs in this repository.
- The installer does not manage DNS, firewall, SSH or proxy configuration.
- Existing port-80 services and ACME automation require manual review; do not bypass checks blindly.
- Renewal depends on DNS, port 80, CA policy and availability. Monitor the renewal service journal.
- Backup recovery covers project-owned files and service state, not APT or CA side effects.
- A normal website does not guarantee that any proxy is undetectable or continuously available.

For a potential vulnerability, use the repository's private security reporting feature if available. Otherwise contact the repository owner without including credentials or exploit details in a public issue. No external contact address is currently designated.
