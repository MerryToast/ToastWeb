"""Template selection, offline assets, truthful metadata and legacy compatibility."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlsplit

import toastweb as tw


class PageAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.duplicates: set[str] = set()
        self.links: list[str] = []
        self.tags: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        for name, value in attrs:
            if value is None:
                continue
            if name == "id":
                if value in self.ids:
                    self.duplicates.add(value)
                self.ids.add(value)
            if name in {"href", "src"}:
                self.links.append(value)


class ThemeSelectionTests(unittest.TestCase):
    def test_subdomain_matching(self) -> None:
        for theme in ("docs", "status", "api"):
            with self.subTest(theme=theme):
                self.assertEqual(tw.choose_theme(theme + ".example.org"), theme)
                self.assertEqual(tw.choose_theme(theme.upper() + ".example.org"), theme)
        self.assertEqual(tw.choose_theme("www.example.org"), "docs")
        self.assertEqual(tw.choose_theme("example.org"), "docs")

    def test_manual_theme_wins(self) -> None:
        self.assertEqual(tw.choose_theme("api.example.org", "status"), "status")
        for name in ("../api", "missing", "/tmp/site"):
            with self.subTest(name=name), self.assertRaises(tw.ToastError):
                tw.choose_theme("api.example.org", name)

    def test_existing_and_legacy_themes_are_preserved(self) -> None:
        self.assertEqual(tw.choose_theme("api.example.org", state={"content_type": "builtin"}), "notes")
        self.assertEqual(tw.choose_theme("api.example.org", state={"content_type": "builtin", "theme": "status"}), "status")
        self.assertEqual(tw.state_theme({"content_type": "custom"}), None)
        for value in ("unknown", ["api"]):
            with self.subTest(value=value), self.assertRaises(tw.ToastError):
                tw.state_theme({"content_type": "builtin", "theme": value})

    def test_content_selection_guards(self) -> None:
        with self.assertRaises(tw.ToastError):
            tw.select_content("api.example.org", "api", "/tmp/public")
        with self.assertRaises(tw.ToastError):
            tw.select_content("api.example.org", "auto", None, {"content_type": "custom"})
        source, theme = tw.select_content("api.example.org", "api", None, {"content_type": "custom"})
        self.assertTrue((source / "index.html").is_file())
        self.assertEqual(theme, "api")
        _, theme = tw.select_content("api.example.org", "auto", "/tmp/public")
        self.assertIsNone(theme)

    def test_plan_theme_and_explicit_override(self) -> None:
        for domain, options, expected in (("docs.example.org", [], "docs"),
                                          ("status.example.org", [], "status"),
                                          ("api.example.org", [], "api"),
                                          ("api.example.org", ["--theme", "docs"], "docs")):
            with self.subTest(domain=domain, options=options), mock.patch.object(tw, "run") as runner, contextlib.redirect_stdout(io.StringIO()) as output:
                result = tw.main(["plan", "--domain", domain, "--email", "ops@example.org", *options])
                self.assertEqual(result, 0)
                plan = json.loads(output.getvalue())
                self.assertEqual(plan["theme"], expected)
                self.assertEqual(plan["title"], tw.THEMES[expected][0])
                self.assertFalse(plan["executed"])
                runner.assert_not_called()

    def test_metadata_defaults_and_preservation(self) -> None:
        for theme in ("docs", "status", "api"):
            self.assertEqual(tw.site_metadata(None, None, theme), tw.THEMES[theme])
        self.assertEqual(tw.site_metadata(None, None, "api", {"title": "My brand", "description": "My description"}), ("My brand", "My description"))
        self.assertEqual(tw.site_metadata("New name", None, "docs")[0], "New name")

    def test_legacy_state_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = tw.Paths(Path(directory))
            state = {"managed_by": tw.OWNER, "domain": "notes.example.org", "port": 8443,
                     "content_type": "builtin", "active_release": "v1", "previous_release": "v0",
                     "previous_content_type": "custom"}
            tw.write_atomic(paths.state, json.dumps(state))
            loaded = tw.load_state(paths) or self.fail("Missing state")
            self.assertEqual(loaded["theme"], "notes")
            self.assertIsNone(loaded["previous_theme"])
            self.assertNotIn('"theme"', paths.state.read_text())  # Reading is non-mutating.


class ThemeAssetTests(unittest.TestCase):
    def test_all_themes_are_offline_and_links_exist(self) -> None:
        for theme in ("docs", "status", "api"):
            with self.subTest(theme=theme):
                source, _ = tw.select_content(theme + ".example.org", theme, None)
                parser = PageAudit()
                parser.feed((source / "index.html").read_text(encoding="utf-8"))
                self.assertFalse(parser.duplicates)
                self.assertFalse(parser.tags & {"form", "input", "script", "iframe"})
                for url in parser.links:
                    parsed = urlsplit(url)
                    self.assertFalse(parsed.scheme or parsed.netloc, url)
                    target = source / (parsed.path.lstrip("/") or "index.html")
                    if target.is_dir():
                        target /= "index.html"
                    self.assertTrue(target.is_file(), url)
                    if parsed.fragment:
                        target_page = PageAudit()
                        target_page.feed(target.read_text(encoding="utf-8"))
                        self.assertIn(unquote(parsed.fragment), target_page.ids, url)
                stylesheet = (source / "assets/theme.css").read_text(encoding="utf-8")
                self.assertNotIn("@import", stylesheet)
                self.assertNotIn("url(", stylesheet)
                self.assertLess(sum(p.stat().st_size for p in source.rglob("*") if p.is_file()), 50 * 1024)

    def test_render_substitutions_and_escape(self) -> None:
        for theme in ("docs", "status", "api"):
            with self.subTest(theme=theme), tempfile.TemporaryDirectory() as directory:
                paths = tw.Paths(Path(directory))
                source, _ = tw.select_content(theme + ".example.org", theme, None)
                release = tw.create_release(paths, source, '<script>Title</script>', "Description & detail", theme + ".example.org", builtin=True)
                page = (paths.releases / release / "index.html").read_text(encoding="utf-8")
                self.assertIn('data-theme="' + theme + '"', page)
                self.assertIn("&lt;script&gt;Title&lt;/script&gt;", page)
                self.assertIn("Description &amp; detail", page)
                self.assertNotIn("{{", page)
                self.assertIn("Page built ", page)
                parser = PageAudit()
                parser.feed(page)
                self.assertNotIn("script", parser.tags)

    def test_api_metadata_is_real_static_information(self) -> None:
        metadata = json.loads((tw.BUNDLE / "themes/api/meta.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata, {"schema_version": "1", "service": "static-site", "authentication": "not-required", "live_monitoring": False})
        page = (tw.BUNDLE / "themes/api/index.html").read_text(encoding="utf-8")
        self.assertIn('href="/meta.json"', page)
        self.assertIn('"authentication"', page)
        self.assertNotIn("401 Unauthorized", page)

    def test_status_does_not_claim_live_monitoring(self) -> None:
        page = (tw.BUNDLE / "themes/status/index.html").read_text(encoding="utf-8")
        self.assertIn("未接入实时监测", page)
        self.assertIn("当前没有发布具体维护公告", page)
        self.assertNotIn("All systems operational", page)

    def test_version_files_match(self) -> None:
        self.assertEqual((tw.BUNDLE / "VERSION").read_text().strip(), tw.VERSION)
        self.assertIn('version = "' + tw.VERSION + '"', (tw.BUNDLE / "pyproject.toml").read_text())


if __name__ == "__main__":
    unittest.main()
