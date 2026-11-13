#!/usr/bin/env python3
"""Developer-only local previews with a fresh headless Chromium/Edge profile.

No VPS, DNS, real certificates, browser login profile, or external assets are used.
Requires Node 22+ for the browser CDP helper. Run as a normal desktop user;
this is not part of the VPS installer.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import toastweb as tw  # noqa: E402


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


def find_browser() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome", "msedge"):
        if executable := shutil.which(name):
            return executable
    for name in (
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
    ):
        if Path(name).is_file():
            return name
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default=find_browser())
    parser.add_argument("--output", type=Path, default=ROOT / ".github/previews")
    args = parser.parse_args()
    if not args.browser:
        parser.error("No Chromium-compatible browser found; pass --browser explicitly.")
    node = shutil.which("node")
    if not node:
        parser.error("Node 22+ is required for accurate viewport screenshots (developer tool only).")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="toastweb-theme-previews-", ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        for theme in ("docs", "status", "api"):
            paths = tw.Paths(root / theme)
            source, _ = tw.select_content(theme + ".example.org", theme, None)
            title, description = tw.site_metadata(None, None, theme)
            release = tw.create_release(paths, source, title, description, theme + ".example.org", builtin=True)
            handler = functools.partial(QuietHandler, directory=str(paths.releases / release))
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                command = [node, str(ROOT / "tools/capture_preview.mjs"), args.browser,
                           str(root / f"browser-{theme}"), f"http://127.0.0.1:{server.server_port}/", str(output), theme]
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, check=False)
                if result.returncode:
                    raise RuntimeError(f"Headless browser failed for {theme}: {result.stderr[-2000:]}")
                print(result.stdout.strip())
                for suffix in ("", "-mobile"):
                    screenshot = output / f"{theme}{suffix}.png"
                    if not screenshot.is_file() or screenshot.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
                        raise RuntimeError("Browser did not produce a PNG screenshot")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
