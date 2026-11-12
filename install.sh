#!/usr/bin/env bash
# ToastWeb entry point: no curl|bash, firewall changes, or proxy configuration.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'ToastWeb requires Python 3.10+. Install python3 using your system package manager first.' >&2
    exit 1
fi
exec python3 "$ROOT/toastweb.py" install "$@"
