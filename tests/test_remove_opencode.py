import copy
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import remove_opencode


class RemoveOpenCodeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'handy'
        self.project.mkdir()
        self.config = self.root / 'config/opencode.json'
        self.config.parent.mkdir()
        self.enterContext(patch.object(remove_opencode, 'PROJECT', self.project))
        self.provider = {
            'name': 'Handy', 'package': '@opencode/ai/providers/openai-compatible',
            'settings': {'baseURL': 'http://127.0.0.1:18746/v1', 'apiKey': 'private-test-value'},
            'models': {'memory-engine': {'name': 'Handy · local'}},
        }

    def write(self, settings):
        self.config.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n')
        return self.config.read_bytes()

    def test_removes_actual_mixed_schema_preserving_veai_mcp_and_other_models(self):
        settings = {
            '$schema': 'https://opencode.ai/config.json', '$comment': 'User configuration',
            'instructions': ['AGENTS.md'], 'mcp': {'chrome': {'command': ['node', 'bridge.js']}},
            'provider': {'veai': {'options': {'apiKey': 'unrelated-private-value'}}},
            'providers': {'funny': self.provider, 'ollama': {'name': 'Ollama'}},
            'agents': {'funny': {'model': remove_opencode.MODEL}, 'mimo': {'model': 'ollama/mimo'}},
            'plugins': ['@opencode/plugin-browser', str(self.project / 'integration/opencode-plugin')],
            'commands': {'check': {'agent': 'mimo', 'template': 'Check project'}},
        }
        original = self.write(settings)
        result = remove_opencode.remove(self.config)
        expected = copy.deepcopy(settings)
        del expected['providers']['funny']
        del expected['agents']['funny']
        expected['plugins'] = ['@opencode/plugin-browser']
        self.assertEqual(json.loads(self.config.read_text()), expected)
        backup = Path(result['backup'])
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o600)
        self.assertEqual(set(result['removed']), {'providers.funny', 'agents.funny', 'plugins[1]'})
        self.assertTrue(result['changed'])
        self.assertFalse(remove_opencode.remove(self.config)['changed'])
        self.assertEqual(list(backup.parent.iterdir()), [backup])

    def test_dry_run_is_read_only_and_does_not_create_backups(self):
        original = self.write({'providers': {'funny': self.provider}})
        result = remove_opencode.remove(self.config, dry_run=True)
        self.assertEqual(result['removed'], ['providers.funny'])
        self.assertTrue(result['dry_run'])
        self.assertIsNone(result['backup'])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.project / '.runtime').exists())

    def test_foreign_provider_causes_no_partial_removal_or_backup(self):
        for provider in ({'name': 'Unrelated'},
                         {**self.provider, 'name': 'Someone else'},
                         {**self.provider, 'settings': {'baseURL': 'https://other.example/v1'}},
                         {**self.provider, 'models': {'memory-engine': {}, 'other': {}}},
                         {**self.provider, 'package': '@some/other-package'},
                         {**self.provider, 'settings': {'baseURL': 'http://user:pass@127.0.0.1:18746/v1'}}):
            with self.subTest(provider=provider):
                original = self.write({'providers': {'funny': provider},
                                       'agents': {'funny': {'model': remove_opencode.MODEL}},
                                       'plugins': [str(self.project / 'integration/opencode-plugin')]})
                with self.assertRaisesRegex(ValueError, 'not a verified Handy provider'):
                    remove_opencode.remove(self.config)
                self.assertEqual(self.config.read_bytes(), original)
                self.assertFalse((self.project / '.runtime').exists())

    def test_singular_schema_and_only_owned_commands_defaults_and_paths(self):
        provider = {**self.provider, 'npm': self.provider['package'], 'options': self.provider['settings']}
        del provider['package'], provider['settings']
        settings = {
            'provider': {'funny': provider},
            'agent': {'handy': {'model': remove_opencode.MODEL}, 'funny': {'model': 'other/something'}},
            'command': {'handy': {'agent': 'handy'}, 'funny': {'template': 'Tell a joke'},
                        'other': {'agent': 'funny'}, 'direct': {'model': remove_opencode.MODEL}},
            'plugin': [(self.project / 'integration/opencode-plugin/index.js').as_uri(),
                       str(self.root / 'funny/integration/opencode-plugin'),
                       str(self.root / 'unrelated/integration/opencode-plugin'), '@someone/handy'],
            'model': remove_opencode.MODEL, 'small_model': 'other/small', 'default_agent': 'handy',
        }
        self.write(settings)
        remove_opencode.remove(self.config)
        updated = json.loads(self.config.read_text())
        self.assertEqual(updated['provider'], {})
        self.assertEqual(updated['agent'], {'funny': {'model': 'other/something'}})
        self.assertEqual(updated['command'], {'funny': {'template': 'Tell a joke'}, 'other': {'agent': 'funny'}})
        self.assertEqual(updated['plugin'], settings['plugin'][2:])
        self.assertNotIn('model', updated)
        self.assertNotIn('default_agent', updated)
        self.assertEqual(updated['small_model'], 'other/small')

    def test_invalid_shape_and_duplicate_keys_are_not_rewritten(self):
        for settings in (None, [], {'providers': []}, {'plugins': {}}, {'agents': 'funny'}):
            with self.subTest(settings=settings):
                original = self.write(settings)
                with self.assertRaises(ValueError):
                    remove_opencode.remove(self.config)
                self.assertEqual(self.config.read_bytes(), original)
        self.config.write_text('{"providers": {}, "providers": {"veai": {}}}')
        original = self.config.read_bytes()
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            remove_opencode.remove(self.config)
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse((self.project / '.runtime').exists())

    def test_config_change_during_write_is_not_overwritten(self):
        self.write({'providers': {'funny': self.provider}})
        later = {'provider': {'veai': {'name': 'New value'}}}
        original_replace = os.replace
        original_dump = json.dump

        def write_and_change(value, stream, **options):
            original_dump(value, stream, **options)
            self.write(later)

        with patch.object(remove_opencode.json, 'dump', side_effect=write_and_change), \
                patch.object(remove_opencode.os, 'replace', wraps=original_replace) as replace:
            with self.assertRaises(RuntimeError):
                remove_opencode.remove(self.config)
        replace.assert_not_called()
        self.assertEqual(json.loads(self.config.read_text()), later)

    def test_atomic_replace_failure_preserves_config_and_private_backup(self):
        original = self.write({'providers': {'funny': self.provider}})
        with patch.object(remove_opencode.os, 'replace', side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError):
                remove_opencode.remove(self.config)
        self.assertEqual(self.config.read_bytes(), original)
        backup, = (self.project / '.runtime/config-backups').iterdir()
        self.assertEqual(backup.read_bytes(), original)
        self.assertFalse(list(self.config.parent.glob('.opencode-remove-*')))

    def test_absent_config_is_a_noop_and_global_path_respects_environment(self):
        self.assertFalse(remove_opencode.remove(self.config)['changed'])
        self.assertFalse((self.project / '.runtime').exists())
        with patch.dict(os.environ, {'OPENCODE_CONFIG_DIR': str(self.root / 'custom')}):
            self.assertEqual(remove_opencode.config_path(), self.root / 'custom/opencode.json')
        with patch.dict(os.environ, {'OPENCODE_CONFIG_DIR': '', 'XDG_CONFIG_HOME': str(self.root / 'xdg')}):
            self.assertEqual(remove_opencode.config_path(), self.root / 'xdg/opencode/opencode.json')
