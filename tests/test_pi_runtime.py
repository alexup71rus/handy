"""Pi config, credentials and terminal launch keep their real boundary contracts."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from folder_memory import pi_runtime as pi
from folder_memory import pi_control, cli
from folder_memory.model_registry import ModelRegistry


class PiRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.enterContext(patch.dict(os.environ, {'HANDY_CONFIG_HOME': str(self.root / 'config')}))
        self.registry = ModelRegistry(self.state)
        self.registry.add('fixture', {'kind': 'chat', 'backend': 'openai', 'enabled': True,
                                     'name': 'Fixture', 'model_id': 'fixture-model', 'context': 4096,
                                     'base_url': 'http://127.0.0.1:12345/v1', 'api_key_env': 'HANDY_TEST_TOKEN'})
        self.registry.select('fixture')

    def test_generates_native_pi_models_and_mcp_without_copying_secrets(self):
        with patch.dict(os.environ, {'HANDY_TEST_TOKEN': 'test-not-a-real-token'}):
            directory = pi.configure(self.state)
            models = json.loads((directory / 'models.json').read_text())
            provider = models['providers']['handy-fixture']
            self.assertEqual(provider['models'][0]['id'], 'fixture-model')
            self.assertNotIn('test-not-a-real-token', (directory / 'models.json').read_text())
            self.assertEqual(pi_control.credential(self.state, 'fixture'), 'test-not-a-real-token')
        servers = json.loads((directory / 'mcp.json').read_text())['mcpServers']
        self.assertEqual(servers['handy']['cwd'], '.')
        self.assertIn('--no-usage-statistics', servers['browser']['args'])
        with self.assertRaises(ValueError):
            pi_control.credential(self.state, 'fixture')

    def test_preferences_preserve_pi_auth_and_unmanaged_settings(self):
        directory = pi.configure(self.state)
        auth = directory / 'auth.json'
        auth.write_text('{"fixture":"private"}')
        path = directory / 'settings.json'
        settings = json.loads(path.read_text()); settings['customEditor'] = 'vim'
        path.write_text(json.dumps(settings))
        with patch('folder_memory.pi_scheduler.stop_worker') as stop:
            pi.save_settings(self.state, {'scheduler_enabled': False, 'theme': 'light'})
            stop.assert_called_once()
        self.assertEqual(auth.read_text(), '{"fixture":"private"}')
        self.assertEqual(json.loads(path.read_text())['customEditor'], 'vim')
        self.assertIn('--no-scheduler', json.loads((directory/'mcp.json').read_text())['mcpServers']['handy']['args'])

    def test_api_sampling_preferences_reach_the_pi_request_configuration(self):
        self.registry.update('fixture', {**self.registry.get('fixture'),
                                        'sampling': {'temperature': 0.23, 'top_p': 0.7}})
        with patch.dict(os.environ, {'HANDY_TEST_TOKEN': 'fixture'}):
            directory = pi.configure(self.state)
        provider = json.loads((directory/'models.json').read_text())['providers']['handy-fixture']
        self.assertEqual(provider['models'][0]['samplingParams'], {'temperature': 0.23, 'top_p': 0.7})

    def test_colliding_provider_names_keep_distinct_endpoints_and_picker_groups(self):
        from folder_memory.pi_models import inventory, select_model
        binary = self.root/'llama-server'; binary.write_text(''); binary.chmod(0o700)
        weights = self.root/'model.gguf'; weights.touch()
        self.registry.add('local', {'kind':'chat', 'backend':'local', 'model':str(weights), 'binary':str(binary)})
        self.registry.save_provider('system', {'name':'System API', 'base_url':'http://127.0.0.1:12346/v1'})
        self.registry.add('remote', {'kind':'chat', 'backend':'openai', 'provider_id':'system', 'model_id':'remote'})
        self.registry.save_provider('shared', {'name':'Named', 'base_url':'http://127.0.0.1:12347/v1'})
        self.registry.add('named', {'kind':'chat', 'backend':'openai', 'provider_id':'shared', 'model_id':'named'})
        self.registry.add('shared', {'kind':'chat', 'backend':'openai', 'base_url':'http://127.0.0.1:12348/v1', 'model_id':'standalone'})
        groups = {group['id']:group for group in inventory(self.state)['groups']}
        self.assertTrue(groups['handy-system']['system'])
        self.assertFalse(groups['handy-provider:system']['system'])
        for identifier in ('remote', 'named', 'shared'):
            with self.subTest(identifier=identifier), patch('folder_memory.pi_models.activate_models'):
                selected = select_model(self.state, identifier)
            providers = json.loads((pi.agent_dir(self.state)/'models.json').read_text())['providers']
            connection = providers[selected['pi_provider']]
            self.assertEqual(connection['baseUrl'], self.registry.get(identifier)['base_url'])
            self.assertEqual([item['id'] for item in connection['models']], [selected['pi_model']])
            self.assertIn(selected['pi_provider'], groups)

    def test_plain_http_preserves_lan_hostname_and_public_dns_cannot_release_a_key(self):
        import socket
        port = 12345
        self.registry.save_provider('lan', {'name':'LAN', 'base_url':f'http://home.example:{port}/v1',
                                           'api_key_env':'HANDY_TEST_TOKEN'})
        for identifier in ('one', 'two'):
            self.registry.add(identifier, {'kind':'chat', 'backend':'openai', 'provider_id':'lan', 'model_id':identifier})
        self.registry.select('one')
        address = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('::1', port, 0, 0)),
                   (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', port))]
        with patch.dict(os.environ, {'HANDY_TEST_TOKEN':'fixture'}), \
                patch('folder_memory.remote_model.socket.getaddrinfo', return_value=address) as resolve:
            directory = pi.configure(self.state)
        connection = json.loads((directory/'models.json').read_text())['providers']['handy-lan']
        self.assertEqual(connection['baseUrl'], f'http://home.example:{port}/v1')
        self.assertNotIn('headers', connection)
        resolve.assert_called_once()
        address[0] = (*address[0][:4], ('8.8.8.8', 12345))
        with patch('folder_memory.remote_model.socket.getaddrinfo', return_value=address), \
                patch('folder_memory.remote_model.OpenAIModel._key', side_effect=AssertionError('Unsafe URL read a key')):
            with self.assertRaisesRegex(ValueError, 'HTTPS'):
                pi_control.credential(self.state, 'one')
            with patch.dict(os.environ, {'HANDY_TEST_TOKEN':'fixture'}), self.assertRaisesRegex(ValueError, 'HTTPS'):
                pi.configure(self.state)

    def test_api_profile_validation_and_credentials_keep_the_transport_guards(self):
        bad = {**self.registry.get('fixture'), 'base_url':'http://8.8.8.8/v1'}
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            self.registry.add('unsafe', bad)
        self.assertFalse(self.registry.profile_ready(bad))
        token = self.state/'token'; token.write_text('PRIVATE_LOCAL_TOKEN'); token.chmod(0o600)
        linked = self.root/'provider-key'; os.link(token, linked)
        self.registry.add('linked', {'kind':'chat','backend':'openai','model_id':'linked',
                                    'base_url':'https://provider.example/v1','api_key_file':str(linked)})
        with self.assertRaisesRegex(ValueError, 'Локальный токен'):
            pi_control.credential(self.state, 'linked')

    def test_rejects_external_browser_endpoint_and_invalid_preferences(self):
        for values in ({'browser_cdp_url': 'https://remote.example:9222'}, {'notifications_enabled': 1},
                       {'theme':'unknown'}, {'browser_cdp_url': 'http://127.0.0.1:bad'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                pi.validate_settings(values)

    def test_open_chat_passes_quoted_data_to_terminal(self):
        session = self.root / "session '$x;echo nope.jsonl"; session.touch()
        import subprocess
        with patch.object(pi_control.sys, 'platform', 'darwin'), \
             patch.object(pi_control.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'tab 1 of window 2\n', '')) as run:
            pi_control.open_chat(self.state, {'session_path': str(session), 'cwd':str(self.root)})
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ['/usr/bin/osascript','-e'])
        import shlex
        args = shlex.split(command[-1])
        self.assertEqual(args[-1], str(session))
        self.assertEqual(args[-3], str(self.root))
        self.assertTrue(run.call_args.kwargs['capture_output'])

    def test_cli_replaces_process_with_pi_in_requested_workspace(self):
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        with patch.object(pi, 'prepare'), patch.object(pi, 'load_settings', return_value={'scheduler_enabled':False}), \
             patch.object(os, 'execve', side_effect=SystemExit(0)) as launch:
            with self.assertRaises(SystemExit):
                cli.main(['--no-app','--state-dir',str(self.state),'-C',str(self.root),'--session','abc.jsonl'])
        self.assertEqual(Path.cwd(), self.root)
        args = launch.call_args.args
        self.assertEqual(args[0], pi.executable())
        self.assertIn('--session', args[1])
        self.assertEqual(args[2]['PI_CODING_AGENT_DIR'], str(pi.agent_dir(self.state)))

    def test_hidden_selected_model_stays_default_instead_of_paid_fallback(self):
        self.registry.update('fixture', {**self.registry.get('fixture'), 'visible': False})
        with patch.dict(os.environ, {'HANDY_TEST_TOKEN': 'fixture'}):
            directory = pi.configure(self.state)
        settings = json.loads((directory / 'settings.json').read_text())
        self.assertEqual(settings['defaultProvider'], 'handy-fixture')

    def test_export_uses_one_registry_snapshot_when_another_window_edits_models(self):
        original = ModelRegistry.load
        loads = []
        def snapshot(registry):
            loads.append(True)
            if len(loads) > 1:
                raise AssertionError('Export reloaded a concurrently changed registry')
            return original(registry)
        with patch.dict(os.environ, {'HANDY_TEST_TOKEN': 'fixture'}), patch.object(ModelRegistry, 'load', snapshot):
            directory = pi.configure(self.state)
        exported = json.loads((directory/'models.json').read_text())
        self.assertEqual(exported['providers']['handy-fixture']['models'][0]['id'], 'fixture-model')

    def test_pi_requests_bypass_legacy_context_augmentation(self):
        from contextlib import nullcontext
        from unittest.mock import Mock
        from folder_memory.server import App
        from folder_memory.neural import CHAT_MODEL
        app = object.__new__(App)
        app._ensure_running = Mock()
        app.model_config = 'a' * 64
        app.neural_model = Mock()
        response = object()
        app.neural_model.chat_completion.return_value = nullcontext(response)
        body = {'model': CHAT_MODEL, 'messages': [{'role': 'user', 'content': 'hello'}]}
        with patch('folder_memory.chat_evidence.augment_chat', side_effect=AssertionError('Must not inject old context')):
            with app.model_completion(body, {'X-Handy-Client': 'pi', 'X-Handy-Model-Config': app.model_config}) as actual:
                self.assertIs(actual, response)
        app.neural_model.chat_completion.assert_called_once_with(body, cancelled=None)

    def test_concurrent_model_switch_cannot_silently_answer_with_another_local_model(self):
        from unittest.mock import Mock
        from folder_memory.server import App, MODEL
        app = object.__new__(App)
        app._ensure_running = Mock()
        app.model_config = 'a' * 64
        app.neural_model = Mock()
        body = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'hello'}]}
        for fingerprint in ('b' * 64, '', 'не digest'):
            with self.subTest(fingerprint=fingerprint), self.assertRaisesRegex(ValueError, '/models'):
                with app.model_completion(body, {'X-Handy-Client': 'pi', 'X-Handy-Model-Config': fingerprint}):
                    self.fail('A stale session reached the new local model')
        app.neural_model.chat_completion.assert_not_called()

    def test_mcp_native_toggles_and_custom_servers_survive_exports_and_settings_saves(self):
        directory = pi.configure(self.state)
        path = directory / 'mcp.json'
        config = json.loads(path.read_text())
        config['mcpServers']['browser']['enabled'] = False
        config['mcpServers']['custom'] = {'command': '/tmp/custom', 'args': ['private-argument'], 'enabled': False}
        path.write_text(json.dumps(config))
        original = pi.load_settings(self.state)
        with patch('folder_memory.pi_scheduler.start_worker'), patch('folder_memory.pi_scheduler.stop_worker'):
            pi.save_settings(self.state, {'theme': 'system', 'browser_enabled': original['browser_enabled']})
        exported = json.loads(path.read_text())['mcpServers']
        self.assertFalse(exported['browser']['enabled'])
        self.assertEqual(exported['custom'], config['mcpServers']['custom'])
        with patch('folder_memory.pi_scheduler.start_worker') as start, patch('folder_memory.pi_scheduler.stop_worker') as stop:
            result = pi.set_mcp_enabled(self.state, 'handy', False)
        start.assert_not_called(); stop.assert_not_called()
        self.assertEqual(result['settings'], pi.load_settings(self.state))
        self.assertEqual(result['settings']['scheduler_enabled'], original['scheduler_enabled'])
        pi.configure(self.state)
        self.assertFalse(json.loads(path.read_text())['mcpServers']['handy']['enabled'])
        self.assertFalse(next(row for row in result['servers'] if row['id'] == 'handy')['enabled'])

    def test_browser_legacy_toggle_changes_master_only_on_an_actual_preference_change(self):
        pi.configure(self.state)
        enabled = pi.load_settings(self.state)['browser_enabled']
        with patch('folder_memory.pi_scheduler.start_worker'), patch('folder_memory.pi_scheduler.stop_worker'):
            pi.save_settings(self.state, {'browser_enabled': not enabled})
        self.assertEqual(next(row for row in pi.mcp_servers(self.state) if row['id'] == 'browser')['enabled'], not enabled)
        result = pi.set_mcp_enabled(self.state, 'browser', enabled)
        self.assertEqual(result['settings']['browser_enabled'], enabled)

    def test_passive_server_inventory_never_exposes_connection_credentials(self):
        directory = pi.configure(self.state)
        path = directory / 'mcp.json'
        config = json.loads(path.read_text())
        config['mcpServers']['remote'] = {'url': 'https://login:private@example.test/path?key=secret'}
        config['mcpServers']['custom'] = {'command': '/tmp/my-server', 'args': ['password'], 'env': {'KEY': 'hidden'}}
        path.write_text(json.dumps(config))
        rows = {row['id']: row for row in pi.mcp_servers(self.state)}
        self.assertEqual(rows['remote']['detail'], 'https://example.test')
        self.assertEqual(rows['custom']['detail'], 'my-server')
        self.assertFalse(rows['custom']['managed'])
        self.assertNotIn('private', json.dumps(rows))

    def test_system_context_window_and_output_reserve_do_not_change_api_profiles(self):
        binary = self.root / 'llama-server'
        binary.write_text('#!/bin/sh\nexit 0\n'); binary.chmod(0o700)
        weights = self.root / 'model.gguf'; weights.write_bytes(b'weights')
        self.registry.add('installed', {'kind':'chat','backend':'local','enabled':True,
                                      'model':str(weights),'binary':str(binary),'context':32768})
        with patch.dict(os.environ, {'HANDY_TEST_TOKEN':'fixture'}):
            directory = pi.configure(self.state)
        models = json.loads((directory/'models.json').read_text())['providers']
        self.assertEqual(models['handy-system']['models'][0]['contextWindow'], 32768)
        self.assertEqual(models['handy-fixture']['models'][0]['contextWindow'], 4096)
        settings = json.loads((directory/'settings.json').read_text())
        compaction = settings['compaction']['modelOverrides']
        local = compaction['handy-system/installed']
        self.assertEqual(32768-local['reserveTokens'], 27648)
        self.assertLess(local['keepRecentTokens'] + 4096, 27648)
        self.assertNotIn('handy-fixture/fixture-model', compaction)
        self.assertEqual(settings['sessionDir'], str(self.state/'pi/sessions'))

    def test_first_interactive_launch_without_ready_models_allows_the_picker(self):
        with patch('folder_memory.model_activation.activate_models') as activate:
            directory = pi.prepare(self.state, allow_unconfigured=True)
        self.assertTrue((directory/'models.json').is_file())
        activate.assert_not_called()

    def test_environment_marks_only_missing_exports_as_requiring_model_selection(self):
        with patch.dict(os.environ, {'HANDY_TEST_TOKEN':'fixture', 'HANDY_REQUIRE_MODEL_SELECTION':'1'}):
            directory = pi.configure(self.state)
            self.assertEqual(pi.environment(self.state)['HANDY_REQUIRE_MODEL_SELECTION'], '0')
        exported = json.loads((directory/'settings.json').read_text())
        exported.pop('defaultProvider'); exported.pop('defaultModel')
        (directory/'settings.json').write_text(json.dumps(exported))
        with patch.dict(os.environ, {'HANDY_REQUIRE_MODEL_SELECTION':'0'}):
            self.assertEqual(pi.environment(self.state)['HANDY_REQUIRE_MODEL_SELECTION'], '1')

    def test_unready_selection_never_substitutes_another_paid_provider(self):
        self.registry.add('other', {'kind':'chat','backend':'openai','name':'Other',
                                   'model_id':'other','base_url':'http://127.0.0.1:12345/v1'})
        with self.assertRaisesRegex(ValueError, 'готовую модель'):
            pi.prepare(self.state, allow_unconfigured=True)

    def test_disabled_selection_never_substitutes_another_ready_api(self):
        self.registry.update('fixture', {**self.registry.get('fixture'), 'enabled':False})
        self.registry.add('other', {'kind':'chat','backend':'openai','model_id':'other',
                                   'base_url':'http://127.0.0.1:12345/v1'})
        with patch.dict(os.environ, {'HANDY_TEST_TOKEN':'fixture'}), \
                patch('folder_memory.model_activation.activate_models') as activate, patch.object(pi,'configure') as configure:
            with self.assertRaisesRegex(ValueError, 'готовую модель'):
                pi.prepare(self.state, allow_unconfigured=True)
        activate.assert_not_called(); configure.assert_not_called()

    def test_launcher_opens_settings_app_even_if_model_needs_repair(self):
        import contextlib, io
        with patch.object(cli, 'open_menu') as app, patch.object(pi, 'prepare', side_effect=ValueError('Model needs setup')), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--state-dir',str(self.state),'-C',str(self.root)]), 1)
        app.assert_called_once_with(self.state)


if __name__ == '__main__':
    unittest.main()
