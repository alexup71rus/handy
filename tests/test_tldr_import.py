"""The tldr pack stays pinned, attributed and strictly non-executable."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.import_tldr import REVISION, FetchError, build, parse_page, safe_example, source_path, source_url, validation_variants


class TldrImportTests(unittest.TestCase):
    def test_validation_preserves_only_read_only_recipes(self):
        self.assertTrue(safe_example("ls {{path/to/directory}}"))
        self.assertTrue(safe_example("head -n {{10}} {{path/to/file}}"))
        self.assertTrue(safe_example("find {{path/to/directory}} -name '{{*.txt}}' -print | wc -l"))
        self.assertTrue(safe_example("ls {{[-a|-A]}}"))
        for command in ["rm -rf {{path}}", "kill {{123}}", "ls > {{output_file}}", "ls {{$(touch_hacked)}}", "ls; rm -rf x", "find . -exec rm x", "uniq {{input}} {{output}}", "sort {{[-n|--output]}} {{file}}", "echo `touch x`", "ls {{path1 path2}}"]:
            with self.subTest(command=command):
                self.assertFalse(safe_example(command))

    def test_original_placeholders_remain_reference_data(self):
        raw = b"# ls\n\n> List directory contents.\n\n- Show a selected directory:\n\n`ls {{path/to/directory}}`\n\n- Write an output file:\n\n`ls > {{path/to/output}}`\n"
        page = parse_page(raw, command="ls", platform="common", language="en", path="pages/common/ls.md")
        self.assertEqual(page["examples"], ["ls {{path/to/directory}}"])
        self.assertEqual(page["signatures"], page["examples"])
        self.assertEqual(validation_variants(page["examples"][0]), ["ls example"])
        self.assertNotIn("ls example", json.dumps(page))
        self.assertTrue(page["reference_only"])
        self.assertFalse(page["executable"])
        self.assertEqual(page["provenance"]["sha256"], hashlib.sha256(raw).hexdigest())

    def test_dangerous_pages_and_mutable_revisions_are_rejected(self):
        for command in ["rm", "kill", "sudo", "../ls"]:
            with self.assertRaises(ValueError):
                source_path("common", command, "en")
        for revision in ["main", "latest", "758e8f2", "../main"]:
            with self.assertRaises(ValueError):
                source_url("pages/common/ls.md", revision)
        self.assertIn("/" + REVISION + "/", source_url("pages/common/ls.md"))

    def test_offline_build_ru_preference_en_fallback_and_full_license(self):
        license_data = b"Copyright tldr contributors\nCreative Commons Attribution 4.0\nFull fixture terms preserved.\n"
        ru = "# ls\n\n> Показать содержимое папки.\n\n- Показать папку:\n\n`ls {{path}}`\n".encode()
        en = b"# sw_vers\n\n> Print macOS version information.\n\n- Show details:\n\n`sw_vers`\n"
        calls = []

        def fixture(url, *, deadline):
            calls.append(url)
            if url.endswith("LICENSE.md"):
                return license_data
            if url.endswith("pages.ru/common/ls.md"):
                return ru
            if url.endswith("pages/osx/sw_vers.md"):
                return en
            raise FetchError("404", "fixture missing")

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "tldr-reference.json"
            report = build(output, targets=[("common", "ls"), ("osx", "sw_vers")], fetcher=fixture)
            pack = json.loads(output.read_text())
            manifest = json.loads(output.with_name("tldr-manifest.json").read_text())
            self.assertEqual(report["pages"], 2)
            self.assertEqual(report["languages"], {"ru": 1, "en": 1})
            self.assertEqual(output.with_name("tldr-LICENSE.md").read_bytes(), license_data)
            self.assertEqual(manifest["pack"]["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(manifest["license"]["sha256"], hashlib.sha256(license_data).hexdigest())
            self.assertTrue(all("/" + REVISION + "/" in url for url in calls))
            self.assertFalse(any(url.endswith("pages/common/ls.md") for url in calls))
            self.assertEqual(pack["entries"][1]["examples"], [])  # not in reference recipe allowlist


if __name__ == "__main__":
    unittest.main()
