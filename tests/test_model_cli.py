"""Model CLI exercises the shared registry and provider API without model runs."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from folder_memory import cli, model_cli
from folder_memory.model_registry import ModelRegistry


class _TerminalInput(io.StringIO):
    def isatty(self):
        return True


class ModelCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="handy-model-cli-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state = self.directory / "runtime"
        self.state.mkdir()
        self.root = self.directory / "config"
        self.enterContext(patch.dict(os.environ, {"HANDY_CONFIG_HOME": str(self.root), "TEST_PROVIDER_KEY": "TEST_ENV_KEY"}))
        self.binary = self.directory / "llama-server"
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.binary.chmod(0o700)
        self.weights = self.directory / "test model.gguf"
        self.weights.write_bytes(b"GGUF" + struct.pack("<I", 3) + b"fixture")
        self.enterContext(patch("folder_memory.model_registry.shutil.which", return_value=str(self.binary)))
        self.enterContext(patch("folder_memory.model_settings.engine_status", return_value={"status": "stopped"}))
        self.registry = ModelRegistry(self.state)
        self.registry.load()
        self.chat_start = self.enterContext(patch.object(cli.pi, "prepare", side_effect=AssertionError("Must not start chat")))
        self.app_start = self.enterContext(patch.object(cli, "open_menu", side_effect=AssertionError("Must not open menu")))

    def run_cli(self, *arguments, stdin=None):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            with patch.object(model_cli.sys, "stdin", stdin if stdin is not None else io.StringIO()):
                result = cli.main(["models", *arguments, "--state-dir", str(self.state)])
        return result, output.getvalue(), errors.getvalue()

    def local(self, identifier="local", **fields):
        profile = {"name": "Local", "kind": "chat", "backend": "local", "enabled": True,
                   "model": str(self.weights), "binary": str(self.binary), "context": 4096, **fields}
        self.registry.add(identifier, profile)
        return profile

    def provider(self, identifier="test-api", **fields):
        self.registry.save_provider(identifier, {"name": "Test API", "base_url": "http://127.0.0.1:12345/v1", **fields})

    def api(self, identifier="test-chat", kind="chat", **fields):
        if "test-api" not in self.registry.providers():
            self.provider()
        profile = {"name": "Test chat", "kind": kind, "backend": "openai", "enabled": True,
                   "provider_id": "test-api", "model_id": "manual-model", "context": 4096, **fields}
        if kind == "embedding":
            profile.setdefault("dimensions", 2)
        self.registry.add(identifier, profile)

    def test_default_list_is_global_json_and_never_starts_chat_or_app(self):
        code, output, error = self.run_cli("--json")
        self.assertEqual(code, 0, error)
        data = json.loads(output)
        self.assertEqual(data["selected_chat"], "lfm3-vl")
        self.assertEqual({p["id"] for p in data["profiles"]}, {"lfm3-vl", "spark", "qwen3-embedding"})
        self.assertEqual(data["config_root"], str(self.root))
        self.chat_start.assert_not_called()
        self.app_start.assert_not_called()

    def test_interactive_models_and_providers_open_the_shared_browser(self):
        with patch.object(model_cli, '_interactive_tty', return_value=True), \
                patch.object(model_cli, '_interactive_menu', return_value=0) as menu:
            self.assertEqual(self.run_cli()[0], 0)
            menu.assert_called_once_with(self.state.resolve())
            menu.reset_mock()
            self.assertEqual(self.run_cli('providers')[0], 0)
            menu.assert_called_once_with(self.state.resolve(), management=True)

    def test_bare_crud_commands_open_forms_but_redirected_scripts_require_id(self):
        actions = [(('add',), 'add_model'), (('edit',), 'edit_model'), (('remove',), 'remove_model'),
                   (('providers', 'add'), 'add_provider'), (('providers', 'edit'), 'edit_provider'),
                   (('providers', 'remove'), 'remove_provider')]
        with patch.object(model_cli, '_interactive_tty', return_value=True), \
                patch.object(model_cli, '_interactive_menu', return_value=0) as menu:
            for arguments, action in actions:
                menu.reset_mock()
                self.assertEqual(self.run_cli(*arguments)[0], 0)
                menu.assert_called_once_with(self.state.resolve(), management=True, action=action)
        with patch.object(model_cli, '_interactive_tty', return_value=False):
            with self.assertRaises(SystemExit) as raised:
                self.run_cli('edit')
            self.assertEqual(raised.exception.code, 2)
        self.chat_start.assert_not_called()
        self.app_start.assert_not_called()

    def test_json_and_explicit_list_never_start_interactive_browser(self):
        with patch.object(model_cli, '_interactive_tty', return_value=True), \
                patch.object(model_cli, '_interactive_menu', side_effect=AssertionError('Not a browser request')):
            self.assertEqual(self.run_cli('--json')[0], 0)
            self.assertEqual(self.run_cli('list')[0], 0)

    def test_providers_top_level_alias_never_starts_chat(self):
        self.provider()
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = cli.main(['providers', 'list', '--json', '--state-dir', str(self.state)])
        self.assertEqual(code, 0, errors.getvalue())
        self.assertEqual(json.loads(output.getvalue())['providers'][0]['name'], 'Test API')
        self.chat_start.assert_not_called()
        self.app_start.assert_not_called()

    def test_global_options_before_management_subcommand_never_start_chat(self):
        self.provider()
        for arguments, key in ((['--state-dir', str(self.state), 'models', 'list', '--json'], 'profiles'),
                               (['--state-dir=' + str(self.state), 'providers', 'list', '--json'], 'providers'),
                               (['--json', '--ids', '--state-dir', str(self.state), 'models', 'list'], 'profiles')):
            with self.subTest(arguments=arguments):
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    self.assertEqual(cli.main(arguments), 0)
                self.assertIn(key, json.loads(output.getvalue()))
                self.assertEqual(errors.getvalue(), '')
        self.chat_start.assert_not_called()
        self.app_start.assert_not_called()

    def test_search_matches_all_words_across_api_id_name_and_provider(self):
        self.api('one', name='Small coder', model_id='vendor/GPT-Mini')
        self.api('two', name='Big coder', model_id='vendor/GPT-Large')
        code, output, error = self.run_cli('search', 'test', 'mini', '--json')
        self.assertEqual(code, 0, error)
        self.assertEqual([item['id'] for item in json.loads(output)['profiles']], ['one'])
        code, output, error = self.run_cli('list', '--provider', 'test api', '--search', 'large', '--json')
        self.assertEqual(code, 0, error)
        self.assertEqual([item['id'] for item in json.loads(output)['profiles']], ['two'])
        code, output, error = self.run_cli('search', 'does-not-exist', '--json')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['profiles'], [])

    def test_provider_list_search_and_name_references(self):
        self.provider()
        self.provider('other', name='Other API', base_url='https://other.example/v1')
        code, output, error = self.run_cli('providers', 'list', '--search', 'OTHER', '--json')
        self.assertEqual(code, 0, error)
        self.assertEqual([item['id'] for item in json.loads(output)['providers']], ['other'])
        code, _, error = self.run_cli('providers', 'edit', 'Test API', '--hidden')
        self.assertEqual(code, 0, error)
        self.assertFalse(self.registry.providers()['test-api']['visible'])
        code, _, error = self.run_cli('add', 'human-ref', '--provider', 'Test API', '--model', 'CaseSensitive/ID')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.registry.get('human-ref')['provider_id'], 'test-api')

    def test_use_api_id_and_disambiguate_duplicate_provider_models(self):
        self.api('one', model_id='vendor/CaseSensitive')
        self.provider('second', name='Second API')
        self.registry.add('two', {'kind': 'chat', 'backend': 'openai', 'name': 'Other copy', 'provider_id': 'second',
                                  'model_id': 'vendor/CaseSensitive', 'context': 4096})
        code, _, error = self.run_cli('use', 'vendor/CaseSensitive')
        self.assertEqual(code, 1)
        self.assertIn('--provider', error)
        self.assertEqual(self.registry.load()['selected_chat'], 'lfm3-vl')
        with patch('folder_memory.model_settings.activate_models'):
            code, _, error = self.run_cli('use', 'vendor/CaseSensitive', '--provider', 'Second API')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.registry.load()['selected_chat'], 'two')
        code, _, _ = self.run_cli('use', 'vendor/casesensitive', '--provider', 'Second API')
        self.assertEqual(code, 1)

    def test_edit_and_remove_accept_unique_model_names(self):
        self.api('named', name='Friendly coder', model_id='vendor/exact')
        code, _, error = self.run_cli('edit', 'Friendly coder', '--context', '8192')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.registry.get('named')['context'], 8192)
        code, _, error = self.run_cli('remove', 'vendor/exact', '--provider', 'Test API')
        self.assertEqual(code, 0, error)
        self.assertNotIn('named', self.registry.profiles())

    def test_provider_model_search_filters_actual_inventory_response(self):
        self.provider()
        response = {'data': [{'id': 'vendor/mini'}, {'id': 'vendor/large'}]}
        with patch('folder_memory.model_settings._request', return_value=response):
            code, output, error = self.run_cli('providers', 'models', 'Test API', '--search', 'MINI', '--json')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['models'], [{'id': 'vendor/mini'}])
        self.assertEqual(json.loads(output)['count'], 1)

    def test_local_api_inventory_flows_through_search_and_model_choice(self):
        requests = []

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get('Authorization')))
                body = json.dumps({'data': [{'id': 'Vendor/UPPER:2B'}, {'id': 'vendor/large'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        serving = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        serving.start()
        self.addCleanup(server.server_close)
        self.addCleanup(serving.join)
        self.addCleanup(server.shutdown)
        self.provider(base_url=f'http://127.0.0.1:{server.server_port}/v1', api_key_env='TEST_PROVIDER_KEY')
        selected = self.registry.load()['selected_chat']
        code, output, error = self.run_cli('providers', 'models', 'Test API', '--search', 'UPPER', '--json')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['models'], [{'id': 'Vendor/UPPER:2B'}])
        self.assertEqual(self.registry.load()['selected_chat'], selected)
        self.assertEqual(requests, [('/v1/models', 'Bearer TEST_ENV_KEY')])
        self.assertNotIn('TEST_ENV_KEY', output + error)
        with patch('folder_memory.model_settings.activate_models'):
            code, _, error = self.run_cli('use', 'Vendor/UPPER:2B', '--provider', 'test api')
        self.assertEqual(code, 0, error)
        chosen = self.registry.get(self.registry.load()['selected_chat'])
        self.assertEqual(chosen['model_id'], 'Vendor/UPPER:2B')
        self.assertEqual(chosen['provider_id'], 'test-api')
        self.chat_start.assert_not_called()

    def test_opaque_ids_are_optional_but_api_id_and_provider_are_readable(self):
        self.api('api-opaque-internal', name='Friendly model', model_id='vendor/chat')
        code, output, error = self.run_cli('list', '--provider', 'Test API')
        self.assertEqual(code, 0, error)
        self.assertIn('Friendly model', output)
        self.assertIn('vendor/chat', output)
        self.assertIn('Test API', output)
        self.assertNotIn('api-opaque-internal', output)
        code, output, error = self.run_cli('list', '--provider', 'Test API', '--ids')
        self.assertEqual(code, 0, error)
        self.assertIn('api-opaque-internal', output)

    def test_global_options_work_around_nested_commands_and_kind_filters_json(self):
        code, output, error = self.run_cli("--json", "list", "--kind", "embedding")
        self.assertEqual(code, 0, error)
        self.assertEqual([p["id"] for p in json.loads(output)["profiles"]], ["qwen3-embedding"])
        code, output, error = self.run_cli("providers", "list", "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["providers"], [])

    def test_human_list_is_readable_without_json(self):
        code, output, error = self.run_cli("list")
        self.assertEqual(code, 0, error)
        self.assertIn("lfm3-vl", output)
        self.assertIn("Не готова", output)
        self.assertNotIn('"profiles"', output)

    def test_add_local_by_flags_and_validate_files_without_loading_model(self):
        code, _, error = self.run_cli("add", "local", "--model", str(self.weights), "--name", "My local model", "--context", "8192")
        self.assertEqual(code, 0, error)
        self.assertEqual(self.registry.get("local")["binary"], str(self.binary))
        self.assertEqual(self.registry.get("local")["model"], str(self.weights))
        with patch("folder_memory.model_settings._request", side_effect=AssertionError("Local file check must not run inference")):
            code, output, error = self.run_cli("test", "local", "--json")
        self.assertEqual(code, 0, error)
        self.assertGreaterEqual(json.loads(output)["milliseconds"], 0)

    def test_add_api_by_flags_uses_saved_provider_and_manual_exact_id(self):
        self.provider()
        code, _, error = self.run_cli("add", "manual", "--provider", "test-api", "--model", "vendor/unknown:latest", "--context", "32768")
        self.assertEqual(code, 0, error)
        profile = self.registry.get("manual")
        self.assertEqual(profile["backend"], "openai")
        self.assertEqual(profile["model_id"], "vendor/unknown:latest")
        self.assertEqual(profile["base_url"], self.registry.providers()["test-api"]["base_url"])

    def test_add_embedding_by_flags_preserves_prefix_whitespace(self):
        self.provider()
        code, _, error = self.run_cli("add", "encoder", "--kind", "embedding", "--provider", "test-api", "--model", "embed-v2",
                                      "--dimensions", "2", "--query-prefix", "Query: ", "--passage-prefix", "Doc: ")
        self.assertEqual(code, 0, error)
        profile = self.registry.get("encoder")
        self.assertEqual(profile["query_prefix"], "Query: ")
        self.assertEqual(profile["passage_prefix"], "Doc: ")
        self.assertEqual(profile["context"], 8192)

    def test_partial_json_edit_deep_merges_and_never_drops_unrelated_fields(self):
        original = self.local(sampling={"temperature": 0.1, "top_k": 50}, future_option={"a": 1, "b": 2})
        incoming = {"name": "Renamed", "sampling": {"temperature": .3}, "future_option": {"b": 9}}
        code, _, error = self.run_cli("edit", "local", "--config", "-", stdin=io.StringIO(json.dumps(incoming)))
        self.assertEqual(code, 0, error)
        profile = self.registry.get("local")
        self.assertEqual(profile["sampling"], {"temperature": .3, "top_k": 50})
        self.assertEqual(profile["future_option"], {"a": 1, "b": 9})
        self.assertEqual(profile["model"], original["model"])
        self.assertEqual(profile["name"], "Renamed")

    def test_add_accepts_named_profile_from_full_registry_config(self):
        incoming = {"profiles": {"custom": {"kind": "chat", "backend": "local", "name": "Custom", "model": str(self.weights)}}}
        path = self.directory / "profiles.json"
        path.write_text(json.dumps(incoming))
        code, _, error = self.run_cli("add", "custom", "--config", str(path))
        self.assertEqual(code, 0, error)
        self.assertEqual(self.registry.get("custom")["context"], 65536)

    def test_edit_flags_win_over_json_without_changing_selection(self):
        self.local()
        code, _, error = self.run_cli("edit", "local", "--config", "-", "--name", "CLI name", "--context", "8192",
                                      stdin=io.StringIO('{"name":"JSON name","context":16384}'))
        self.assertEqual(code, 0, error)
        profile = self.registry.get("local")
        self.assertEqual(profile["name"], "CLI name")
        self.assertEqual(profile["context"], 8192)
        self.assertEqual(self.registry.load()["selected_chat"], "lfm3-vl")

    def test_provider_add_and_partial_edit_keep_api_references(self):
        code, _, error = self.run_cli("providers", "add", "api", "--base-url", "http://127.0.0.1:12345/v1",
                                      "--api-key-env", "TEST_PROVIDER_KEY")
        self.assertEqual(code, 0, error)
        code, _, error = self.run_cli("providers", "edit", "api", "--config", "-", stdin=io.StringIO('{"name":"Renamed API"}'))
        self.assertEqual(code, 0, error)
        provider = self.registry.providers()["api"]
        self.assertEqual(provider["name"], "Renamed API")
        self.assertEqual(provider["api_key_env"], "TEST_PROVIDER_KEY")

    def test_provider_ask_key_is_not_printed_or_stored_in_registry(self):
        with patch.object(model_cli.getpass, "getpass", return_value="TEST_PRIVATE_SECRET") as prompt:
            code, output, error = self.run_cli("providers", "add", "private", "--base-url", "http://127.0.0.1:12345/v1",
                                             "--ask-key", "--json", stdin=_TerminalInput())
        self.assertEqual(code, 0, error)
        prompt.assert_called_once()
        self.assertNotIn("TEST_PRIVATE_SECRET", output + error + self.registry.path.read_text())
        key = Path(self.registry.providers()["private"]["api_key_file"])
        self.assertEqual(key.read_text(), "TEST_PRIVATE_SECRET")
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)

    def test_provider_stdin_secret_is_kept_private_and_ask_key_requires_terminal(self):
        payload = {"provider": {"name": "Private", "base_url": "http://127.0.0.1:12345/v1", "auth": "key"}, "api_key": "STDIN_PRIVATE_SECRET"}
        code, output, error = self.run_cli("providers", "add", "private", "--config", "-", "--json", stdin=io.StringIO(json.dumps(payload)))
        self.assertEqual(code, 0, error)
        self.assertNotIn("STDIN_PRIVATE_SECRET", output + error + self.registry.path.read_text())
        code, output, error = self.run_cli("providers", "add", "other", "--base-url", "http://127.0.0.1:12345/v1", "--ask-key")
        self.assertEqual(code, 1)
        self.assertIn("нужен терминал", error)
        self.assertNotIn("other", self.registry.providers())

    def test_provider_edit_can_switch_auth_to_file_via_partial_json(self):
        self.provider(api_key_env="TEST_PROVIDER_KEY")
        key_file = self.directory / "test.key"
        key_file.write_text("FILE_PRIVATE_SECRET")
        key_file.chmod(0o600)
        code, _, error = self.run_cli("providers", "edit", "test-api", "--config", "-",
                                      stdin=io.StringIO(json.dumps({"api_key_file": str(key_file)})))
        self.assertEqual(code, 0, error)
        provider = self.registry.providers()["test-api"]
        self.assertNotIn("api_key_env", provider)
        self.assertEqual(provider["api_key_file"], str(key_file))

    def test_remove_profile_keeps_weights_and_rejects_selected_profile(self):
        self.local()
        code, _, error = self.run_cli("remove", "local")
        self.assertEqual(code, 0, error)
        self.assertTrue(self.weights.exists())
        code, _, error = self.run_cli("remove", "lfm3-vl")
        self.assertEqual(code, 1)
        self.assertIn("lfm3-vl", self.registry.profiles())

    def test_provider_removal_cascades_its_profiles_and_keeps_other_models(self):
        self.api()
        self.local()
        code, _, error = self.run_cli("providers", "remove", "test-api")
        self.assertEqual(code, 0, error)
        self.assertNotIn("test-api", self.registry.providers())
        self.assertNotIn("test-chat", self.registry.profiles())
        self.assertIn("local", self.registry.profiles())
        self.assertTrue(self.weights.exists())
        self.assertEqual(self.registry.load()["selected_chat"], "lfm3-vl")

    def test_select_infers_embedding_kind_and_can_disable_encoder(self):
        self.api(identifier="encoder", kind="embedding")
        with patch("folder_memory.model_settings.activate_models") as activate:
            code, _, error = self.run_cli("select", "encoder")
            self.assertEqual(code, 0, error)
            self.assertEqual(self.registry.load()["selected_embedding"], "encoder")
            self.assertEqual(activate.call_args.kwargs, {"start": False})
            code, _, error = self.run_cli("select", "none", "--kind", "embedding")
            self.assertEqual(code, 0, error)
            self.assertIsNone(self.registry.load()["selected_embedding"])

    def test_apply_uses_shared_restart_path_without_starting_chat(self):
        self.local()
        self.registry.select("local")
        with patch("folder_memory.model_settings.activate_models") as activate:
            code, _, error = self.run_cli("apply")
        self.assertEqual(code, 0, error)
        self.assertEqual(activate.call_args.kwargs, {"start": False, "force": True})

    def test_download_reports_progress_on_stderr_and_keeps_json_stdout_clean(self):
        def download(registry, identifier, callback):
            self.assertEqual(identifier, "spark")
            callback("model.gguf", 20, 100)
            callback("model.gguf", 100, 100)
        with patch.object(ModelRegistry, "download", autospec=True, side_effect=download):
            code, output, error = self.run_cli("download", "spark", "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["selected_chat"], "lfm3-vl")
        self.assertIn("model.gguf", error)

    def test_provider_discovery_and_test_use_shared_operations(self):
        self.provider()
        with patch("folder_memory.model_settings._request", return_value={"data": [{"id": "remote-model"}]}) as request:
            code, output, error = self.run_cli("providers", "models", "test-api", "--json")
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["models"], [{"id": "remote-model"}])
            self.assertEqual(request.call_args.args[1], "/v1/models")
            code, output, error = self.run_cli("providers", "test", "test-api", "--json")
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["count"], 1)
            self.assertEqual(request.call_args.args[1], "/v1/models")

    def test_model_test_uses_small_explicit_request(self):
        self.api()
        response = {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        with patch("folder_memory.model_settings._request", return_value=response) as request:
            code, output, error = self.run_cli("test", "test-chat")
        self.assertEqual(code, 0, error)
        self.assertEqual(request.call_args.args[1], "/v1/chat/completions")
        self.assertEqual(request.call_args.args[2]["max_tokens"], 16)
        self.assertEqual(output.strip(), "Подключено")

    def test_malformed_config_error_never_reflects_source(self):
        code, output, error = self.run_cli("add", "broken", "--config", "-", "--json",
                                          stdin=io.StringIO('{"api_key":"PASTED_PRIVATE_SECRET", bad'))
        self.assertEqual(code, 1)
        self.assertNotIn("PASTED_PRIVATE_SECRET", output + error)
        self.assertNotIn("Traceback", error)
        self.assertIn("JSON", json.loads(error)["error"])

    def test_literal_key_flag_is_not_accepted_or_echoed_in_parser_error(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            cli.main(["models", "providers", "add", "bad", "--api-key", "PASTED_PRIVATE_SECRET"])
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("PASTED_PRIVATE_SECRET", output.getvalue() + errors.getvalue())

    def test_unknown_profile_edit_does_not_create_it(self):
        code, _, error = self.run_cli("edit", "missing", "--name", "Anything")
        self.assertEqual(code, 1)
        self.assertNotIn("missing", self.registry.profiles())
        self.assertNotIn("Traceback", error)

    def test_oversized_and_non_object_configs_are_rejected_before_save(self):
        for value in ("[]", "{" + " " * (1024 * 1024) + "}", '{"context":NaN}'):
            with self.subTest(value=value[:30]):
                code, _, error = self.run_cli("add", "bad", "--config", "-", stdin=io.StringIO(value))
                self.assertEqual(code, 1)
                self.assertNotIn("bad", self.registry.profiles())
                self.assertNotIn("Traceback", error)

    def test_model_visibility_flags_preserve_hidden_setting_during_other_edits(self):
        code, _, error = self.run_cli("add", "hidden", "--model", str(self.weights), "--hidden")
        self.assertEqual(code, 0, error)
        self.assertFalse(self.registry.get("hidden")["visible"])
        code, _, error = self.run_cli("edit", "hidden", "--name", "Renamed hidden model")
        self.assertEqual(code, 0, error)
        self.assertFalse(self.registry.get("hidden")["visible"])
        code, _, error = self.run_cli("edit", "hidden", "--visible")
        self.assertEqual(code, 0, error)
        self.assertTrue(self.registry.get("hidden")["visible"])

    def test_visibility_flag_overrides_json_and_management_list_keeps_hidden_models(self):
        self.local(visible=False)
        code, output, error = self.run_cli("list", "--json")
        self.assertEqual(code, 0, error)
        hidden = next(item for item in json.loads(output)["profiles"] if item["id"] == "local")
        self.assertFalse(hidden["visible"])
        code, output, error = self.run_cli("list")
        self.assertEqual(code, 0, error)
        self.assertIn("local", output)
        self.assertIn("скрыта", output)
        code, _, error = self.run_cli("edit", "local", "--visible", "--config", "-",
                                      stdin=io.StringIO('{"visible":false}'))
        self.assertEqual(code, 0, error)
        self.assertTrue(self.registry.get("local")["visible"])

    def test_provider_visibility_applies_to_linked_models_and_management_keeps_provider(self):
        code, _, error = self.run_cli("providers", "add", "test-api", "--base-url", "http://127.0.0.1:12345/v1", "--hidden")
        self.assertEqual(code, 0, error)
        self.api()
        self.assertFalse(self.registry.providers()["test-api"]["visible"])
        self.assertNotIn("test-chat", self.registry.visible_profiles("chat"))
        code, _, error = self.run_cli("providers", "edit", "test-api", "--name", "Renamed hidden API")
        self.assertEqual(code, 0, error)
        self.assertFalse(self.registry.providers()["test-api"]["visible"])
        code, output, error = self.run_cli("providers", "list", "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["providers"][0]["id"], "test-api")
        self.assertFalse(json.loads(output)["providers"][0]["visible"])
        code, _, error = self.run_cli("providers", "edit", "test-api", "--visible")
        self.assertEqual(code, 0, error)
        self.assertIn("test-chat", self.registry.visible_profiles("chat"))

    def test_hidden_profile_can_still_be_explicitly_selected_by_id(self):
        self.local(visible=False)
        with patch("folder_memory.model_settings.activate_models"):
            code, _, error = self.run_cli("select", "local")
        self.assertEqual(code, 0, error)
        self.assertEqual(self.registry.load()["selected_chat"], "local")
        self.assertFalse(self.registry.get("local")["visible"])

    def test_provider_cli_checkbox_cascades_and_one_child_can_reactivate_it(self):
        self.api('first')
        self.api('second')
        self.api('encoder', kind='embedding')
        before = self.registry.load()['selected_chat']
        code, _, error = self.run_cli('providers', 'edit', 'Test API', '--hidden')
        self.assertEqual(code, 0, error)
        self.assertTrue(all(not self.registry.get(key)['visible'] for key in ('first', 'second', 'encoder')))
        code, _, error = self.run_cli('edit', 'first', '--visible')
        self.assertEqual(code, 0, error)
        self.assertTrue(self.registry.providers()['test-api']['visible'])
        self.assertTrue(self.registry.get('first')['visible'])
        self.assertFalse(self.registry.get('second')['visible'])
        self.assertFalse(self.registry.get('encoder')['visible'])
        self.assertEqual(self.registry.load()['selected_chat'], before)

    def test_visibility_flags_are_mutually_exclusive(self):
        for arguments in (("add", "model", "--visible", "--hidden"),
                          ("providers", "edit", "api", "--hidden", "--visible")):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                model_cli._parser().parse_args(arguments)
            self.assertEqual(raised.exception.code, 2)

    def test_json_errors_remain_machine_readable_when_terminal_colors_are_enabled(self):
        with patch.dict(os.environ, {"FORCE_COLOR": "1", "TERM": "xterm-256color"}):
            code, output, error = self.run_cli("edit", "missing", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("error", json.loads(error))
        self.assertNotIn("\x1b", error)


if __name__ == "__main__":
    unittest.main()
