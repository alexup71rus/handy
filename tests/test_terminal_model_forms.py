"""Interactive editors persist once, preserve unrelated fields, and hide keys."""

import io
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch

from rich.console import Console

from folder_memory.model_settings import ModelSettings
from folder_memory.terminal_model_forms import model_form, provider_form
from folder_memory.terminal_picker import PickerResult


class Answers:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.calls = []

    def __call__(self, label, **options):
        self.calls.append((label, options))
        try:
            answer = next(self.answers)
        except StopIteration:
            raise AssertionError("Unexpected extra form question: " + label) from None
        if isinstance(answer, BaseException):
            raise answer
        return answer


class TerminalModelFormsTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.directory / "config"
        self.state = self.directory / "state"
        self.enterContext(patch.dict(os.environ, {"HANDY_CONFIG_HOME": str(self.root)}))
        self.binary = self.directory / "llama-server"
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.binary.chmod(0o700)
        self.enterContext(patch("folder_memory.model_registry.shutil.which", return_value=str(self.binary)))
        self.enterContext(patch("folder_memory.model_settings.engine_status", return_value={"status": "stopped"}))
        self.enterContext(patch("folder_memory.model_settings.activate_models", side_effect=AssertionError("No activation from a form")))
        self.enterContext(patch("folder_memory.remote_model.OpenAIModel._request", side_effect=AssertionError("No network from a form")))
        self.manager = ModelSettings(self.state)
        self.output = io.StringIO()
        self.console = Console(file=self.output, color_system=None, width=100)

    def provider(self, identifier="fixture", **changes):
        request = {"operation": "save_provider", "id": identifier,
                   "provider": {"name": "Fixture", "base_url": "https://provider.example/v1", "auth": "none", **changes}}
        if request["provider"]["auth"] == "key":
            request["api_key"] = "TEST_EXISTING_SECRET"
        self.manager.perform(request)
        return identifier

    def api_model(self, identifier="existing", **changes):
        self.provider()
        self.manager.perform({"operation": "save_profile", "id": identifier,
                              "profile": {"kind": "chat", "backend": "openai", "name": "Original",
                                          "provider_id": "fixture", "model_id": "Vendor/Exact-ID", "context": 65536, **changes}})
        return identifier

    def weights(self, name="tiny model.gguf"):
        path = self.directory / name
        path.write_bytes(b"GGUF" + struct.pack("<I", 3) + b"fixture")
        return path

    def test_new_provider_secret_uses_hidden_prompt_and_private_backend_file(self):
        answers = Answers("https://provider.example/v1", "My provider", "ключ", "TEST_NEW_SECRET", "да")
        identifier = provider_form(self.manager, self.console, answers)
        self.assertTrue(identifier.startswith("provider-"))
        provider = self.manager.registry.providers()[identifier]
        key = Path(provider["api_key_file"])
        self.assertEqual(key.read_text(), "TEST_NEW_SECRET")
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        secret_calls = [options for _, options in answers.calls if options.get("is_password")]
        self.assertEqual(secret_calls, [{"cancel_on_escape": True, "is_password": True}])
        self.assertNotIn("TEST_NEW_SECRET", self.output.getvalue())
        self.assertNotIn("TEST_NEW_SECRET", self.manager.registry.path.read_text())
        self.assertFalse((self.state / "neural.json").exists())

    def test_an_empty_new_key_creates_anonymous_local_connection(self):
        identifier = provider_form(self.manager, self.console, Answers("http://127.0.0.1:11434/v1", "", "", "", ""))
        provider = self.manager.registry.providers()[identifier]
        self.assertEqual(provider["name"], "127.0.0.1:11434")
        self.assertNotIn("api_key_file", provider)
        self.assertFalse((self.root / "credentials").exists())

    def test_provider_edit_preserves_stored_key_and_hidden_flag_without_asking_for_key(self):
        self.provider(auth="key", visible=False)
        previous = self.manager.registry.providers()["fixture"]
        answers = Answers("", "Renamed", "", "")
        result = provider_form(self.manager, self.console, answers, identifier="fixture")
        provider = self.manager.registry.providers()[result]
        self.assertEqual(provider["name"], "Renamed")
        self.assertEqual(provider["api_key_file"], previous["api_key_file"])
        self.assertFalse(provider["visible"])
        self.assertFalse(any(options.get("is_password") for _, options in answers.calls))

    def test_blank_replacement_key_keeps_existing_file_and_content(self):
        self.provider(auth="key")
        previous = self.manager.registry.providers()["fixture"]["api_key_file"]
        provider_form(self.manager, self.console, Answers("", "", "ключ", "", ""), identifier="fixture")
        self.assertEqual(self.manager.registry.providers()["fixture"]["api_key_file"], previous)
        self.assertEqual(Path(previous).read_text(), "TEST_EXISTING_SECRET")
        self.assertEqual(len(list((self.root / "credentials").glob("*.key"))), 1)

    def test_auth_none_explicitly_clears_reference_but_keeps_active_runtime_key_file(self):
        self.provider(auth="key")
        previous = self.manager.registry.providers()["fixture"]["api_key_file"]
        provider_form(self.manager, self.console, Answers("", "", "нет", ""), identifier="fixture")
        self.assertNotIn("api_key_file", self.manager.registry.providers()["fixture"])
        self.assertTrue(Path(previous).exists())

    def test_provider_cancel_after_entering_key_writes_nothing_even_on_fresh_config(self):
        for cancellation in (KeyboardInterrupt(), EOFError(), "/cancel", "нет"):
            with self.subTest(cancellation=type(cancellation).__name__):
                result = provider_form(self.manager, self.console,
                                       Answers("https://provider.example/v1", "API", "ключ", "TEST_CANCELLED_KEY", cancellation))
                self.assertIsNone(result)
                self.assertFalse(self.root.exists())

    def test_invalid_provider_key_fails_before_creating_any_global_files(self):
        with self.assertRaisesRegex(ValueError, "ключ"):
            provider_form(self.manager, self.console, Answers("https://provider.example/v1", "API", "ключ", "two words", ""))
        self.assertFalse(self.root.exists())

    def test_key_file_symlink_is_rejected_instead_of_silently_resolving_to_target(self):
        target = self.directory / "actual-key"
        target.write_text("TEST_FILE_KEY")
        target.chmod(0o600)
        link = self.directory / "symlink-key"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            provider_form(self.manager, self.console,
                          Answers("https://provider.example/v1", "API", "файл", str(link), ""))
        self.assertFalse(self.root.exists())

    def test_remote_model_has_one_exact_provider_id_and_auto_profile_identifier(self):
        self.provider()
        answers = Answers("Vendor/GPT.Exact-V1", "", "", "нет", "да")
        identifier = model_form(self.manager, self.console, answers, provider_id="fixture", backend="openai")
        value = self.manager.registry.get(identifier)
        self.assertTrue(identifier.startswith("api-"))
        self.assertEqual(value["model_id"], "Vendor/GPT.Exact-V1")
        self.assertEqual(value["name"], "Vendor/GPT.Exact-V1")
        self.assertEqual(value["provider_id"], "fixture")
        self.assertEqual(value["context"], 65536)
        self.assertFalse(any("профиля" in label.casefold() for label, _ in answers.calls))
        self.assertFalse((self.state / "neural.json").exists())

    def test_remote_model_chooses_provider_with_shared_searchable_picker(self):
        self.provider()
        self.provider("second", name="Another API", base_url="http://127.0.0.1:11434/v1")
        answers = Answers("Vendor/Exact-ID", "", "", "нет", "да")
        with patch("folder_memory.terminal_model_forms.pick", return_value=PickerResult("select", "second")) as picker:
            identifier = model_form(self.manager, self.console, answers, backend="openai")
        self.assertEqual(self.manager.registry.get(identifier)["provider_id"], "second")
        self.assertEqual(picker.call_args.args[0], "Провайдер")
        items = picker.call_args.args[1]
        self.assertEqual([(item.id, item.title, item.detail) for item in items], [
            ("second", "Another API", "http://127.0.0.1:11434/v1"),
            ("fixture", "Fixture", "https://provider.example/v1"),
        ])
        self.assertFalse(any("Провайдер:" in label for label, _ in answers.calls))

    def test_cancelling_provider_picker_leaves_model_and_registry_unchanged(self):
        self.provider()
        self.provider("second", name="Another API")
        previous = self.manager.registry.path.read_bytes()
        for action in ("back", "cancel"):
            with self.subTest(action=action), patch("folder_memory.terminal_model_forms.pick", return_value=PickerResult(action, "second")):
                answers = Answers()
                self.assertIsNone(model_form(self.manager, self.console, answers, backend="openai"))
                self.assertEqual(answers.calls, [])
                self.assertEqual(self.manager.registry.path.read_bytes(), previous)

    def test_single_provider_can_be_used_without_picker(self):
        self.provider()
        with patch("folder_memory.terminal_model_forms.pick") as picker:
            identifier = model_form(self.manager, self.console, Answers("Vendor/Exact-ID", "", "", "нет", "да"), backend="openai")
        picker.assert_not_called()
        self.assertEqual(self.manager.registry.get(identifier)["provider_id"], "fixture")

    def test_remote_edit_keeps_sampling_unknown_settings_visibility_and_disabled_state(self):
        identifier = self.api_model(sampling={"temperature": .7, "top_p": .8}, visible=False, enabled=False,
                                    future_setting={"nested": [1, 2]})
        model_form(self.manager, self.console, Answers("", "Renamed", "32768", "", ""), identifier=identifier)
        value = self.manager.registry.get(identifier)
        self.assertEqual(value["name"], "Renamed")
        self.assertEqual(value["context"], 32768)
        self.assertEqual(value["model_id"], "Vendor/Exact-ID")
        self.assertEqual(value["sampling"], {"temperature": .7, "top_p": .8})
        self.assertEqual(value["future_setting"], {"nested": [1, 2]})
        self.assertFalse(value["visible"])
        self.assertFalse(value["enabled"])

    def test_long_provider_model_id_keeps_exact_id_but_bounded_default_display_name(self):
        self.provider()
        remote_id = "Vendor/" + "Exact" * 60
        identifier = model_form(self.manager, self.console, Answers(remote_id, "", "", "", ""),
                                backend="openai", provider_id="fixture")
        profile = self.manager.registry.get(identifier)
        self.assertEqual(profile["model_id"], remote_id)
        self.assertEqual(profile["name"], remote_id[:200])

    def test_cancel_model_edit_does_not_touch_saved_bytes(self):
        identifier = self.api_model()
        previous = self.manager.registry.path.read_bytes()
        self.assertIsNone(model_form(self.manager, self.console, Answers("Other/ID", "Changed", "8192", "нет", EOFError()), identifier=identifier))
        self.assertEqual(self.manager.registry.path.read_bytes(), previous)

    def test_local_model_path_with_spaces_and_discovered_runtime(self):
        path = self.weights()
        identifier = model_form(self.manager, self.console, Answers(str(path), "", "", "32768", "", ""), backend="local")
        profile = self.manager.registry.get(identifier)
        self.assertEqual(profile["model"], str(path))
        self.assertEqual(profile["name"], "tiny model")
        self.assertEqual(profile["binary"], str(self.binary))
        self.assertEqual(profile["context"], 32768)
        self.assertNotIn("mmproj", profile)

    def test_uninstalled_standard_can_change_name_context_without_download_or_file_check_failure(self):
        self.manager.registry.load()
        previous = self.manager.registry.get("lfm3-vl")
        self.assertFalse(Path(previous["model"]).exists())
        model_form(self.manager, self.console, Answers("", "", "Short local context", "32768", "", ""), identifier="lfm3-vl")
        profile = self.manager.registry.get("lfm3-vl")
        self.assertEqual(profile["model"], previous["model"])
        self.assertEqual(profile["download"], previous["download"])
        self.assertEqual(profile["sampling"], previous["sampling"])
        self.assertEqual(profile["context"], 32768)

    def test_local_cancel_does_not_create_fresh_registry(self):
        result = model_form(self.manager, self.console, Answers("/cancel"), backend="local")
        self.assertIsNone(result)
        self.assertFalse(self.root.exists())

    def test_existing_encoder_keeps_exact_prefixes_pooling_and_custom_index(self):
        path = self.weights("encoder.gguf")
        index = str(self.directory / "my-index.sqlite3")
        self.manager.perform({"operation": "save_profile", "id": "encoder", "profile": {
            "kind": "embedding", "backend": "local", "name": "Encoder", "model": str(path),
            "binary": str(self.binary), "context": 512, "dimensions": 2,
            "query_prefix": "query: \n ", "passage_prefix": "doc: ", "pooling": "last", "index_path": index}})
        model_form(self.manager, self.console, Answers("", "New encoder label", "", "нет", "нет", ""), identifier="encoder")
        profile = self.manager.registry.get("encoder")
        self.assertEqual(profile["name"], "New encoder label")
        self.assertEqual(profile["query_prefix"], "query: \n ")
        self.assertEqual(profile["passage_prefix"], "doc: ")
        self.assertEqual(profile["pooling"], "last")
        self.assertEqual(profile["index_path"], index)

    def test_new_api_encoder_keeps_prefix_whitespace_and_dimension(self):
        self.provider()
        identifier = model_form(self.manager, self.console,
                                Answers("Embedding/Exact", "", "", "2", "query: \n ", "doc: ", "нет", "нет", ""),
                                provider_id="fixture", backend="openai", kind="embedding")
        profile = self.manager.registry.get(identifier)
        self.assertEqual(profile["model_id"], "Embedding/Exact")
        self.assertEqual(profile["dimensions"], 2)
        self.assertEqual(profile["query_prefix"], "query: \n ")
        self.assertEqual(profile["passage_prefix"], "doc: ")

    def test_advanced_edit_can_remove_override_without_resetting_other_options(self):
        identifier = self.api_model(sampling={"temperature": .7, "top_p": .8}, future_setting=42)
        model_form(self.manager, self.console,
                   Answers("", "", "", "да", "/default", "0.9", "512", "нет", ""), identifier=identifier)
        profile = self.manager.registry.get(identifier)
        self.assertEqual(profile["sampling"], {"top_p": .9})
        self.assertEqual(profile["max_output_tokens"], 512)
        self.assertFalse(profile["visible"])
        self.assertEqual(profile["future_setting"], 42)

    def test_large_integer_context_is_a_validation_error_without_overflow_or_write(self):
        self.provider()
        previous = self.manager.registry.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "диапазон"):
            model_form(self.manager, self.console, Answers("Exact", "", "9" * 400), backend="openai", provider_id="fixture")
        self.assertEqual(self.manager.registry.path.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
