"""Exercise explicit setup choices with tiny local files and inert API profiles."""

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rich.console import Console

from folder_memory.embeddings import Embedder
from folder_memory.model_registry import ModelRegistry
from folder_memory.model_setup import _index_ready, _offer_index, choose_profile, model_menu, prepare_models
from folder_memory.semantic import SemanticStore
from folder_memory.terminal_picker import PickerResult


class Answers:
    def __init__(self, *values):
        self.values = iter(values)
        self.calls = []

    def __call__(self, text, **options):
        self.calls.append((text, options))
        try:
            return next(self.values)
        except StopIteration:
            raise AssertionError("Setup asked an unexpected extra question: " + text) from None


class ModelSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "global-config"
        self.state = self.directory / "runtime-one"
        self.state.mkdir()
        environment = patch.dict(os.environ, {"HANDY_CONFIG_HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.binary = self.directory / "llama-server"
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.binary.chmod(0o700)
        discovered = patch("folder_memory.model_registry.shutil.which", return_value=str(self.binary))
        discovered.start()
        self.addCleanup(discovered.stop)
        network = patch("folder_memory.model_registry.urlopen", side_effect=AssertionError("Unexpected network request"))
        network.start()
        self.addCleanup(network.stop)
        download = patch.object(ModelRegistry, "download", side_effect=AssertionError("Unexpected model download"))
        self.download = download.start()
        self.addCleanup(download.stop)
        subprocesses = patch("folder_memory.model_setup.subprocess.run", side_effect=AssertionError("Unexpected index build"))
        subprocesses.start()
        self.addCleanup(subprocesses.stop)
        # Model setup chooses and publishes settings, but must not start a real
        # service in these fixture-only tests.
        for target in ("folder_memory.model_settings.activate_models", "folder_memory.model_console.activate_models"):
            activation = patch(target)
            activation.start()
            self.addCleanup(activation.stop)
        status = patch("folder_memory.model_settings.engine_status", return_value={"status": "stopped"})
        status.start()
        self.addCleanup(status.stop)
        self.output = io.StringIO()
        self.console = Console(file=self.output, width=120, color_system=None)
        self.registry = ModelRegistry(self.state)

    def browser(self, *steps):
        """Exercise the public picker boundary without opening a real terminal."""
        choices = iter(steps)

        def pick(title, source, **options):
            try:
                result = next(choices)
            except StopIteration:
                self.fail("Browser unexpectedly asked for another choice")
            if callable(result):
                return result(title, source, **options)
            if isinstance(result, str):
                result = PickerResult("select", "m:" + result, self.registry.get(result)["name"])
            if result.id and result.id.startswith("m:"):
                self.assertIn(result.id, [item.id for item in source(result.query)])
            return result

        return patch("folder_memory.model_console.pick", side_effect=pick)

    def weights(self, name="fixture.gguf", content=b"tiny fixture weights"):
        path = self.directory / name
        path.write_bytes(content)
        return path

    def ready_chat(self):
        self.registry.add("working", {"kind": "chat", "backend": "local", "enabled": True,
                                      "name": "Working fixture", "model": str(self.weights()),
                                      "binary": str(self.binary), "context": 4096})
        self.registry.select("working")
        self.registry.select(None, kind="embedding")

    def ready_encoder(self):
        self.registry.add("encoder", {"kind": "embedding", "backend": "local", "enabled": True,
                                      "name": "Fixture encoder", "model": str(self.weights("encoder.gguf")),
                                      "binary": str(self.binary), "context": 512, "dimensions": 2,
                                      "query_prefix": "", "passage_prefix": "", "pooling": "mean",
                                      "index_path": str(self.root / "indices" / "encoder.sqlite3")})
        self.registry.select("encoder", kind="embedding")
        return self.registry.get("encoder")

    def semantic_fixture(self, profile, *, indexed_profile=None):
        fingerprint = Embedder(SimpleNamespace(config=indexed_profile or profile)).fingerprint
        encoder = SimpleNamespace(fingerprint=fingerprint, enabled=True,
                                  encode=lambda texts: [[1.0, 0.0] for _ in texts])
        store = SemanticStore(profile["index_path"], encoder)
        store.upsert([{"id": "fixture", "kind": "reference", "text": "Fixture reference"}])
        return store

    def test_unconfigured_noninteractive_start_fails_without_downloading(self):
        with self.assertRaises(ValueError):
            prepare_models(self.state, interactive=False)
        self.download.assert_not_called()
        self.assertFalse((self.root / "models").exists())
        self.assertFalse((self.state / "neural.json").exists())

    def test_ready_chat_with_no_encoder_starts_without_prompts_or_downloads(self):
        self.ready_chat()
        prompt = Answers()
        config = prepare_models(self.state, interactive=True, console=self.console, prompt=prompt)
        self.assertEqual(prompt.calls, [])
        self.download.assert_not_called()
        self.assertEqual(config["name"], "Working fixture")
        self.assertEqual(config["embedding"], {"enabled": False})
        self.assertEqual(prepare_models(self.state, interactive=False), config)
        self.assertFalse((self.state / 'neural.json').exists())

    def test_explicit_browser_choice_enables_disabled_migrated_profiles(self):
        model, encoder = self.weights("legacy-chat.gguf"), self.weights("legacy-encoder.gguf")
        (self.state / "neural.json").write_text(json.dumps({
            "enabled": False, "name": "Legacy chat", "model": str(model), "binary": str(self.binary),
            "embedding": {"enabled": False, "name": "Legacy encoder", "model": str(encoder),
                          "binary": str(self.binary), "model_sha256": hashlib.sha256(encoder.read_bytes()).hexdigest()}}))
        data = self.registry.load()
        chat, embedding = data["selected_chat"], data["selected_embedding"]
        self.assertFalse(self.registry.get(chat)["enabled"])
        self.assertFalse(self.registry.get(embedding)["enabled"])
        with self.browser(chat):
            self.assertTrue(choose_profile(self.registry, "chat", self.console, Answers()))
        self.assertTrue(self.registry.get(chat)["enabled"])
        self.assertFalse(self.registry.get(embedding)["enabled"])
        with self.browser(embedding):
            self.assertTrue(choose_profile(self.registry, "embedding", self.console, Answers("нет")))
        self.assertTrue(self.registry.get(embedding)["enabled"])
        self.download.assert_not_called()

    def test_missing_optional_encoder_does_not_block_noninteractive_chat_or_download(self):
        self.ready_chat()
        self.registry.select("qwen3-embedding", kind="embedding")
        config = prepare_models(self.state, interactive=False)
        self.assertEqual(self.registry.load()["selected_embedding"], "qwen3-embedding")
        self.assertEqual(config['name'], 'Working fixture')
        self.download.assert_not_called()

    def test_initial_setup_can_choose_api_and_skip_optional_encoder_without_weights(self):
        self.registry.add("api-chat", {"kind": "chat", "backend": "openai", "name": "Fixture API",
                                        "base_url": "http://127.0.0.1:9999/v1", "model_id": "provider/chat",
                                        "api_key_env": "HANDY_SETUP_FIXTURE_KEY"})
        prompt = Answers()
        with patch.dict(os.environ, {"HANDY_SETUP_FIXTURE_KEY": "private-test-value"}), self.browser(
                "api-chat", PickerResult("disable")):
            config = prepare_models(self.state, interactive=True, console=self.console, prompt=prompt)
        self.assertEqual(self.registry.load()["selected_chat"], "api-chat")
        self.assertIsNone(self.registry.load()["selected_embedding"])
        self.assertEqual(config["model_id"], "provider/chat")
        self.assertEqual(config["api_key_env"], "HANDY_SETUP_FIXTURE_KEY")
        self.assertEqual(prompt.calls, [])
        self.assertNotIn("private-test-value", self.registry.path.read_text())
        self.assertNotIn("private-test-value", json.dumps(config))
        self.assertFalse((self.root / "models").exists())
        self.assertFalse((self.state / "neural.json").exists())
        self.download.assert_not_called()

    def test_declining_download_keeps_working_selection(self):
        self.ready_chat()
        with self.browser("spark", PickerResult("back")):
            self.assertFalse(choose_profile(self.registry, "chat", self.console, Answers("нет")))
        self.assertEqual(self.registry.load()["selected_chat"], "working")
        self.download.assert_not_called()

    def test_failed_download_keeps_selection_and_returns_to_browser(self):
        self.ready_chat()
        self.registry.materialize()
        before = (self.state / "neural.json").read_bytes()
        with patch.object(ModelRegistry, "download", side_effect=ValueError("Fixture download failed")) as download:
            with self.browser("spark", PickerResult("back")):
                self.assertFalse(choose_profile(self.registry, "chat", self.console, Answers("да")))
        self.assertEqual(download.call_count, 1)
        self.assertEqual(download.call_args.args[0], "spark")
        self.assertEqual(self.registry.load()["selected_chat"], "working")
        self.assertEqual((self.state / "neural.json").read_bytes(), before)
        self.assertIn("Fixture download failed", self.output.getvalue())

    def test_cancelled_download_preserves_selection_and_closes_browser(self):
        self.ready_chat()
        with patch.object(ModelRegistry, "download", side_effect=KeyboardInterrupt), self.browser("spark"):
            self.assertFalse(choose_profile(self.registry, "chat", self.console, Answers("да")))
        self.assertEqual(self.registry.load()["selected_chat"], "working")
        self.assertFalse((self.state / "neural.json").exists())

    def test_back_from_initial_setup_does_not_silently_choose_or_download(self):
        with self.browser(PickerResult("back")):
            with self.assertRaisesRegex(ValueError, "не выбрана"):
                prepare_models(self.state, interactive=True, console=self.console, prompt=Answers())
        self.download.assert_not_called()
        self.assertFalse((self.state / "neural.json").exists())

    def test_unavailable_api_key_env_does_not_replace_working_selection(self):
        self.ready_chat()
        self.registry.add("unready-api", {"kind": "chat", "backend": "openai", "name": "Unavailable API",
                                           "base_url": "http://127.0.0.1:9999/v1", "model_id": "provider/chat",
                                           "api_key_env": "HANDY_SETUP_MISSING_KEY"})
        with patch.dict(os.environ, {"HANDY_SETUP_MISSING_KEY": ""}), self.browser("unready-api", PickerResult("back")):
            self.assertFalse(choose_profile(self.registry, "chat", self.console, Answers()))
        self.assertEqual(self.registry.load()["selected_chat"], "working")
        self.assertFalse((self.state / "neural.json").exists())
        self.download.assert_not_called()

    def test_custom_gguf_can_be_selected_without_copying_or_downloading(self):
        self.ready_chat()
        source = self.weights("my custom chat.gguf")
        self.registry.add("custom-chat", {"kind": "chat", "backend": "local", "name": "Custom chat",
                                           "model": str(source), "binary": str(self.binary), "context": 8192})
        with self.browser("custom-chat"):
            self.assertTrue(choose_profile(self.registry, "chat", self.console, Answers()))
        profile = self.registry.get("custom-chat")
        self.assertEqual(Path(profile["model"]).resolve(), source.resolve())
        self.assertEqual(profile["context"], 8192)
        self.assertEqual(self.registry.load()["selected_chat"], "custom-chat")
        self.assertFalse((self.root / "models").exists())
        self.download.assert_not_called()

    def test_invalid_index_is_not_ready_and_is_not_modified(self):
        self.ready_chat()
        profile = self.ready_encoder()
        path = Path(profile["index_path"])
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not a database")
        self.assertFalse(_index_ready(profile))
        self.assertEqual(path.read_bytes(), b"not a database")

    def test_api_encoder_index_build_uses_its_config_and_preserves_prefix_spaces(self):
        self.ready_chat()
        self.registry.add("api-encoder", {"kind": "embedding", "backend": "openai", "name": "API encoder",
                                           "base_url": "http://127.0.0.1:9999/v1", "model_id": "provider/embed",
                                           "context": 512, "dimensions": 32, "query_prefix": "Question: ",
                                           "passage_prefix": "", "pooling": "mean"})
        self.registry.select("api-encoder", kind="embedding")
        observed = {}

        def build(arguments, **options):
            config_path = Path(arguments[arguments.index("--config") + 1])
            observed["config"] = json.loads(config_path.read_text())
            observed["mode"] = stat.S_IMODE(config_path.stat().st_mode)
            observed["output"] = arguments[arguments.index("--output") + 1]
            return subprocess.CompletedProcess(arguments, 0)

        with patch("folder_memory.model_setup.subprocess.run", side_effect=build) as builder:
            with self.browser("api-encoder"):
                self.assertTrue(choose_profile(self.registry, "embedding", self.console, Answers("да")))
        builder.assert_called_once()
        self.assertEqual(observed["config"]["embedding"]["model_id"], "provider/embed")
        self.assertEqual(observed["config"]["embedding"]["query_prefix"], "Question: ")
        self.assertEqual(observed["output"], str(self.root / "indices" / "api-encoder.sqlite3"))
        self.assertEqual(observed["mode"], 0o600)
        self.assertFalse((self.state / "neural.json").exists())
        self.download.assert_not_called()

    def test_reselecting_encoder_with_missing_index_reoffers_after_decline(self):
        self.ready_chat()
        profile = self.ready_encoder()
        for _ in range(2):
            prompt = Answers("нет")
            with self.browser("encoder"):
                self.assertTrue(choose_profile(self.registry, "embedding", self.console, prompt))
            self.assertEqual(len(prompt.calls), 1)
            self.assertEqual(self.registry.load()["selected_embedding"], "encoder")
        self.assertFalse(Path(profile["index_path"]).exists())
        self.download.assert_not_called()

    def test_index_fingerprint_includes_encoding_prefix_policy(self):
        self.ready_chat()
        profile = self.ready_encoder()
        changed = {**profile, "query_prefix": "Changed encoding policy: "}
        self.semantic_fixture(profile, indexed_profile=changed)
        self.assertTrue(_index_ready(changed))
        self.assertFalse(_index_ready(profile))
        before = Path(profile["index_path"]).read_bytes()
        prompt = Answers("нет")
        with self.browser("encoder"):
            self.assertTrue(choose_profile(self.registry, "embedding", self.console, prompt))
        self.assertEqual(len(prompt.calls), 1)
        self.assertEqual(Path(profile["index_path"]).read_bytes(), before)

    def test_valid_matching_index_does_not_prompt_to_rebuild(self):
        self.ready_chat()
        profile = self.ready_encoder()
        self.semantic_fixture(profile)
        self.assertTrue(_index_ready(profile))
        prompt = Answers()
        with self.browser("encoder"):
            self.assertTrue(choose_profile(self.registry, "embedding", self.console, prompt))
        self.assertEqual(prompt.calls, [])
        self.download.assert_not_called()

    def test_explicit_index_action_still_offers_rebuild_for_matching_index(self):
        self.ready_chat()
        profile = self.ready_encoder()
        self.semantic_fixture(profile)
        before = Path(profile["index_path"]).read_bytes()
        prompt = Answers("нет")
        with self.browser(PickerResult("index", "m:encoder", "Fixture encoder"), PickerResult("back")):
            model_menu(self.state, self.console, prompt, kind="embedding")
        self.assertEqual(len(prompt.calls), 1)
        self.assertEqual(Path(profile["index_path"]).read_bytes(), before)
        self.download.assert_not_called()

    def test_shared_provider_selection_resolves_same_configuration_without_publishing(self):
        self.ready_chat()
        profile = {"kind": "chat", "backend": "openai", "name": "Shared provider", "enabled": True,
                   "base_url": "http://127.0.0.1:9999/v1", "model_id": "provider/chat",
                   "api_key_env": "HANDY_SETUP_FIXTURE_KEY"}
        self.registry.add("shared-api", profile)
        self.registry.select("shared-api")
        other_state = self.directory / "runtime-two"
        other_state.mkdir()
        (other_state / "neural.json").write_text(json.dumps({"name": "Old workspace model", "enabled": False}))
        with patch.dict(os.environ, {"HANDY_SETUP_FIXTURE_KEY": "fixture-env-value"}):
            first = prepare_models(self.state, interactive=False)
            second = prepare_models(other_state, interactive=False)
        self.assertEqual(first["backend"], "openai")
        self.assertEqual(first["model_id"], "provider/chat")
        self.assertEqual(first, second)
        self.assertEqual(json.loads((other_state / 'neural.json').read_text())['name'], 'Old workspace model')
        self.assertEqual(ModelRegistry(other_state).load()["selected_chat"], "shared-api")
        self.download.assert_not_called()

    def test_search_hides_profiles_and_hidden_provider_models(self):
        self.ready_chat()
        self.registry.add("private-model", {"kind": "chat", "backend": "local", "name": "Private model",
                                            "model": str(self.weights("private.gguf")), "binary": str(self.binary), "visible": False})
        self.registry.save_provider("private-api", {"name": "Private API", "base_url": "http://127.0.0.1:9999/v1", "visible": False})
        self.registry.add("private-api-model", {"kind": "chat", "backend": "openai", "name": "Private API model",
                                                "provider_id": "private-api", "model_id": "chat", "context": 4096})

        def inspect(title, source, **options):
            self.assertEqual(source("Private"), [])
            self.assertEqual([item.id for item in source("Working")], ["m:working"])
            self.assertNotIn("p:private-api", [item.id for item in source("")])
            return PickerResult("back")

        with self.browser(inspect):
            self.assertFalse(choose_profile(self.registry, "chat", self.console, Answers()))
        self.assertEqual(self.registry.load()["selected_chat"], "working")

    def test_hidden_selected_model_remains_named_and_keeps_working(self):
        self.ready_chat()
        self.registry.update("working", {**self.registry.get("working"), "visible": False})

        def inspect(title, source, **options):
            self.assertIn("Working fixture", title)
            self.assertEqual(source("Working"), [])
            return PickerResult("back")

        with self.browser(inspect):
            self.assertFalse(model_menu(self.state, self.console, Answers()))
        self.assertEqual(prepare_models(self.state, interactive=False)["name"], "Working fixture")

    def test_embedding_browser_can_disable_hidden_selected_encoder(self):
        self.ready_chat()
        self.ready_encoder()
        self.registry.update("encoder", {**self.registry.get("encoder"), "visible": False})

        def inspect(title, source, **options):
            self.assertEqual(source("Fixture encoder"), [])
            self.assertIn("disable", [action.id for action in options["actions"]])
            return PickerResult("disable")

        with self.browser(inspect):
            self.assertTrue(choose_profile(self.registry, "embedding", self.console, Answers()))
        self.assertIsNone(self.registry.load()["selected_embedding"])

    def test_hidden_local_group_still_runs_selected_model_and_keeps_api_choices(self):
        self.ready_chat()
        data = self.registry.load()
        data["standard_provider_visible"] = False
        self.registry.path.write_text(json.dumps(data))
        self.registry.add("api-choice", {"kind": "chat", "backend": "openai", "enabled": True,
                                         "name": "Visible API", "base_url": "http://127.0.0.1:9999/v1", "model_id": "chat"})

        def inspect(title, source, **options):
            self.assertNotIn("p:builtin-standard", [item.id for item in source("")])
            self.assertEqual(source("Working"), [])
            self.assertIn("m:api-choice", [item.id for item in source("Visible API")])
            return PickerResult("back")

        with self.browser(inspect):
            self.assertFalse(choose_profile(self.registry, "chat", self.console, Answers()))
        self.assertEqual(prepare_models(self.state, interactive=False)["name"], "Working fixture")


if __name__ == "__main__":
    unittest.main()
