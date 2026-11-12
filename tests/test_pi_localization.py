"""Localized UI is reproducible and cannot rewrite model-facing or user text."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from folder_memory import pi_localization as locale


@unittest.skipUnless(shutil.which('node'), 'Node.js is required by pi')
class LocalizationTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.catalogues=self.root/'catalogues';self.catalogues.mkdir()
        self.enterContext(patch.object(locale,'CATALOGUES',self.catalogues))
        self.pi=self.root/'pi';self.pi.mkdir()
        for name in ('pi-coding-agent','pi-tui'):
            package=self.pi/'node_modules/@earendil-works'/name
            (package/'dist').mkdir(parents=True)
            (package/'package.json').write_text(json.dumps({'name':name,'version':'1.5.2','type':'module'}))
            (package/'dist/cli.js').write_text('#!/usr/bin/env node\nconst label="Cancel"; const prompt="Cancel";\nconsole.log(JSON.stringify({label,prompt}));\n')
        self.source=self.pi/'node_modules/@earendil-works/pi-coding-agent/dist/cli.js'
        self.catalogue=self.catalogues/'ui.ru.json'
        self.write_catalogue()

    def write_catalogue(self,old='label="Cancel"',new='label="Отмена"'):
        self.catalogue.write_text(json.dumps([{'file':'cli.js','old':old,'new':new}]))

    def test_private_copy_changes_only_declared_ui_and_preserves_upstream(self):
        original=self.source.read_bytes()
        native=self.pi/'node_modules/@earendil-works/pi-tui/native/darwin/prebuilds/darwin-arm64/platform.node'
        native.parent.mkdir(parents=True);native.write_bytes(b'upstream native helper')
        cli=locale.localized_cli(self.pi,'1.5.2')
        result=subprocess.run(['node',str(cli)],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout),{'label':'Отмена','prompt':'Cancel'})
        self.assertEqual(self.source.read_bytes(),original)
        copied_native=cli.parent.parent/'node_modules/@earendil-works/pi-tui'/native.relative_to(native.parents[4])
        self.assertEqual(copied_native.read_bytes(),native.read_bytes())
        before=cli.stat().st_mtime_ns
        self.assertEqual(locale.localized_cli(self.pi,'1.5.2').stat().st_mtime_ns,before)

    def test_catalogue_update_rebuilds_but_mismatch_preserves_working_copy(self):
        cli=locale.localized_cli(self.pi,'1.5.2')
        self.write_catalogue(new='label="Назад"')
        locale.localized_cli(self.pi,'1.5.2')
        self.assertIn('Назад',cli.read_text())
        self.write_catalogue(old='missing source')
        with self.assertRaisesRegex(ValueError,'не соответствует'):
            locale.localized_cli(self.pi,'1.5.2')
        self.assertIn('Назад',cli.read_text())

    def test_invalid_javascript_never_replaces_a_working_runtime(self):
        cli=locale.localized_cli(self.pi,'1.5.2')
        self.write_catalogue(new='label="unterminated')
        with self.assertRaisesRegex(ValueError,'Ошибка в переводе'):
            locale.localized_cli(self.pi,'1.5.2')
        self.assertIn('Отмена',cli.read_text())

    def test_unknown_versions_and_paths_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError,'установлен 1.5.2'):
            locale.localized_cli(self.pi,'2.0.0')
        self.catalogue.write_text(json.dumps([{'file':'../../private.js','old':'x','new':'y'}]))
        with self.assertRaisesRegex(ValueError,'Некорректный путь'):
            locale.localized_cli(self.pi,'1.5.2')

    def test_missing_cached_entrypoint_or_broken_marker_is_rebuilt(self):
        cli=locale.localized_cli(self.pi,'1.5.2')
        cli.unlink()
        self.assertTrue(locale.localized_cli(self.pi,'1.5.2').is_file())
        marker=cli.parent.parent/'.handy-localization.json'
        marker.write_text('{interrupted cache metadata')
        self.assertEqual(locale.localized_cli(self.pi,'1.5.2'),cli)
        self.assertIn('Отмена',cli.read_text())

    def test_mismatched_tui_version_is_not_loaded_with_an_old_translation(self):
        locale.localized_cli(self.pi,'1.5.2')
        package=self.pi/'node_modules/@earendil-works/pi-tui/package.json'
        metadata=json.loads(package.read_text());metadata['version']='1.6.0';package.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError,'установлен 1.6.0'):
            locale.localized_cli(self.pi,'1.5.2')


if __name__=='__main__':unittest.main()
