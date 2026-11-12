"""Global profile migration and downloads without any real network/model call."""

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from folder_memory.filesystem_tools import read_file
from folder_memory.model_registry import ModelRegistry, config_root, file_sha256


class ModelRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state = self.directory / "runtime"
        self.state.mkdir()
        self.root = self.directory / "private-config"
        environment = patch.dict(os.environ, {"HANDY_CONFIG_HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.binary = self.directory / "llama-server"
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.binary.chmod(0o700)
        discovery = patch("folder_memory.model_registry.shutil.which", return_value=str(self.binary))
        discovery.start()
        self.addCleanup(discovery.stop)
        network = patch("folder_memory.model_registry.urlopen", side_effect=AssertionError("Unexpected network request"))
        network.start()
        self.addCleanup(network.stop)
        self.registry = ModelRegistry(self.state)

    def legacy(self, value):
        (self.state / "neural.json").write_text(json.dumps(value))

    def weights(self, name, content=b"fixture weights"):
        path = self.directory / name
        path.write_bytes(content)
        return path

    def downloadable(self, model_id="fixture", *, files=None):
        contents = b"verified fixture weights"
        files = files or [{"field": "model", "filename": "fixture.gguf", "bytes": len(contents),
                           "sha256": hashlib.sha256(contents).hexdigest()}]
        profile = {"kind": "chat", "backend": "local", "name": "Fixture", "enabled": True,
                   "binary": str(self.binary), "model": str(self.root / "models" / files[0]["filename"]),
                   "download": {"repo": "Official/Fixture", "revision": "a" * 40, "files": files}}
        self.registry.add(model_id, profile)
        return contents

    def test_environment_override_is_exact_and_default_uses_xdg(self):
        self.assertEqual(config_root(), self.root)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.directory / "xdg")}, clear=True):
            self.assertEqual(config_root(), self.directory / "xdg" / "handy")

    def test_global_directory_rejects_cwd_dependent_or_filesystem_root_overrides(self):
        for name, value in (('HANDY_CONFIG_HOME', ''), ('HANDY_CONFIG_HOME', 'config'),
                            ('XDG_CONFIG_HOME', ''), ('XDG_CONFIG_HOME', 'config'),
                            ('HANDY_CONFIG_HOME', '/'), ('HANDY_CONFIG_HOME', '/usr/..')):
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}, clear=True):
                with self.assertRaises(ValueError):
                    ModelRegistry(self.state).load()
        self.assertFalse((self.directory / 'models.json').exists())

    def test_initial_catalogue_is_private_and_does_not_download(self):
        data = self.registry.load()
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["selected_chat"], "lfm3-vl")
        self.assertEqual(data["selected_embedding"], "qwen3-embedding")
        self.assertEqual(set(data["profiles"]), {"lfm3-vl", "spark", "qwen3-embedding"})
        self.assertEqual(self.registry.path, self.root / "models.json")
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.registry.path.stat().st_mode), 0o600)
        self.assertFalse((self.root / "models").exists())
        for profile in data["profiles"].values():
            self.assertEqual(profile["binary"], str(self.binary))
            self.assertTrue(Path(profile["model"]).is_relative_to(self.root / "models"))
        self.assertFalse(self.registry.ready("lfm3-vl"))

    def test_migration_reuses_existing_weights_and_preserves_unknown_fields(self):
        model = self.weights("LFM3-VL-2B-Q6_K.gguf")
        projector = self.weights("mmproj-LFM3-VL-2B-F16.gguf")
        encoder = self.weights("Qwen3-Embedding-0.6B-Q8_0.gguf")
        original = {"enabled": True, "name": "My vision model", "model": str(model),
                    "mmproj": str(projector), "binary": str(self.binary), "context": 12345,
                    "custom_future_setting": {"keep": [1, 2]},
                    "embedding": {"enabled": False, "model": str(encoder), "binary": str(self.binary),
                                  "query_prefix": "Project prefix: ", "other_setting": 42}}
        self.legacy(original)
        data = self.registry.load()
        self.assertEqual(data["selected_chat"], "lfm3-vl")
        self.assertEqual(data["selected_embedding"], "qwen3-embedding")
        resolved = self.registry.resolved()
        for key in original:
            if key != "embedding":
                self.assertEqual(resolved[key], original[key])
        for key, value in original["embedding"].items():
            self.assertEqual(resolved["embedding"][key], value)
        self.assertTrue(self.registry.ready("lfm3-vl"))
        self.assertTrue(self.registry.ready("qwen3-embedding"))
        self.assertEqual(json.loads((self.state / "neural.json").read_text()), original)

    def test_custom_model_is_imported_instead_of_relabelled(self):
        custom = self.weights("my-custom-model.gguf")
        self.legacy({"enabled": True, "name": "Custom", "binary": str(self.binary),
                     "model": str(custom), "unknown_option": 7})
        data = self.registry.load()
        self.assertEqual(data["selected_chat"], "imported")
        self.assertEqual(self.registry.get("imported")["model"], str(custom))
        self.assertEqual(self.registry.get("imported")["unknown_option"], 7)

    def test_explicit_selection_enables_a_migrated_disabled_profile_even_when_already_selected(self):
        model = self.weights("custom.gguf")
        encoder = self.weights("custom-encoder.gguf")
        self.legacy({"enabled": False, "name": "Custom", "binary": str(self.binary), "model": str(model),
                     "embedding": {"enabled": False, "name": "Encoder", "binary": str(self.binary),
                                   "model": str(encoder)}})
        before = self.registry.load()
        self.assertFalse(before["profiles"][before["selected_chat"]]["enabled"])
        self.assertFalse(before["profiles"][before["selected_embedding"]]["enabled"])
        self.registry.select(before["selected_chat"])
        self.assertTrue(self.registry.get(before["selected_chat"])["enabled"])
        self.assertFalse(self.registry.get(before["selected_embedding"])["enabled"])
        self.registry.select(before["selected_embedding"], kind="embedding")
        self.assertTrue(self.registry.get(before["selected_embedding"])["enabled"])
        self.registry.select(None, kind="embedding")
        self.assertEqual(self.registry.resolved()["embedding"], {"enabled": False})

    def test_first_migration_is_not_repeated_from_another_runtime(self):
        self.registry.select("spark")
        other_state = self.directory / "other-runtime"
        other_state.mkdir()
        (other_state / "neural.json").write_text(json.dumps({"name": "Unrelated", "model": "somewhere"}))
        other = ModelRegistry(other_state)
        self.assertEqual(other.load()["selected_chat"], "spark")
        self.assertNotIn("imported", other.profiles())

    def test_selection_validates_kind_and_materialization_only_writes_changes(self):
        self.registry.select("spark")
        with self.assertRaises(ValueError):
            self.registry.select("qwen3-embedding")
        with self.assertRaises(ValueError):
            self.registry.select("missing")
        self.assertEqual(self.registry.load()["selected_chat"], "spark")
        self.assertTrue(self.registry.materialize())
        path = self.state / "neural.json"
        stamp = path.stat().st_mtime_ns
        self.assertFalse(self.registry.materialize())
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        config = json.loads(path.read_text())
        self.assertEqual(config["name"], "Spark-X2.6-4B-Q8_0")
        self.assertNotIn("kind", config)
        self.assertNotIn("download", config)
        self.assertEqual(config["embedding"]["name"], "Qwen3-Embedding-0.6B-Q8_0")
        self.registry.select(None, kind="embedding")
        self.registry.materialize()
        self.assertEqual(json.loads(path.read_text())["embedding"], {"enabled": False})

    def test_update_preserves_selection_and_validates_replacement(self):
        self.registry.select("spark")
        original = self.registry.get("spark")
        modified = {**original, "context": 16384, "custom_option": "retained"}
        self.assertEqual(self.registry.update("spark", modified), modified)
        self.assertEqual(self.registry.load()["selected_chat"], "spark")
        self.assertEqual(self.registry.get("spark")["context"], 16384)
        with self.assertRaises(ValueError):
            self.registry.update("spark", {**modified, "kind": "embedding"})
        self.assertEqual(self.registry.get("spark"), modified)
        with self.assertRaises(ValueError):
            self.registry.update("absent", modified)
        with self.assertRaises(ValueError):
            self.registry.add("spark", modified)

    def test_binary_installed_after_first_setup_is_discovered_without_changing_profile(self):
        weights = self.weights("standalone.gguf")
        self.registry.add("local", {"kind": "chat", "model": str(weights), "binary": ""})
        self.registry.select("local")
        self.assertTrue(self.registry.ready("local"))
        self.assertEqual(self.registry.resolved()["binary"], str(self.binary))
        self.assertEqual(self.registry.get("local")["binary"], "")

    def test_load_returns_copies_and_parallel_adds_do_not_lose_profiles(self):
        data = self.registry.load()
        data["profiles"].clear()
        self.assertEqual(len(self.registry.profiles()), 3)
        errors = []

        def add(model_id):
            try:
                ModelRegistry(self.state).add(model_id, {"kind": "chat", "name": model_id})
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=add, args=(name,)) for name in ("first", "second")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        self.assertEqual(errors, [])
        self.assertTrue({"first", "second"}.issubset(self.registry.profiles("chat")))

    def test_readiness_checks_projector_and_executable_without_network(self):
        weights = self.weights("standalone.gguf")
        self.registry.add("local", {"kind": "chat", "model": str(weights), "binary": str(self.binary)})
        self.assertTrue(self.registry.ready("local"))
        self.registry.add("vision", {"kind": "chat", "model": str(weights), "binary": str(self.binary),
                                     "mmproj": str(self.directory / "missing.gguf")})
        self.assertFalse(self.registry.ready("vision"))
        self.binary.chmod(0o600)
        self.assertFalse(self.registry.ready("local"))

    def test_remote_credentials_are_references_and_readiness_is_local(self):
        profile = {"kind": "chat", "backend": "openai", "base_url": "https://provider.example/v1",
                   "model_id": "vendor/model", "api_key_env": "HANDY_TEST_PROVIDER_KEY"}
        self.registry.add("provider", profile)
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(self.registry.ready("provider"))
        with patch.dict(os.environ, {"HANDY_TEST_PROVIDER_KEY": "not-printed-secret"}):
            self.assertTrue(self.registry.ready("provider"))
        self.assertNotIn("not-printed-secret", self.registry.path.read_text())
        self.registry.select("provider")
        self.assertEqual(self.registry.resolved()["model_id"], "vendor/model")
        for secret_key in ("api_key", "Authorization", "access_token"):
            with self.assertRaises(ValueError):
                self.registry.add("bad", {**profile, secret_key: "secret"})
        with self.assertRaises(ValueError):
            self.registry.add("bad", {**profile, "api_key_file": "/tmp/key"})

    def test_private_key_file_can_be_relative_to_runtime(self):
        key = self.state / "provider-key"
        key.write_text("fixture secret\n")
        key.chmod(0o600)
        self.registry.add("provider", {"kind": "chat", "backend": "openai", "model_id": "test",
                                      "base_url": "http://127.0.0.1:9999/v1", "api_key_file": "provider-key"})
        self.assertEqual(self.registry.get("provider")["api_key_file"], str(key.resolve()))
        other = ModelRegistry(self.directory / "other-runtime")
        self.assertTrue(other.ready("provider"))
        self.assertTrue(self.registry.ready("provider"))
        with self.assertRaisesRegex(ValueError, "секрет"):
            read_file(self.directory, path=str(key))
        key.chmod(0o644)
        self.assertFalse(self.registry.ready("provider"))
        key.chmod(0o600)
        source = key.with_name("actual-provider-key")
        key.rename(source)
        key.symlink_to(source)
        self.assertFalse(self.registry.ready("provider"))

    def test_legacy_api_key_files_keep_their_original_runtime_after_migration(self):
        key = self.state / "legacy-provider-access"
        key.write_text("fixture-only-legacy-key")
        key.chmod(0o600)
        self.legacy({"backend": "openai", "model_id": "test", "base_url": "http://127.0.0.1:9999/v1",
                     "api_key_file": key.name, "embedding": {"enabled": False}})
        data = self.registry.load()
        profile = data["profiles"][data["selected_chat"]]
        self.assertEqual(profile["api_key_file"], str(key.resolve()))
        other = ModelRegistry(self.directory / "other-runtime")
        self.assertTrue(other.ready(data["selected_chat"]))
        with self.assertRaisesRegex(ValueError, "секрет"):
            read_file(self.directory, path=str(key))

    def test_hand_edited_registry_rejects_runtime_dependent_secret_references(self):
        key = self.state / 'provider-secret'
        key.write_text('FAKE_PRIVATE_KEY')
        key.chmod(0o600)
        self.registry.add('api', {'kind': 'chat', 'backend': 'openai', 'model_id': 'fixture',
                                 'base_url': 'http://127.0.0.1:9999/v1', 'api_key_file': str(key)})
        data = self.registry.load()
        data['profiles']['api']['api_key_file'] = key.name
        self.registry.path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'абсолютный'):
            self.registry.ready('api')

    def test_arbitrary_embedding_weights_get_their_real_hash(self):
        weights = self.weights("my-encoder.gguf", b"an arbitrary encoder")
        self.registry.add("encoder", {"kind": "embedding", "binary": str(self.binary), "model": str(weights)})
        self.assertEqual(self.registry.get("encoder")["model_sha256"], hashlib.sha256(weights.read_bytes()).hexdigest())
        self.assertEqual(file_sha256(weights), self.registry.get("encoder")["model_sha256"])
        self.registry.select("encoder", kind="embedding")
        self.assertTrue(self.registry.resolved()["embedding"]["embedding"])

    def test_explicit_download_verifies_and_publishes_atomically(self):
        contents = self.downloadable()
        before = self.registry.load()["selected_chat"]
        progress = []
        with patch("folder_memory.model_registry._open_url", side_effect=lambda *args: io.BytesIO(contents)) as request:
            profile = self.registry.download("fixture", lambda *args: progress.append(args))
        target = self.root / "models" / "fixture.gguf"
        self.assertEqual(target.read_bytes(), contents)
        self.assertEqual(profile["model"], str(target))
        self.assertEqual(request.call_count, 1)
        self.assertTrue(progress)
        self.assertEqual(progress[-1], ("fixture.gguf", len(contents), len(contents)))
        self.assertEqual(self.registry.load()["selected_chat"], before)
        self.assertEqual(list((self.root / "models").glob("*.part")), [])
        with patch("folder_memory.model_registry._open_url", side_effect=AssertionError("Already downloaded")):
            self.registry.download("fixture")

    def test_bad_hash_keeps_existing_file_and_selection_and_removes_partial(self):
        good = b"already existing useful weights"
        expected = b"expected replacement weights"
        self.downloadable(files=[{"field": "model", "filename": "fixture.gguf", "bytes": len(expected),
                                 "sha256": hashlib.sha256(expected).hexdigest()}])
        target = self.root / "models" / "fixture.gguf"
        target.parent.mkdir(parents=True)
        target.write_bytes(good)
        self.registry.select("fixture")
        before = self.registry.path.read_bytes()
        with patch("folder_memory.model_registry._open_url", side_effect=lambda *args: io.BytesIO(b"x" * len(expected))):
            with self.assertRaisesRegex(ValueError, "SHA256"):
                self.registry.download("fixture")
        self.assertEqual(target.read_bytes(), good)
        self.assertEqual(self.registry.path.read_bytes(), before)
        self.assertEqual(list(target.parent.glob(".*.part")), [])

    def test_multi_file_failure_does_not_publish_any_replacement(self):
        model, projector = b"verified model", b"verified projector"
        files = [{"field": field, "filename": field + ".gguf", "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()}
                 for field, data in (("model", model), ("mmproj", projector))]
        self.downloadable(files=files)
        with patch("folder_memory.model_registry._open_url", side_effect=[io.BytesIO(model), io.BytesIO(b"broken")]):
            with self.assertRaises(ValueError):
                self.registry.download("fixture")
        self.assertFalse((self.root / "models" / "model.gguf").exists())
        self.assertFalse((self.root / "models" / "mmproj.gguf").exists())
        self.assertEqual(list((self.root / "models").glob(".*.part")), [])

    def test_missing_hash_is_resolved_from_the_same_pinned_revision(self):
        contents = b"weights from pinned metadata"
        self.downloadable(files=[{"field": "model", "filename": "fixture.gguf"}])
        digest = hashlib.sha256(contents).hexdigest()
        metadata = json.dumps({"sha": "a" * 40, "siblings": [{"rfilename": "fixture.gguf",
                              "lfs": {"sha256": digest, "size": len(contents)}}]}).encode()
        with patch("folder_memory.model_registry._open_url", side_effect=[io.BytesIO(metadata), io.BytesIO(contents)]) as request:
            profile = self.registry.download("fixture")
        self.assertIn("/revision/" + "a" * 40, request.call_args_list[0].args[0])
        self.assertEqual(profile["model_sha256"], digest)
        self.assertEqual(profile["download"]["files"][0]["sha256"], digest)

    def test_wrong_revision_or_missing_lfs_hash_cannot_supply_weights(self):
        self.downloadable(files=[{"field": "model", "filename": "fixture.gguf"}])
        for metadata in ({"sha": "b" * 40, "siblings": []},
                         {"sha": "a" * 40, "siblings": [{"rfilename": "fixture.gguf", "lfs": None}]}):
            with patch("folder_memory.model_registry._open_url", return_value=io.BytesIO(json.dumps(metadata).encode())):
                with self.assertRaises(ValueError):
                    self.registry.download("fixture")
        self.assertFalse((self.root / "models" / "fixture.gguf").exists())

    def test_unpinned_download_and_escaping_filename_are_rejected(self):
        for revision, filename in (("main", "fixture.gguf"), ("a" * 40, "../outside.gguf")):
            with self.assertRaises(ValueError):
                self.registry.add("bad", {"kind": "chat", "download": {"repo": "Official/Fixture",
                                          "revision": revision,
                                          "files": [{"field": "model", "filename": filename}]}})

    def test_manual_provider_edit_overrides_flat_copies_without_rewriting_json(self):
        self.registry.save_provider("cloud", {"name": "Cloud", "base_url": "https://old.example/v1",
                                              "api_key_env": "OLD_PROVIDER_KEY"})
        self.registry.add("api", {"kind": "chat", "name": "API model", "provider_id": "cloud",
                                  "model_id": "provider/model", "context": 32768})
        self.registry.select("api")
        raw = json.loads(self.registry.path.read_text())
        raw["providers"]["cloud"] = {"name": "Edited cloud", "base_url": "https://new.example/api/v1"}
        self.registry.path.write_text(json.dumps(raw))
        before = self.registry.path.read_bytes()
        loaded = self.registry.load()["profiles"]["api"]
        self.assertEqual(loaded["base_url"], "https://new.example/api/v1")
        self.assertNotIn("api_key_env", loaded)
        self.assertTrue(self.registry.ready("api"))
        self.assertEqual(self.registry.resolved()["base_url"], "https://new.example/api/v1")
        self.assertEqual(self.registry.path.read_bytes(), before)
        self.registry.update("api", {**loaded, "name": "Renamed"})
        saved = json.loads(self.registry.path.read_text())["profiles"]["api"]
        self.assertEqual(saved["base_url"], "https://new.example/api/v1")
        self.assertNotIn("api_key_env", saved)

    def test_manual_linked_profile_needs_no_duplicate_url_or_auth_fields(self):
        self.registry.save_provider("home", {"name": "Home", "base_url": "http://127.0.0.1:12345/v1",
                                             "api_key_env": "JSON_PROVIDER_KEY"})
        raw = self.registry.load()
        raw["profiles"]["manual"] = {"kind": "chat", "name": "Manual model", "provider_id": "home",
                                        "model_id": "exact-id", "context": 65536}
        raw["selected_chat"] = "manual"
        self.registry.path.write_text(json.dumps(raw))
        with patch.dict(os.environ, {"JSON_PROVIDER_KEY": "PRIVATE_KEY"}):
            profile = self.registry.get("manual")
            self.assertEqual(profile["backend"], "openai")
            self.assertEqual(profile["base_url"], "http://127.0.0.1:12345/v1")
            self.assertEqual(profile["api_key_env"], "JSON_PROVIDER_KEY")
            self.assertTrue(self.registry.ready("manual"))
            self.assertEqual(self.registry.resolved()["model_id"], "exact-id")
            self.assertTrue(self.registry.resolved()["enabled"])
        self.assertNotIn("PRIVATE_KEY", self.registry.path.read_text())

    def test_provider_binding_still_rejects_missing_provider_and_inline_secrets(self):
        self.registry.save_provider("safe", {"name": "Safe", "base_url": "https://example.com/v1"})
        valid = self.registry.load()
        for changes in ({"provider_id": "missing"}, {"provider_id": "safe", "api_key": "NEVER_STORE_THIS"}):
            raw = json.loads(json.dumps(valid))
            raw["profiles"]["bad"] = {"name": "Bad model", "kind": "chat", "model_id": "exact-id", **changes}
            self.registry.path.write_text(json.dumps(raw))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.registry.load()

    def test_first_migration_of_flattened_runtime_does_not_require_missing_provider_records(self):
        self.legacy({"kind": "chat", "backend": "openai", "enabled": True, "name": "Moved model",
                     "provider_id": "provider-on-another-checkout", "model_id": "exact-model",
                     "base_url": "https://provider.example/v1", "context": 32768})
        data = self.registry.load()
        profile = data["profiles"][data["selected_chat"]]
        self.assertNotIn("provider_id", profile)
        self.assertEqual(profile["base_url"], "https://provider.example/v1")
        self.assertTrue(self.registry.ready(data["selected_chat"]))


if __name__ == "__main__":
    unittest.main()
