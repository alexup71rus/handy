"""Browser flows share the real registry/forms and a loopback provider API."""

import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from rich.console import Console

from folder_memory.model_console import ModelConsole
from folder_memory.terminal_picker import PickerResult


class Answers:
    def __init__(self, *values):
        self.values = iter(values)
        self.calls = []

    def __call__(self, label, **options):
        self.calls.append((label, options))
        value = next(self.values)
        if isinstance(value, BaseException):
            raise value
        return value


class BrowserScript:
    def __init__(self, *steps):
        self.steps = iter(steps)
        self.screens = []

    def __call__(self, title, source, *, query='', actions=(), selected_id=None):
        items = list(source(query) if callable(source) else source)
        screen = {'title': title, 'items': items, 'query': query, 'actions': actions}
        self.screens.append(screen)
        step = next(self.steps)
        return step(screen) if callable(step) else step


class ModelConsoleTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='handy-browser-')))
        self.state = self.directory / 'state'
        self.enterContext(patch.dict(os.environ, {'HANDY_CONFIG_HOME': str(self.directory / 'config')}))
        self.enterContext(patch('folder_memory.model_registry.shutil.which', return_value=None))
        self.enterContext(patch('folder_memory.model_settings.engine_status', return_value={'status': 'stopped'}))
        self.activation = self.enterContext(patch('folder_memory.model_settings.activate_models'))
        self.finish_activation = self.enterContext(patch('folder_memory.model_console.activate_models'))
        self.output = io.StringIO()
        self.console = Console(file=self.output, width=100, color_system=None)
        self.requests = []
        self.payload = {'data': [{'id': 'Vendor/UPPER:2B'}, {'id': 'vendor/small'}]}
        self.http_status = 200
        fixture = self

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                fixture.requests.append(self.path)
                body = json.dumps(fixture.payload).encode()
                self.send_response(fixture.http_status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                fixture.requests.append('UNEXPECTED INFERENCE')
                self.send_error(500)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    def browser(self, steps=(), answers=(), **options):
        self.script = BrowserScript(*steps)
        self.answers = Answers(*answers)
        browser = ModelConsole(self.state, self.console, self.answers, picker=self.script, **options)
        browser.registry.load()
        return browser

    def provider(self, browser, identifier='fixture', **fields):
        browser.manager.perform({'operation': 'save_provider', 'id': identifier,
                                 'provider': {'name': 'Fixture API', 'base_url': self.base, 'auth': 'none', **fields}})

    def model(self, browser, identifier='manual', **fields):
        browser.manager.perform({'operation': 'save_profile', 'id': identifier,
                                 'profile': {'name': 'Manual', 'backend': 'openai', 'kind': 'chat',
                                             'model_id': 'Vendor/Exact-ID', 'provider_id': 'fixture',
                                             'context': 32768, **fields}})

    def test_provider_opens_inventory_and_selection_uses_exact_api_id(self):
        def selected(screen):
            self.assertEqual({item.title for item in screen['items']}, {'Vendor/UPPER:2B', 'vendor/small'})
            return PickerResult('select', next(item.id for item in screen['items'] if item.title == 'Vendor/UPPER:2B'))
        browser = self.browser([PickerResult('select', 'p:fixture'), selected])
        self.provider(browser)
        self.assertTrue(browser.run())
        selected = browser.registry.get(browser.registry.load()['selected_chat'])
        self.assertEqual(selected['model_id'], 'Vendor/UPPER:2B')
        self.assertEqual(self.requests, ['/v1/models'])
        self.activation.assert_called_once()
        self.assertEqual(self.activation.call_args.kwargs, {'start': False})

    def test_search_finds_models_across_providers_without_requests(self):
        def choose(screen):
            self.assertEqual([item.id for item in screen['items']], ['m:manual'])
            self.assertIn('Vendor/Exact-ID', screen['items'][0].detail)
            return PickerResult('select', 'm:manual', query='exact-id')
        browser = self.browser([choose], query='exact-id')
        self.provider(browser)
        self.model(browser)
        self.assertTrue(browser.run())
        self.assertEqual(browser.registry.load()['selected_chat'], 'manual')
        self.assertEqual(self.requests, [])

    def test_search_can_find_a_provider_even_when_it_has_no_models(self):
        browser = self.browser([PickerResult('cancel')], query='fixture api', management=True)
        self.provider(browser)
        self.assertFalse(browser.run())
        self.assertEqual([item.id for item in self.script.screens[0]['items']], ['p:fixture'])
        self.assertEqual(self.requests, [])

    def test_unavailable_inventory_keeps_manual_model_available(self):
        self.http_status = 404
        browser = self.browser([PickerResult('select', 'p:fixture'), PickerResult('select', 'm:manual')])
        self.provider(browser)
        self.model(browser)
        self.assertTrue(browser.run())
        self.assertEqual(browser.registry.load()['selected_chat'], 'manual')
        self.assertIn('HTTP 404', self.output.getvalue())
        self.assertEqual(self.requests, ['/v1/models'])

    def test_escape_at_provider_returns_to_root_and_cancel_does_not_apply(self):
        browser = self.browser([PickerResult('select', 'p:builtin-standard'), PickerResult('back'), PickerResult('cancel')])
        before = browser.registry.path.read_bytes()
        self.assertFalse(browser.run())
        self.assertEqual(len(self.script.screens), 3)
        self.assertEqual(browser.registry.path.read_bytes(), before)
        self.assertEqual(self.requests, [])
        self.activation.assert_not_called()
        self.finish_activation.assert_not_called()

    def test_missing_weights_require_confirmation_and_never_download_on_cancel(self):
        browser = self.browser([PickerResult('select', 'm:spark'), PickerResult('cancel')], ['нет'])
        with patch.object(browser.registry, 'download', side_effect=AssertionError('No download')):
            self.assertFalse(browser.run())
        self.assertEqual(browser.registry.load()['selected_chat'], 'lfm3-vl')
        self.assertTrue(self.answers.calls[0][1]['cancel_on_escape'])

    def test_provider_removal_is_confirmed_and_cascades_only_its_models(self):
        browser = self.browser([PickerResult('remove', 'p:fixture'), PickerResult('cancel')], ['да'], management=True)
        self.provider(browser)
        self.model(browser)
        before = set(browser.registry.profiles()) - {'manual'}
        self.assertTrue(browser.run())
        self.assertNotIn('fixture', browser.registry.providers())
        self.assertEqual(set(browser.registry.profiles()), before)
        self.assertTrue(self.answers.calls[0][1]['cancel_on_escape'])
        self.assertEqual(self.requests, [])

    def test_escape_in_confirmation_keeps_provider_and_does_not_apply(self):
        browser = self.browser([PickerResult('remove', 'p:fixture')], [KeyboardInterrupt()], management=True)
        self.provider(browser)
        self.model(browser)
        before = browser.registry.path.read_bytes()
        self.assertFalse(browser.run())
        self.assertEqual(browser.registry.path.read_bytes(), before)
        self.activation.assert_not_called()
        self.finish_activation.assert_not_called()

    def test_add_model_form_persists_exact_id_without_changing_current_selection(self):
        browser = self.browser([], ['New/Exact:ID', 'My model', '4096', 'нет', 'да'], provider='builtin-standard')
        self.provider(browser)
        browser.provider = 'fixture'
        before = browser.registry.load()['selected_chat']
        self.assertTrue(browser.run(action='add_model'))
        models = [p for p in browser.registry.profiles().values() if p.get('model_id') == 'New/Exact:ID']
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]['provider_id'], 'fixture')
        self.assertEqual(browser.registry.load()['selected_chat'], before)
        self.assertEqual(sum(label.startswith('ID модели') for label, _ in self.answers.calls), 1)
        self.assertEqual(self.requests, [])

    def test_cancelled_form_never_saves_or_applies(self):
        browser = self.browser([], [self.base, KeyboardInterrupt()])
        before = browser.registry.path.read_bytes()
        self.assertFalse(browser.run(action='add_provider'))
        self.assertEqual(browser.registry.path.read_bytes(), before)
        self.assertEqual(self.requests, [])
        self.finish_activation.assert_not_called()

    def test_default_list_hides_masked_models_management_can_reveal_them(self):
        browser = self.browser([PickerResult('cancel')], query='exact-id')
        self.provider(browser, visible=False)
        self.model(browser)
        browser.run()
        self.assertEqual(self.script.screens[0]['items'], [])
        visible = self.browser([PickerResult('cancel')], query='exact-id', management=True)
        visible.run()
        self.assertEqual([item.id for item in self.script.screens[0]['items']], ['m:manual'])

    def test_disabling_embedding_preserves_selected_chat(self):
        browser = self.browser([PickerResult('disable')], kind='embedding')
        before = browser.registry.load()['selected_chat']
        self.assertTrue(browser.run())
        self.assertIsNone(browser.registry.load()['selected_embedding'])
        self.assertEqual(browser.registry.load()['selected_chat'], before)

    def test_exit_apply_failure_does_not_trap_user_in_the_picker(self):
        browser = self.browser([PickerResult('cancel')])
        browser.edited = browser.pending_apply = True
        self.finish_activation.side_effect = ValueError('Restart failed')
        with self.assertRaisesRegex(ValueError, 'Restart failed'):
            browser.run()
        self.assertEqual(len(self.script.screens), 1)
        self.finish_activation.assert_called_once()

    def test_visibility_cascades_then_enables_one_child_without_activation(self):
        browser = self.browser([PickerResult('visibility', 'p:fixture'), PickerResult('visibility', 'm:first'), PickerResult('cancel')], management=True)
        self.provider(browser)
        self.model(browser, 'first')
        self.model(browser, 'second')
        before = browser.registry.load()['selected_chat']
        self.assertTrue(browser.run())
        data = browser.registry.load()
        self.assertTrue(data['providers']['fixture']['visible'])
        self.assertTrue(data['profiles']['first']['visible'])
        self.assertFalse(data['profiles']['second']['visible'])
        self.assertEqual(data['selected_chat'], before)
        self.activation.assert_not_called()
        self.finish_activation.assert_not_called()


if __name__ == '__main__':
    unittest.main()
