"""Native app operations share real global profiles and provider transport."""

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from folder_memory.model_registry import ModelRegistry
from folder_memory.model_backend import model_config_digest
from folder_memory.model_settings import ModelSettings
from folder_memory.neural import NeuralError


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state = self.directory / "runtime"
        self.state.mkdir()
        self.root = self.directory / "config"
        environment = patch.dict(os.environ, {"HANDY_CONFIG_HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.binary = self.directory / "llama-server"
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.binary.chmod(0o700)
        discovery = patch("folder_memory.model_registry.shutil.which", return_value=str(self.binary))
        discovery.start()
        self.addCleanup(discovery.stop)
        service = patch("folder_memory.model_settings.engine_status", return_value={"status": "stopped"})
        service.start()
        self.addCleanup(service.stop)
        self.manager = ModelSettings(self.state)
        self.registry = self.manager.registry
        self.registry.load()
        self.requests = []
        self.payload = {"data": [{"id": "z-chat", "context_length": 32768}, {"id": "a-chat"}, {"id": "a-chat"}]}
        self.http_status = 200
        self.chat_response = {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        fixture = self

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                fixture.requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
                self.respond(fixture.payload)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append({"path": self.path, "body": body, "authorization": self.headers.get("Authorization")})
                if self.path.endswith("embeddings"):
                    self.respond({"data": [{"index": 0, "embedding": [0.2, 0.4]}]})
                else:
                    self.respond(fixture.chat_response)

            def respond(self, value):
                raw = json.dumps(value).encode()
                self.send_response(fixture.http_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.serving = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.serving.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"

    def provider(self, **changes):
        return {"name": "Fixture API", "base_url": self.base, "auth": "none", **changes}

    def save_provider(self, identifier="fixture", **changes):
        request = {"operation": "save_provider", "id": identifier, "provider": self.provider(**changes)}
        self.manager.perform(request)
        return self.registry.providers()[identifier]

    def api_profile(self, **changes):
        return {"name": "Fixture chat", "kind": "chat", "backend": "openai", "enabled": True,
                "provider_id": "fixture", "model_id": "z-chat", "context": 32768, **changes}

    def add_model(self, identifier="chat", **changes):
        self.manager.perform({"operation": "save_profile", "id": identifier, "profile": self.api_profile(**changes)})

    def local(self, identifier="local", kind="chat"):
        path = self.directory / (identifier + ".gguf")
        path.write_bytes(b"GGUF" + struct.pack("<I", 3) + b"fixture")
        profile = {"name": "Local fixture", "kind": kind, "backend": "local", "enabled": True,
                   "binary": str(self.binary), "model": str(path), "context": 4096}
        if kind == "embedding":
            profile.update(dimensions=2, pooling="mean")
        self.manager.perform({"operation": "save_profile", "id": identifier, "profile": profile})
        return path

    def test_snapshot_shares_global_selection_and_does_not_start_or_request(self):
        snapshot = self.manager.perform({"operation": "snapshot"})
        self.assertEqual(snapshot["selected_chat"], "lfm3-vl")
        self.assertEqual(snapshot["config_root"], str(self.root))
        self.assertEqual(snapshot["providers"], [])
        self.assertEqual(snapshot["binary"], str(self.binary))
        spark = next(item for item in snapshot["profiles"] if item["id"] == "spark")
        self.assertEqual(spark["download_bytes"], 4375021824)
        self.assertFalse(spark["ready"])
        self.assertEqual(spark["category"], "standard")
        self.assertEqual(self.requests, [])

    def test_provider_discovery_and_connection_use_real_endpoint_and_sort_dedupe(self):
        self.save_provider()
        result = self.manager.perform({"operation": "discover", "provider_id": "fixture"})
        self.assertEqual(result["models"], [{"id": "a-chat"}, {"id": "z-chat", "context": 32768}])
        tested = self.manager.perform({"operation": "test_provider", "provider_id": "fixture"})
        self.assertEqual(tested["count"], 2)
        self.assertEqual([item["path"] for item in self.requests], ["/v1/models", "/v1/models"])

    def test_unsaved_key_is_only_in_request_memory_and_authorization(self):
        self.manager.perform({"operation": "discover", "provider": self.provider(auth="key"), "api_key": "TEST_SECRET"})
        self.assertEqual(self.requests[-1]["authorization"], "Bearer TEST_SECRET")
        self.assertNotIn("TEST_SECRET", self.registry.path.read_text())
        self.assertFalse((self.root / "credentials").exists())

    def test_saved_key_is_private_not_in_snapshot_and_preserved_when_blank(self):
        snapshot = self.manager.perform({"operation": "save_provider", "id": "private", "provider": self.provider(auth="key"),
                                         "api_key": "TEST_SECRET"})
        self.assertNotIn("TEST_SECRET", json.dumps(snapshot))
        provider = self.registry.providers()["private"]
        path = Path(provider["api_key_file"])
        self.assertEqual(path.read_text(), "TEST_SECRET")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.manager.perform({"operation": "save_provider", "id": "private", "original_id": "private",
                              "provider": self.provider(auth="key", name="Renamed"), "api_key": ""})
        self.assertEqual(self.registry.providers()["private"]["api_key_file"], str(path))
        self.manager.perform({"operation": "discover", "original_id": "private", "provider": self.provider(auth="key")})
        self.assertEqual(self.requests[-1]["authorization"], "Bearer TEST_SECRET")

    def test_key_update_does_not_change_old_runtime_key_and_propagates_models(self):
        self.manager.perform({"operation": "save_provider", "id": "fixture", "provider": self.provider(auth="key"), "api_key": "OLD_SECRET"})
        self.add_model()
        old_path = self.registry.get("chat")["api_key_file"]
        self.manager.perform({"operation": "save_provider", "id": "fixture", "original_id": "fixture",
                              "provider": self.provider(auth="key"), "api_key": "NEW_SECRET"})
        current = self.registry.get("chat")
        self.assertNotEqual(current["api_key_file"], old_path)
        self.assertEqual(Path(old_path).read_text(), "OLD_SECRET")
        self.assertEqual(Path(current["api_key_file"]).read_text(), "NEW_SECRET")

    def test_connection_errors_do_not_reflect_body_or_key(self):
        self.http_status = 401
        self.payload = {"error": "REFLECTED_SECRET unauthorized"}
        with self.assertRaises(NeuralError) as error:
            self.manager.perform({"operation": "discover", "provider": self.provider(auth="key"), "api_key": "TEST_SECRET"})
        self.assertIn("401", str(error.exception))
        self.assertNotIn("SECRET", str(error.exception))

    def test_missing_model_list_does_not_prevent_manual_model(self):
        self.save_provider()
        self.payload = {"status": "online"}
        with self.assertRaisesRegex(ValueError, "вручную"):
            self.manager.perform({"operation": "discover", "provider_id": "fixture"})
        self.add_model(model_id="manual/provider-model")
        self.assertEqual(self.registry.get("chat")["model_id"], "manual/provider-model")

    def test_provider_edits_atomically_update_linked_profiles_and_cascade_removal(self):
        self.save_provider()
        self.add_model()
        self.add_model("another")
        changed = self.provider(base_url=self.base + "/custom")
        self.manager.perform({"operation": "save_provider", "id": "fixture", "original_id": "fixture", "provider": changed})
        self.assertEqual(self.registry.get("chat")["base_url"], self.base + "/custom")
        self.assertEqual(self.registry.get("another")["base_url"], self.base + "/custom")
        with patch("folder_memory.model_settings.activate_models") as activate:
            self.manager.perform({"operation": "delete_provider", "id": "fixture"})
            activate.assert_not_called()
        self.assertEqual(self.registry.providers(), {})
        self.assertNotIn("chat", self.registry.profiles())
        self.assertNotIn("another", self.registry.profiles())

    def test_active_provider_cascade_prefers_ready_standard_local_fallback(self):
        self.save_provider()
        self.add_model()
        self.registry.select("chat")
        other_weights = self.local("another-local")
        standard = self.registry.get("lfm3-vl")
        for field in ("model", "mmproj"):
            path = Path(standard[field])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"GGUF" + struct.pack("<I", 3))
        with patch("folder_memory.model_settings.activate_models") as activate:
            result = self.manager.perform({"operation": "delete_provider", "id": "fixture"})
            self.assertEqual(activate.call_args.kwargs, {"start": False})
            self.assertEqual(activate.call_args.args[1]["name"], standard["name"])
        self.assertEqual(result["selected_chat"], "lfm3-vl")
        self.assertTrue(other_weights.exists())
        self.assertTrue(Path(standard["model"]).exists())

    def test_active_provider_cascade_can_use_custom_local_and_disable_encoder(self):
        self.save_provider()
        self.add_model()
        self.add_model("api-encoder", kind="embedding", context=8192, dimensions=2)
        self.registry.select("chat")
        self.registry.select("api-encoder", kind="embedding")
        self.local("local-fallback")
        with patch("folder_memory.model_settings.activate_models") as activate:
            result = self.manager.perform({"operation": "delete_provider", "id": "fixture"})
        self.assertEqual(result["selected_chat"], "local-fallback")
        self.assertIsNone(result["selected_embedding"])
        self.assertEqual(activate.call_args.args[1]["embedding"], {"enabled": False})

    def test_active_provider_cascade_without_local_never_selects_other_paid_api(self):
        self.manager.perform({"operation": "save_provider", "id": "fixture", "provider": self.provider(auth="key"), "api_key": "RETAINED_KEY"})
        self.add_model()
        key_path = Path(self.registry.providers()["fixture"]["api_key_file"])
        self.save_provider("another-provider")
        self.add_model("another-paid", provider_id="another-provider")
        self.registry.select("chat")
        with patch("folder_memory.model_settings.activate_models") as activate:
            result = self.manager.perform({"operation": "delete_provider", "id": "fixture"})
        self.assertIsNone(result["selected_chat"])
        self.assertFalse(activate.call_args.args[1]["enabled"])
        self.assertIn("another-paid", self.registry.profiles())
        self.assertIn("another-provider", self.registry.providers())
        self.assertEqual(key_path.read_text(), "RETAINED_KEY")

    def test_encoder_only_provider_cascade_restarts_without_changing_chat(self):
        self.local("working-chat")
        self.registry.select("working-chat")
        self.save_provider()
        self.add_model("api-encoder", kind="embedding", context=8192, dimensions=2)
        self.registry.select("api-encoder", kind="embedding")
        with patch("folder_memory.model_settings.activate_models") as activate:
            result = self.manager.perform({"operation": "delete_provider", "id": "fixture"})
        self.assertEqual(result["selected_chat"], "working-chat")
        self.assertIsNone(result["selected_embedding"])
        activate.assert_called_once()
        self.assertEqual(activate.call_args.args[1]["embedding"], {"enabled": False})

    def test_provider_cascade_reports_activation_failure_and_keeps_desired_deletion(self):
        self.save_provider()
        self.add_model()
        self.registry.select("chat")
        with patch("folder_memory.model_settings.activate_models", side_effect=ValueError("restart failed")):
            with self.assertRaisesRegex(ValueError, "restart failed"):
                self.manager.perform({"operation": "delete_provider", "id": "fixture"})
        self.assertNotIn("fixture", self.registry.providers())
        self.assertNotIn("chat", self.registry.profiles())
        self.assertIsNone(self.registry.load()["selected_chat"])

    def test_snapshot_categories_are_derived_and_not_saved_in_profiles(self):
        self.local()
        self.save_provider()
        self.add_model()
        records = {item["id"]: item for item in self.manager.snapshot()["profiles"]}
        self.assertEqual(records["lfm3-vl"]["category"], "standard")
        self.assertEqual(records["local"]["category"], "custom")
        self.assertEqual(records["chat"]["category"], "custom")
        profile = records["local"]
        profile["category"] = "standard"
        self.manager.perform({"operation": "save_profile", "id": "local", "original_id": "local", "profile": profile})
        self.assertNotIn("category", self.registry.get("local"))
        standard = self.registry.get("spark")
        standard["download"]["revision"] = "b" * 40
        self.registry.update("spark", standard)
        record = next(item for item in self.manager.snapshot()["profiles"] if item["id"] == "spark")
        self.assertEqual(record["category"], "custom")

    def test_linked_model_ignores_stale_auth_and_url_from_editor(self):
        self.save_provider()
        self.add_model(base_url="https://wrong.invalid/v1", api_key_file="/missing/file")
        profile = self.registry.get("chat")
        self.assertEqual(profile["base_url"], self.base)
        self.assertNotIn("api_key_file", profile)

    def test_profile_delete_preserves_weights_and_rejects_selected(self):
        path = self.local()
        self.registry.select("local")
        with self.assertRaisesRegex(ValueError, "Сначала выбери"):
            self.manager.perform({"operation": "delete_profile", "id": "local"})
        self.registry.select("lfm3-vl")
        self.manager.perform({"operation": "delete_profile", "id": "local"})
        self.assertTrue(path.is_file())

    def test_select_and_apply_use_safe_activation_and_do_not_autostart(self):
        self.local()
        with patch("folder_memory.model_settings.activate_models") as activate:
            result = self.manager.perform({"operation": "select", "id": "local", "kind": "chat"})
            self.assertEqual(result["selected_chat"], "local")
            self.assertEqual(activate.call_args.kwargs, {"start": False})
            self.manager.perform({"operation": "apply"})
            self.assertEqual(activate.call_args.kwargs, {"start": False, "force": True})
        previous_encoder = self.registry.load()["selected_embedding"]
        with patch("folder_memory.model_settings.activate_models", side_effect=ValueError("restart failed")):
            with self.assertRaisesRegex(ValueError, "restart failed"):
                self.manager.perform({"operation": "select", "id": None, "kind": "embedding"})
        self.assertEqual(self.registry.load()["selected_embedding"], previous_encoder)

    def test_failed_model_switch_restores_selection_and_enable_flag(self):
        self.save_provider()
        self.add_model(enabled=False)
        previous = self.registry.load()
        with patch("folder_memory.model_settings.activate_models", side_effect=ValueError("restart failed")):
            with self.assertRaisesRegex(ValueError, "restart failed"):
                self.manager.perform({"operation": "select", "id": "chat"})
        self.assertEqual(self.registry.load(), previous)

    def test_failed_model_switch_preserves_concurrent_selection(self):
        self.save_provider()
        self.add_model()
        self.add_model('another')
        def concurrent(*args, **kwargs):
            self.registry.select('another')
            raise ValueError('restart failed')
        with patch('folder_memory.model_settings.activate_models', side_effect=concurrent):
            with self.assertRaisesRegex(ValueError, 'restart failed'):
                self.manager.perform({'operation': 'select', 'id': 'chat'})
        self.assertEqual(self.registry.load()['selected_chat'], 'another')

    def test_unready_selection_and_missing_main_model_rejected(self):
        with self.assertRaisesRegex(ValueError, "не готова"):
            self.manager.perform({"operation": "select", "id": "spark", "kind": "chat"})
        with self.assertRaisesRegex(ValueError, "основную"):
            self.manager.perform({"operation": "select", "id": None, "kind": "chat"})

    def test_local_check_reads_gguf_without_loading_runtime(self):
        path = self.local()
        with patch("subprocess.Popen", side_effect=AssertionError("No runtime start")):
            result = self.manager.perform({"operation": "test_profile", "id": "local"})
        self.assertGreaterEqual(result["milliseconds"], 0)
        path.write_bytes(b"not a GGUF")
        with self.assertRaisesRegex(ValueError, "GGUF"):
            self.manager.perform({"operation": "test_profile", "id": "local"})

    def test_api_model_check_is_explicit_tiny_real_inference(self):
        self.save_provider()
        self.add_model()
        result = self.manager.perform({"operation": "test_profile", "id": "chat"})
        self.assertGreaterEqual(result["milliseconds"], 0)
        self.assertEqual(self.requests[-1]["path"], "/v1/chat/completions")
        self.assertEqual(self.requests[-1]["body"]["model"], "z-chat")
        self.assertEqual(self.requests[-1]["body"]["max_tokens"], 16)
        self.add_model("encoder", kind="embedding", dimensions=2, context=8192)
        self.manager.perform({"operation": "test_profile", "id": "encoder"})
        self.assertEqual(self.requests[-1]["path"], "/v1/embeddings")

    def test_catalogue_restore_preserves_user_edits_and_other_profiles(self):
        profile = self.registry.get("lfm3-vl")
        profile["name"] = "My selected model"
        self.registry.update("lfm3-vl", profile)
        self.manager.perform({"operation": "delete_profile", "id": "spark"})
        self.manager.perform({"operation": "restore_catalogue"})
        self.assertIn("spark", self.registry.profiles())
        self.assertEqual(self.registry.get("lfm3-vl")["name"], "My selected model")

    def test_api_probe_accepts_reasoning_before_text_but_rejects_empty_response(self):
        self.save_provider()
        self.add_model()
        self.chat_response = {"choices": [{"message": {"role": "assistant", "content": None,
                                                       "reasoning_content": "Let me check."}, "finish_reason": "length"}]}
        result = self.manager.perform({"operation": "test_profile", "id": "chat"})
        self.assertGreaterEqual(result["milliseconds"], 0)
        self.chat_response = {"choices": [{"message": {"role": "assistant", "content": ""}}]}
        with self.assertRaisesRegex(ValueError, "без текста"):
            self.manager.perform({"operation": "test_profile", "id": "chat"})

    def test_custom_weights_edit_discards_stale_provenance_and_embedding_hash(self):
        old = self.local("encoder", kind="embedding")
        profile = self.registry.get("encoder")
        profile["download"] = {"repo": "Official/Fixture", "revision": "a" * 40,
                               "files": [{"field": "model", "filename": "old.gguf", "sha256": "b" * 64}]}
        profile["model_bytes"] = old.stat().st_size
        self.registry.update("encoder", profile)
        other = self.directory / "other.gguf"
        other.write_bytes(b"GGUF" + struct.pack("<I", 3) + b"changed")
        profile["model"] = str(other)
        self.manager.perform({"operation": "save_profile", "id": "encoder", "original_id": "encoder", "profile": profile})
        current = self.registry.get("encoder")
        self.assertEqual(current["model_sha256"], hashlib.sha256(other.read_bytes()).hexdigest())
        self.assertNotIn("download", current)
        self.assertNotIn("model_bytes", current)

    def test_profile_validation_rejects_relative_paths_context_and_provider_injection(self):
        path = self.local()
        profile = self.registry.get("local")
        for updates in ({"model": "relative.gguf"}, {"context": 65537}, {"context": True}, {"binary": "/not/installed"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.manager.perform({"operation": "save_profile", "id": "local", "original_id": "local", "profile": {**profile, **updates}})
        self.assertTrue(path.exists())
        with self.assertRaises(ValueError):
            self.manager.perform({"operation": "save_provider", "id": "../escape", "provider": self.provider()})
        with self.assertRaises(NeuralError):
            self.manager.perform({"operation": "save_provider", "id": "unsafe", "provider": self.provider(base_url="http://8.8.8.8/v1")})

    def test_embedding_validation_uses_actual_encoder_limits_before_saving(self):
        self.save_provider()
        for changes in ({"context": 65536}, {"context": 32769}, {"query_prefix": "x" * 1001},
                        {"passage_prefix": "x" * 1001}, {"max_input_chars": 16001}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.add_model("invalid-encoder", kind="embedding", dimensions=2,
                               **{"context": 8192, **changes})
            self.assertNotIn("invalid-encoder", self.registry.profiles())
        self.add_model("valid-encoder", kind="embedding", dimensions=2, context=32768,
                       query_prefix="x" * 1000, passage_prefix="")
        self.assertEqual(self.registry.get("valid-encoder")["context"], 32768)

    def test_encoder_metadata_edit_preserves_existing_fallback_index(self):
        profile = self.registry.get("qwen3-embedding")
        self.assertNotIn("index_path", profile)
        profile["name"] = "Renamed encoder"
        self.manager.perform({"operation": "save_profile", "id": "qwen3-embedding",
                              "original_id": "qwen3-embedding", "profile": profile})
        self.assertNotIn("index_path", self.registry.get("qwen3-embedding"))
        profile["context"] = 4096  # Local effective input policy still caps at 512.
        self.manager.perform({"operation": "save_profile", "id": "qwen3-embedding",
                              "original_id": "qwen3-embedding", "profile": profile})
        self.assertNotIn("index_path", self.registry.get("qwen3-embedding"))
        profile["query_prefix"] = "Different policy: "
        self.manager.perform({"operation": "save_profile", "id": "qwen3-embedding",
                              "original_id": "qwen3-embedding", "profile": profile})
        self.assertEqual(self.registry.get("qwen3-embedding")["index_path"],
                         str(self.root / "indices/qwen3-embedding.sqlite3"))

    def test_existing_invalid_encoder_policy_can_be_repaired(self):
        self.save_provider()
        profile = {**self.api_profile(kind="embedding", dimensions=2, context=65536),
                   "base_url": self.base, "query_prefix": "x" * 1001}
        self.registry.add("old-invalid", profile)
        profile.update(context=8192, query_prefix="query: ")
        self.manager.perform({"operation": "save_profile", "id": "old-invalid", "original_id": "old-invalid", "profile": profile})
        self.assertEqual(self.registry.get("old-invalid")["context"], 8192)

    def test_download_progress_and_cancellation_clean_partial_files(self):
        content = b"download fixture"
        profile = {"name": "Download", "kind": "chat", "backend": "local", "enabled": True,
                   "binary": str(self.binary), "model": str(self.root / "models/download.gguf"), "context": 4096,
                   "download": {"repo": "Official/Test", "revision": "a" * 40,
                                "files": [{"field": "model", "filename": "download.gguf", "bytes": len(content),
                                           "sha256": hashlib.sha256(content).hexdigest()}]}}
        self.registry.add("download", profile)
        events = []
        self.manager.progress = lambda *event: events.append(event)
        with patch("folder_memory.model_registry._open_url", return_value=io.BytesIO(content)):
            self.manager.perform({"operation": "download", "id": "download"})
        self.assertEqual(events[-1][1], len(content))
        self.assertEqual(Path(self.registry.get("download")["model"]).read_bytes(), content)
        self.assertEqual(self.registry.load()["selected_chat"], "lfm3-vl")
        profile["model"] = str(self.root / "models/cancelled.gguf")
        profile["download"]["files"][0]["filename"] = "cancelled.gguf"
        self.registry.add("cancelled", profile)
        self.manager.progress = lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        with patch("folder_memory.model_registry._open_url", return_value=io.BytesIO(content)):
            with self.assertRaises(KeyboardInterrupt):
                self.manager.perform({"operation": "download", "id": "cancelled"})
        self.assertFalse((self.root / "models/cancelled.gguf").exists())
        self.assertEqual(list((self.root / "models").glob("*.part")), [])

    def test_bridge_uses_stdin_json_and_only_ndjson_stdout(self):
        result = subprocess.run([sys.executable, "-m", "folder_memory.model_settings", "--state-dir", str(self.state)],
                                input=json.dumps({"operation": "snapshot"}), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["event"], "result")
        self.assertEqual(response["data"]["config_root"], str(self.root))
        malformed = subprocess.run([sys.executable, "-m", "folder_memory.model_settings", "--state-dir", str(self.state)],
                                   input="not JSON SECRET", text=True, capture_output=True, timeout=10)
        self.assertEqual(malformed.returncode, 1)
        self.assertEqual(json.loads(malformed.stdout)["event"], "error")
        self.assertNotIn("SECRET", malformed.stdout + malformed.stderr)

    def test_bridge_sigterm_cancels_download_and_removes_hidden_partial(self):
        content = b"x" * (1024 * 1024 * 2)
        self.registry.add("cancel-process", {"kind": "chat", "backend": "local", "name": "Cancelled fixture",
                                           "model": str(self.root / "models/cancel-process.gguf"),
                                           "binary": str(self.binary), "context": 4096,
                                           "download": {"repo": "Official/Fixture", "revision": "a" * 40,
                                                        "files": [{"field": "model", "filename": "cancel-process.gguf",
                                                                   "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}]}})
        code = """
import io, time
from unittest.mock import patch
from folder_memory.model_settings import main
class Slow(io.BytesIO):
    def read1(self, size):
        if self.tell():
            time.sleep(30)
        return self.read(size)
with patch('folder_memory.model_registry._open_url', return_value=Slow(b'x' * (2 * 1024 * 1024))):
    main()
"""
        process = subprocess.Popen([sys.executable, "-c", code, "--state-dir", str(self.state)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            process.stdin.write(json.dumps({"operation": "download", "id": "cancel-process"}))
            process.stdin.close()
            event = json.loads(process.stdout.readline())
            self.assertEqual(event["event"], "progress")
            self.assertTrue(list((self.root / "models").glob("*.part")))
            process.terminate()
            self.assertEqual(process.wait(timeout=5), 130)
            result = json.loads(process.stdout.read())
            self.assertEqual(result["event"], "error")
            self.assertIn("отменено", result["message"])
            self.assertEqual(list((self.root / "models").glob("*.part")), [])
            self.assertFalse((self.root / "models/cancel-process.gguf").exists())
            self.assertEqual(process.stderr.read(), "")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def draft_api(self, selected=True):
        snapshot = self.manager.snapshot()
        settings = snapshot["settings"]
        settings.setdefault("providers", {})["draft-provider"] = {"name": "Draft provider", "base_url": self.base}
        settings["profiles"]["draft-model"] = self.api_profile(provider_id="draft-provider", model_id="draft-id")
        if selected:
            settings["selected_chat"] = "draft-model"
        return {"settings": settings, "revision": snapshot["revision"], "credentials": {"draft-provider": "DRAFT_SECRET"}}

    def test_preview_keeps_new_credentials_in_memory_and_touches_no_files_or_network(self):
        request = self.draft_api()
        before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.directory.rglob("*") if path.is_file()}
        with patch("folder_memory.model_settings.engine_status", side_effect=AssertionError("No status HTTP")):
            result = self.manager.perform({"operation": "preview_settings", **request})
        after = {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in self.directory.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertNotIn("DRAFT_SECRET", json.dumps(result))
        self.assertNotIn("api_key_file", result["settings"]["providers"]["draft-provider"])
        profile = next(item for item in result["profiles"] if item["id"] == "draft-model")
        self.assertTrue(profile["ready"])
        self.assertEqual(result["revision"], request["revision"])
        self.assertEqual(self.requests, [])
        self.assertFalse((self.root / "credentials").exists())

    def test_preview_fresh_configuration_does_not_create_registry_or_lock(self):
        empty_root = self.directory / "uncreated-config"
        with patch.dict(os.environ, {"HANDY_CONFIG_HOME": str(empty_root)}):
            manager = ModelSettings(self.state)
            settings = manager.registry.peek()
            manager.perform({"operation": "preview_settings", "settings": settings})
        self.assertFalse(empty_root.exists())

    def test_apply_settings_saves_one_private_key_and_activates_snapshot(self):
        request = self.draft_api()
        with patch("folder_memory.model_settings.activate_models") as activate:
            result = self.manager.perform({"operation": "apply_settings", **request})
        activate.assert_called_once()
        self.assertEqual(activate.call_args.kwargs, {"start": False})
        self.assertEqual(activate.call_args.args[1]["model_id"], "draft-id")
        self.assertEqual(result["selected_chat"], "draft-model")
        path = Path(result["settings"]["providers"]["draft-provider"]["api_key_file"])
        self.assertEqual(path.read_text(), "DRAFT_SECRET")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list((self.root / "credentials").glob("*.key")), [path])
        self.assertNotIn("DRAFT_SECRET", json.dumps(result))
        self.assertNotIn("DRAFT_SECRET", self.registry.path.read_text())
        self.assertEqual(self.registry.get("draft-model")["api_key_file"], str(path))

    def test_stale_apply_does_not_overwrite_terminal_edits_or_store_keys(self):
        request = self.draft_api()
        changed = self.registry.get("lfm3-vl")
        changed["name"] = "Changed in terminal"
        self.registry.update("lfm3-vl", changed)
        with self.assertRaisesRegex(ValueError, "уже изменились"):
            self.manager.perform({"operation": "apply_settings", **request})
        self.assertEqual(self.registry.get("lfm3-vl")["name"], "Changed in terminal")
        self.assertNotIn("draft-provider", self.registry.providers())
        self.assertFalse((self.root / "credentials").exists())

    def test_apply_activation_failure_restores_registry(self):
        request = self.draft_api()
        before = self.registry.load()
        with patch("folder_memory.model_settings.activate_models", side_effect=ValueError("activation failed")):
            with self.assertRaisesRegex(ValueError, "activation failed"):
                self.manager.perform({"operation": "apply_settings", **request})
        self.assertEqual(self.registry.load(), before)
        self.assertNotIn("DRAFT_SECRET", self.registry.path.read_text())

    def test_failed_apply_does_not_rollback_concurrent_edit_after_its_commit(self):
        request = self.draft_api()

        def concurrent_edit(*_, **__):
            changed = self.registry.get("spark")
            changed["name"] = "Concurrent terminal edit"
            self.registry.update("spark", changed)
            raise ValueError("activation failed")

        with patch("folder_memory.model_settings.activate_models", side_effect=concurrent_edit):
            with self.assertRaisesRegex(ValueError, "activation failed"):
                self.manager.perform({"operation": "apply_settings", **request})
        self.assertEqual(self.registry.get("spark")["name"], "Concurrent terminal edit")

    def test_noop_apply_does_not_rewrite_registry_or_restart_running_service(self):
        snapshot = self.manager.snapshot()
        before = self.registry.path.stat().st_mtime_ns
        service = {"status": "running", "model_config": model_config_digest(self.registry.resolved())}
        with patch("folder_memory.model_settings.engine_status", return_value=service), \
                patch("folder_memory.model_settings.activate_models") as activate:
            self.manager.perform({"operation": "apply_settings", "settings": snapshot["settings"], "revision": snapshot["revision"]})
        activate.assert_not_called()
        self.assertEqual(self.registry.path.stat().st_mtime_ns, before)

    def test_visibility_hides_choices_without_changing_resolved_config_or_restart(self):
        self.save_provider()
        self.add_model()
        self.registry.select("chat")
        snapshot = self.manager.snapshot()
        before = self.registry.resolved()
        settings = snapshot["settings"]
        settings["providers"]["fixture"]["visible"] = False
        settings["profiles"]["lfm3-vl"]["visible"] = False
        settings["standard_provider_visible"] = False
        service = {"status": "running", "model_config": model_config_digest(before)}
        with patch("folder_memory.model_settings.engine_status", return_value=service), \
                patch("folder_memory.model_settings.activate_models") as activate:
            self.manager.perform({"operation": "apply_settings", "settings": settings, "revision": snapshot["revision"]})
        activate.assert_not_called()
        self.assertEqual(self.registry.resolved(), before)
        self.assertEqual(self.registry.visible_profiles(), {})
        self.registry.select("chat")  # Explicit IDs remain usable even if hidden.
        self.assertEqual(self.registry.load()["selected_chat"], "chat")
        self.assertEqual(self.registry.resolved(), before)

    def test_visibility_requires_boolean_flags(self):
        snapshot = self.manager.snapshot()
        for target, value in (("model", "yes"), ("provider", 1), ("standard", "yes")):
            settings = json.loads(json.dumps(snapshot["settings"]))
            if target == "model":
                settings["profiles"]["lfm3-vl"]["visible"] = value
            elif target == "provider":
                settings["providers"] = {"bad": {"name": "Bad", "base_url": self.base, "visible": value}}
            else:
                settings["standard_provider_visible"] = value
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "visible"):
                self.manager.perform({"operation": "preview_settings", "settings": settings})

    def test_factory_defaults_are_not_migrated_preferences_and_catalogue_stays_available(self):
        current = self.registry.get("lfm3-vl")
        current.update(name="My label", context=8192, sampling={"temperature": 0.9})
        self.registry.update("lfm3-vl", current)
        self.registry.remove("spark")
        snapshot = self.manager.snapshot()
        default = snapshot["defaults"]["lfm3-vl"]
        self.assertEqual(default["name"], "My label")
        self.assertEqual(default["model"], current["model"])
        self.assertEqual(default["context"], 65536)
        self.assertEqual(default["sampling"]["temperature"], 0.1)
        self.assertNotIn("spark", snapshot["settings"]["profiles"])
        spark = next(item for item in snapshot["catalogue"] if item["id"] == "spark")
        self.assertEqual(spark["category"], "standard")
        self.assertFalse(spark["ready"])

    def test_download_deleted_catalogue_entry_restores_only_requested_profile(self):
        self.registry.select(None, kind="embedding")
        self.registry.remove("spark")
        self.registry.remove("qwen3-embedding")
        with patch.object(self.registry, "download") as download:
            self.manager.perform({"operation": "download", "id": "spark"})
        download.assert_called_once()
        self.assertEqual(set(self.registry.profiles()), {"lfm3-vl", "spark"})
        self.assertEqual(self.registry.load()["selected_chat"], "lfm3-vl")

    def test_preview_restored_uninstalled_catalogue_encoder_preserves_pinned_hash_and_default_index(self):
        self.registry.select(None, kind="embedding")
        self.registry.remove("qwen3-embedding")
        snapshot = self.manager.snapshot()
        catalogue = next(item for item in snapshot["catalogue"] if item["id"] == "qwen3-embedding")
        snapshot["settings"]["profiles"]["qwen3-embedding"] = catalogue
        snapshot["settings"]["selected_embedding"] = "qwen3-embedding"
        result = self.manager.perform({"operation": "preview_settings", "settings": snapshot["settings"]})
        profile = result["settings"]["profiles"]["qwen3-embedding"]
        self.assertEqual(profile["model_sha256"], catalogue["model_sha256"])
        self.assertNotIn("index_path", profile)
        self.assertNotIn("qwen3-embedding", self.registry.profiles())

    def test_draft_model_connection_test_uses_unsaved_model_and_key(self):
        request = self.draft_api()
        before = self.registry.path.read_bytes()
        result = self.manager.perform({"operation": "test_profile", "id": "draft-model", **request})
        self.assertEqual(result["message"], "Подключено")
        self.assertEqual(self.requests[-1]["body"]["model"], "draft-id")
        self.assertEqual(self.requests[-1]["authorization"], "Bearer DRAFT_SECRET")
        self.assertEqual(self.registry.path.read_bytes(), before)
        self.assertFalse((self.root / "credentials").exists())

    def test_raw_settings_snapshot_excludes_derived_fields_and_keeps_saved_cas_revision(self):
        profile = self.registry.get("lfm3-vl")
        profile.update(id="wrong-id", ready=True, category="cached", auth="cached")
        self.registry.update("lfm3-vl", profile)
        snapshot = self.manager.snapshot()
        raw = snapshot["settings"]["profiles"]["lfm3-vl"]
        self.assertFalse({"id", "ready", "category", "auth"} & raw.keys())
        self.assertEqual(snapshot["revision"], self.registry.revision())
        with patch("folder_memory.model_settings.activate_models"):
            self.manager.perform({"operation": "apply_settings", "settings": snapshot["settings"], "revision": snapshot["revision"]})
        self.assertFalse({"id", "ready", "category", "auth"} & self.registry.get("lfm3-vl").keys())

    def test_service_start_materializes_current_global_choice_when_cache_is_absent(self):
        self.local("current-choice")
        self.registry.select("current-choice")
        self.assertFalse((self.state / "neural.json").exists())
        with patch("folder_memory.model_settings.activate_models") as activate:
            self.manager.perform({"operation": "start_service"})
        self.assertEqual(activate.call_args.args[1]["name"], "Local fixture")
        self.assertEqual(activate.call_args.kwargs, {"start": True})

    def test_service_start_rejects_uninstalled_or_disabled_selected_model(self):
        with patch("folder_memory.model_settings.activate_models") as activate:
            with self.assertRaisesRegex(ValueError, "Сначала выбери"):
                self.manager.perform({"operation": "start_service"})
            activate.assert_not_called()
        self.local("disabled")
        self.registry.select("disabled")
        profile = self.registry.get("disabled")
        profile["enabled"] = False
        self.registry.update("disabled", profile)
        with patch("folder_memory.model_settings.activate_models") as activate:
            with self.assertRaisesRegex(ValueError, "Сначала выбери"):
                self.manager.perform({"operation": "start_service"})
            activate.assert_not_called()

    def test_service_start_with_intentionally_absent_chat_does_not_reuse_old_provider(self):
        data = self.registry.load()
        data["selected_chat"] = None
        self.registry.replace_settings(data, expected_revision=self.registry.revision())
        with patch("folder_memory.model_settings.activate_models") as activate:
            self.manager.perform({"operation": "start_service"})
        self.assertFalse(activate.call_args.args[1]["enabled"])
        self.assertNotIn("base_url", activate.call_args.args[1])
        self.assertEqual(activate.call_args.kwargs, {"start": True})

    def test_saved_provider_refresh_registers_inventory_without_changing_preferences_or_restart(self):
        self.save_provider()
        self.add_model("existing-z", model_id="z-chat", context=8192, visible=False,
                       sampling={"temperature": 0.73}, name="Custom label")
        before = self.registry.load()
        active_before = self.registry.resolved()
        with patch("folder_memory.model_settings.activate_models", side_effect=AssertionError("No activation")):
            result = self.manager.perform({"operation": "refresh_models", "provider_id": "fixture",
                                           "revision": self.registry.revision()})
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["added"], 1)
        after = self.registry.load()
        self.assertEqual(after["profiles"]["existing-z"], before["profiles"]["existing-z"])
        self.assertEqual(after["selected_chat"], before["selected_chat"])
        self.assertEqual(after["selected_embedding"], before["selected_embedding"])
        self.assertEqual(self.registry.resolved(), active_before)
        new = [(identifier, profile) for identifier, profile in after["profiles"].items() if identifier not in before["profiles"]]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0][1]["model_id"], "a-chat")
        self.assertEqual(new[0][1]["provider_id"], "fixture")
        self.assertEqual(result["settings"], after)

    def test_saved_inventory_refresh_is_idempotent_without_rewriting_or_duplicate_ids(self):
        self.save_provider()
        first = self.manager.perform({"operation": "refresh_models", "provider_id": "fixture"})
        before_bytes = self.registry.path.read_bytes()
        before_mtime = self.registry.path.stat().st_mtime_ns
        second = self.manager.perform({"operation": "refresh_models", "provider_id": "fixture", "revision": first["revision"]})
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(self.registry.path.read_bytes(), before_bytes)
        self.assertEqual(self.registry.path.stat().st_mtime_ns, before_mtime)
        self.assertEqual(self.requests[-1]["path"], "/v1/models")

    def test_inventory_refresh_rejects_stale_revision_before_and_during_network(self):
        self.save_provider()
        stale = self.registry.revision()
        provider = self.registry.providers()["fixture"]
        self.registry.save_provider("fixture", {**provider, "name": "Concurrent name"}, replace=True)
        with self.assertRaisesRegex(ValueError, "уже изменились"):
            self.manager.perform({"operation": "refresh_models", "provider_id": "fixture", "revision": stale})
        self.assertEqual(self.requests, [])
        current = self.registry.revision()
        real_discover = self.manager.discover

        def edit_during_network(request):
            result = real_discover(request)
            provider = self.registry.providers()["fixture"]
            self.registry.save_provider("fixture", {**provider, "base_url": self.base + "/changed"}, replace=True)
            return result

        before = set(self.registry.profiles())
        with patch.object(self.manager, "discover", side_effect=edit_during_network):
            with self.assertRaisesRegex(ValueError, "уже изменились"):
                self.manager.perform({"operation": "refresh_models", "provider_id": "fixture", "revision": current})
        self.assertEqual(set(self.registry.profiles()), before)
        self.assertEqual(self.registry.providers()["fixture"]["base_url"], self.base + "/changed")

    def test_inventory_failed_http_does_not_write_registry_or_expose_key(self):
        self.manager.perform({"operation": "save_provider", "id": "private-inventory",
                              "provider": self.provider(auth="key"), "api_key": "INVENTORY_SECRET"})
        before = self.registry.path.read_bytes()
        self.http_status = 401
        self.payload = {"error": "INVENTORY_SECRET reflected"}
        with self.assertRaises(NeuralError) as error:
            self.manager.perform({"operation": "refresh_models", "provider_id": "private-inventory"})
        self.assertEqual(self.registry.path.read_bytes(), before)
        self.assertNotIn("INVENTORY_SECRET", str(error.exception))

    def test_inventory_refresh_uses_context_metadata_and_bounded_labels(self):
        self.save_provider()
        self.payload = {"data": [{"id": "context-model", "context_length": 262144}, {"id": "x" * 1024}]}
        result = self.manager.perform({"operation": "refresh_models", "provider_id": "fixture"})
        models = {profile["model_id"]: profile for profile in result["settings"]["profiles"].values()
                  if profile.get("provider_id") == "fixture"}
        self.assertEqual(models["context-model"]["context"], 262144)
        self.assertEqual(len(models["x" * 1024]["name"]), 200)


if __name__ == "__main__":
    unittest.main()
