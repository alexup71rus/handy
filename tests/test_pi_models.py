"""The provider-first picker never downloads silently or loses newer settings."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from folder_memory import pi_models
from folder_memory.model_registry import ModelRegistry


class PiModelsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.enterContext(patch.dict(os.environ, {'HANDY_CONFIG_HOME': str(self.root / 'config')}))
        self.enterContext(patch('folder_memory.model_registry.urlopen', side_effect=AssertionError('Unexpected network')))
        self.registry = ModelRegistry(self.state)
        self.registry.save_provider('api', {'name': 'API provider', 'base_url': 'http://127.0.0.1:12345/v1'})
        for name in ('first', 'second', 'third'):
            self.registry.add(name, {'name': name.capitalize(), 'kind': 'chat', 'backend': 'openai',
                                     'provider_id': 'api', 'model_id': name + '-model', 'context': 4096})
        self.registry.select('first')
        self.activate = self.enterContext(patch.object(pi_models, 'activate_models'))
        self.configure = self.enterContext(patch('folder_memory.pi_runtime.configure'))

    def test_inventory_is_grouped_system_first_and_offers_missing_catalogue(self):
        self.registry.remove('spark')
        before = self.registry.path.read_text()
        value = pi_models.inventory(self.state)
        self.assertEqual(value['selected'], 'first')
        self.assertEqual(value['groups'][0]['name'], 'Системные модели Handy')
        models = value['groups'][0]['models']
        spark = next(model for model in models if model['id'] == 'spark')
        self.assertFalse(spark['ready'])
        self.assertTrue(spark['downloadable'])
        self.assertGreater(spark['download_bytes'], 1_000_000_000)
        self.assertEqual((spark['pi_provider'], spark['pi_model']), ('handy-system', 'spark'))
        remote = next(group for group in value['groups'] if group['id'] == 'handy-api')
        self.assertEqual(len(remote['models']), 3)
        self.assertEqual(remote['models'][0]['pi_provider'], 'handy-api')
        self.assertNotIn('qwen3-embedding', json.dumps(value))
        self.assertEqual(self.registry.path.read_text(), before)

    def test_inventory_respects_hidden_models_and_provider_masks(self):
        settings = self.registry.load()
        settings['profiles']['second']['visible'] = False
        settings['standard_provider_visible'] = False
        self.registry.replace_settings(settings, expected_revision=self.registry.revision())
        groups = pi_models.inventory(self.state)['groups']
        self.assertEqual(len(groups), 1)
        self.assertEqual({model['id'] for model in groups[0]['models']}, {'first', 'third'})
        provider = self.registry.providers()['api']
        self.registry.save_provider('api', {**provider, 'visible': False}, replace=True)
        self.assertEqual(pi_models.inventory(self.state)['groups'], [])

    def test_select_returns_canonical_provider_and_real_model_id(self):
        result = pi_models.select_model(self.state, 'second')
        self.assertEqual(self.registry.load()['selected_chat'], 'second')
        self.assertEqual((result['pi_provider'], result['pi_model']), ('handy-api', 'second-model'))
        self.assertEqual(self.activate.call_args.args[1]['model_id'], 'second-model')
        self.configure.assert_called_once_with(self.state)
        revision = self.registry.revision()
        pi_models.select_model(self.state, 'second')
        self.assertEqual(self.registry.revision(), revision, 'Repeated selection must not rewrite preference metadata')

    def test_missing_weights_require_consent_without_mutating_registry(self):
        self.registry.remove('spark')
        before = self.registry.path.read_text()
        with patch.object(ModelRegistry, 'download') as download:
            with self.assertRaisesRegex(ValueError, 'подтверди'):
                pi_models.select_model(self.state, 'spark')
            download.assert_not_called()
        self.assertEqual(self.registry.path.read_text(), before)
        self.activate.assert_not_called()

    def test_explicit_download_is_verified_before_selecting(self):
        binary = self.root / 'llama-server'; binary.write_text('#!/bin/sh\nexit 0\n'); binary.chmod(0o700)
        weights = self.root / 'fixture.gguf'
        self.registry.add('local', {'name':'Local', 'kind':'chat', 'backend':'local', 'binary':str(binary),
                                    'model':str(weights), 'download': {'repo':'Official/Fixture', 'revision':'a'*40,
                                      'files':[{'field':'model','filename':'fixture.gguf','bytes':7,'sha256':'a'*64}]}})
        def downloaded(identifier):
            self.assertEqual(self.registry.load()['selected_chat'], 'first')
            weights.write_bytes(b'weights')
            return self.registry.get(identifier)
        with patch.object(ModelRegistry, 'download', side_effect=downloaded) as download:
            result = pi_models.select_model(self.state, 'local', download=True)
        download.assert_called_once_with('local')
        self.assertEqual(self.registry.load()['selected_chat'], 'local')
        self.assertEqual((result['pi_provider'], result['pi_model']), ('handy-system', 'local'))

    def test_activation_failure_restores_selection(self):
        previous = self.registry.load()
        self.activate.side_effect = ValueError('Cannot start worker')
        with self.assertRaisesRegex(ValueError, 'Cannot start worker'):
            pi_models.select_model(self.state, 'second')
        self.assertEqual(self.registry.load(), previous)
        self.configure.assert_not_called()

    def test_activation_failure_preserves_newer_selection(self):
        def change_then_fail(*args, **kwargs):
            self.registry.select('third')
            raise ValueError('Cannot start worker')
        self.activate.side_effect = change_then_fail
        with self.assertRaisesRegex(ValueError, 'Cannot start worker'):
            pi_models.select_model(self.state, 'second')
        self.assertEqual(self.registry.load()['selected_chat'], 'third')

    def test_failed_export_restores_activated_service_and_selection(self):
        self.configure.side_effect = [ValueError('Cannot export'), None]
        with self.assertRaisesRegex(ValueError, 'Cannot export'):
            pi_models.select_model(self.state, 'second')
        self.assertEqual(self.registry.load()['selected_chat'], 'first')
        self.assertEqual(self.activate.call_args.args[1]['model_id'], 'first-model')

    def test_unknown_or_unready_api_never_downloads(self):
        self.registry.add('locked', {'name':'Locked','kind':'chat','backend':'openai','base_url':'http://127.0.0.1:12345/v1',
                                     'model_id':'locked-model','api_key_env':'HANDY_PICKER_MISSING_KEY'})
        for identifier in ('does-not-exist', 'qwen3-embedding', 'locked', False):
            with self.subTest(identifier=identifier), patch.object(ModelRegistry, 'download') as download:
                with self.assertRaises(ValueError):
                    pi_models.select_model(self.state, identifier, download=True)
                download.assert_not_called()
        self.assertEqual(self.registry.load()['selected_chat'], 'first')


if __name__ == '__main__':
    unittest.main()
