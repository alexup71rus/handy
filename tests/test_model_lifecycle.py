"""Real tiny child processes exercise lifecycle without loading model weights."""

import json
import io
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from folder_memory.neural import LocalModel, NeuralError


class ModelLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve()
        self.binary = self.state / "fake-llama"
        self.binary.write_text("#!" + sys.executable + "\nimport time\ntime.sleep(30)\n")
        self.binary.chmod(0o700)
        self.weights = self.state / "fixture.gguf"
        self.weights.write_bytes(b"fixture, not model weights")
        self.processes = []
        self.addCleanup(self.stop_children)
        real_popen = subprocess.Popen

        def record(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            self.processes.append(process)
            return process

        mocked = patch("folder_memory.neural.subprocess.Popen", side_effect=record)
        mocked.start()
        self.addCleanup(mocked.stop)

    def stop_children(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)

    def model(self, **config):
        model = LocalModel(self.state)
        model.config = {"enabled": True, "binary": str(self.binary), "model": str(self.weights), **config}
        self.addCleanup(model.shutdown)
        return model

    def test_idle_unload_allows_restart_but_terminal_shutdown_does_not(self):
        model = self.model()
        with patch.object(model, "_request", return_value={"status": "ok"}):
            model._start()
            first = model.process
            model.last_used = time.monotonic() - 200
            model.reap_idle()
            self.assertIsNotNone(first.poll())
            model._start()
            second = model.process
            self.assertIsNot(first, second)
            model.shutdown()
            self.assertIsNotNone(second.poll())
            with self.assertRaises(NeuralError):
                model._start()
        self.assertEqual(len(self.processes), 2)

    def test_malformed_health_reaps_the_started_child(self):
        model = self.model()
        with patch.object(model, "_request", return_value=[]):
            with self.assertRaises(NeuralError):
                model._start()
        self.assertEqual(len(self.processes), 1)
        self.assertIsNotNone(self.processes[0].poll())
        self.assertIsNone(model.process)

    def test_shutdown_interrupts_an_in_progress_start(self):
        model = self.model()
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        errors = []

        def health(*args, **kwargs):
            entered.set()
            release.wait(2)
            return {"status": "ok"}

        def start():
            try:
                model._start()
            except NeuralError as error:
                errors.append(error)

        def shutdown():
            model.shutdown()
            stopped.set()

        with patch.object(model, "_request", side_effect=health):
            request = threading.Thread(target=start, daemon=True)
            request.start()
            self.assertTrue(entered.wait(2))
            cleanup = threading.Thread(target=shutdown, daemon=True)
            cleanup.start()
            self.assertTrue(model.shutdown_event.wait(1))
            release.set()
            request.join(3)
            cleanup.join(3)
        self.assertFalse(request.is_alive())
        self.assertTrue(stopped.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsNone(model.process)
        self.assertIsNotNone(self.processes[0].poll())

    def test_exclusive_profiles_share_lock_and_never_overlap_workers(self):
        first = self.model(exclusive=True)
        second = self.model()
        self.assertIs(first.lock, second.lock)
        other = LocalModel(self.state / "different-app")
        self.assertIsNot(first.lock, other.lock)
        entered, release, second_called, second_done = [threading.Event() for _ in range(4)]
        errors = []

        def response(model, path, body=None, **kwargs):
            if path == "/health":
                self.assertLessEqual(sum(process.poll() is None for process in self.processes), 1)
                return {"status": "ok"}
            if path.endswith("/input_tokens"):
                return {"input_tokens": 16}
            if model is first:
                entered.set()
                release.wait(2)
            return {"choices": [{"message": {"content": json.dumps({"ok": "yes"})}}]}

        def complete(model, done=None):
            try:
                if done:
                    second_called.set()
                model.complete([], {"type": "object"})
            except BaseException as error:
                errors.append(error)
            finally:
                if done:
                    done.set()

        with patch.object(first, "_request", side_effect=lambda *a, **kw: response(first, *a, **kw)), \
             patch.object(second, "_request", side_effect=lambda *a, **kw: response(second, *a, **kw)):
            a = threading.Thread(target=complete, args=(first,), daemon=True)
            b = threading.Thread(target=complete, args=(second, second_done), daemon=True)
            a.start()
            self.assertTrue(entered.wait(2))
            first_child = first.process
            b.start()
            self.assertTrue(second_called.wait(1))
            self.assertFalse(second_done.wait(0.1), "Concurrent inference bypassed the per-App model lock")
            release.set()
            a.join(3)
            b.join(3)
            self.assertFalse(a.is_alive() or b.is_alive())
            self.assertEqual(errors, [])
            self.assertIsNotNone(first_child.poll())
            self.assertIsNone(first.process)
            second_child = second.process
            self.assertIsNone(second_child.poll())
            first._start()
            self.assertIsNotNone(second_child.poll())
            self.assertIsNone(second.process)
        self.assertEqual(len(self.processes), 3)

    def test_configured_batch_is_passed_and_invalid_batch_spawns_nothing(self):
        model = self.model(batch_tokens=2048, micro_batch_tokens=2048)
        with patch.object(model, "_request", return_value={"status": "ok"}):
            model._start()
            args = model.process.args
            self.assertEqual(args[args.index("-b") + 1], "2048")
            self.assertEqual(args[args.index("-ub") + 1], "2048")
            model.close()
            model.config["micro_batch_tokens"] = 4097
            with self.assertRaises(NeuralError):
                model._start()
        self.assertEqual(len(self.processes), 1)

    def test_optional_main_cache_settings_preserve_defaults_and_pass_exact_flags(self):
        model = self.model(context=65536)
        with patch.object(model, "_request", return_value={"status": "ok"}):
            model._start()
            for flag in ("-ctk", "-ctv", "-fa"):
                self.assertNotIn(flag, model.process.args)
            model.close()
            for key_type, value_type, attention in (
                ("q8_0", "q8_0", "on"),
                ("f16", "f16", "auto"),
                ("q8_0", "f16", "off"),
            ):
                with self.subTest(key_type=key_type, value_type=value_type, attention=attention):
                    model.config.update(cache_type_k=key_type, cache_type_v=value_type, flash_attention=attention)
                    model._start()
                    args = model.process.args
                    for flag, value in (("-ctk", key_type), ("-ctv", value_type), ("-fa", attention), ("-c", "65536")):
                        self.assertEqual(args[args.index(flag) + 1], value)
                        self.assertEqual(args.count(flag), 1)
                    model.close()

    def test_invalid_main_cache_settings_spawn_nothing(self):
        invalid = {
            "cache_type_k": ("q4_0", "", None, True, []),
            "cache_type_v": ("Q8_0", "f16 --fit on", None, 8, {}),
            "flash_attention": ("yes", "ON", None, False, []),
        }
        model = self.model()
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    model.config[key] = value
                    with self.assertRaisesRegex(NeuralError, key):
                        model._start()
                    self.assertIsNone(model.process)
                del model.config[key]
        self.assertEqual(self.processes, [])

    def test_main_vision_projector_is_passed_and_invalid_configuration_spawns_nothing(self):
        projector = self.state / 'projector.gguf'
        projector.write_bytes(b'fixture projector')
        model = self.model(mmproj=str(projector), image_max_tokens=512)
        with patch.object(model, '_request', return_value={'status': 'ok'}):
            model._start()
            args = model.process.args
            self.assertEqual(args[args.index('--mmproj') + 1], str(projector))
            self.assertEqual(args[args.index('--image-max-tokens') + 1], '512')
            model.close()
            for invalid in ({'mmproj': str(self.state / 'missing.gguf')}, {'mmproj': None},
                            {'embedding': True}, {'image_max_tokens': 0}, {'image_max_tokens': True}):
                with self.subTest(config=invalid):
                    model.config.update(mmproj=str(projector), image_max_tokens=512, embedding=False)
                    model.config.update(invalid)
                    with self.assertRaises(NeuralError):
                        model._start()
        self.assertEqual(len(self.processes), 1)

    def test_cache_settings_are_not_forwarded_to_embedding_or_named_profiles(self):
        settings = {"cache_type_k": "q8_0", "cache_type_v": "q8_0", "flash_attention": "on"}
        encoder = self.model(embedding=True, **settings)
        named = self.model(**settings)
        named.profile = "specialized"
        for model in (encoder, named):
            with self.subTest(profile=model.profile, embedding=model.config.get("embedding")):
                with patch.object(model, "_request", return_value={"status": "ok"}):
                    model._start()
                for flag in ("-ctk", "-ctv", "-fa"):
                    self.assertNotIn(flag, model.process.args)
                model.close()

    def test_idle_policy_is_inherited_and_can_be_overridden_per_profile(self):
        (self.state / "neural.json").write_text(json.dumps({
            "idle_sleep_seconds": 15, "idle_terminate_seconds": 23,
            "translation": {"idle_sleep_seconds": -1},
            "embedding": {"idle_terminate_seconds": 5},
        }))
        self.assertEqual(LocalModel(self.state)._idle_seconds(), (15, 23))
        self.assertEqual(LocalModel(self.state, profile="translation")._idle_seconds(), (-1, 23))
        self.assertEqual(LocalModel(self.state, profile="embedding")._idle_seconds(), (15, 5))

    def test_custom_idle_threshold_controls_the_worker_and_its_reaping(self):
        model = self.model(idle_sleep_seconds=3, idle_terminate_seconds=9)
        with patch.object(model, "_request", return_value={"status": "ok"}):
            model._start()
        args = model.process.args
        self.assertEqual(args[args.index("--sleep-idle-seconds") + 1], "3")
        model.last_used = 100
        with patch("folder_memory.neural.time.monotonic", return_value=108.9):
            model.reap_idle()
        self.assertIsNotNone(model.process)
        with patch("folder_memory.neural.time.monotonic", return_value=109):
            model.reap_idle()
        self.assertIsNone(model.process)
        model.config["idle_sleep_seconds"] = 0
        with self.assertRaisesRegex(NeuralError, "idle_sleep_seconds"):
            model._start()
        self.assertEqual(len(self.processes), 1)

    def test_nested_embedding_profile_does_not_enable_embedding_mode_for_language(self):
        language=self.model(embedding={'enabled':True})
        encoder=self.model(embedding=True)
        with patch.object(language,'_request',return_value={'status':'ok'}),patch.object(encoder,'_request',return_value={'status':'ok'}):
            language._start()
            self.assertNotIn('--embedding',language.process.args)
            self.assertEqual(language.process.args[language.process.args.index('-b')+1],'128')
            self.assertTrue((self.state/'llama.log').exists())
            encoder._start()
            self.assertIn('--embedding',encoder.process.args)
            self.assertTrue((self.state/'embedding-llama.log').exists())

    def test_missing_token_is_a_model_error(self):
        with self.assertRaises(NeuralError):
            self.model()._request("/health")

    def test_model_requests_bypass_proxies_and_reject_redirects(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                self.server.received.append((self.path, self.headers.get("Authorization"), body))
                self.send_response(200)
                response = b'{"status":"ok"}'
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def do_GET(self):
                self.server.received.append((self.path, self.headers.get("Authorization"), b""))
                self.send_response(302)
                self.send_header("Location", self.server.redirect)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def listener():
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.received = []
            server.redirect = "http://127.0.0.1:1/unreachable"
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 1)
            self.addCleanup(server.shutdown)
            return server

        (self.state / "token").write_text("fixture-token")
        worker, unrelated = listener(), listener()
        proxy = f"http://127.0.0.1:{unrelated.server_port}"
        worker.redirect = proxy + "/redirected"
        model = self.model(port=worker.server_port)
        with patch.dict("os.environ", {"http_proxy": proxy, "no_proxy": ""}):
            self.assertEqual(model._request("/local-model", {"prompt": "private fixture"}), {"status": "ok"})
        with self.assertRaisesRegex(NeuralError, "HTTP 302"):
            model._request("/redirect")
        self.assertEqual(worker.received, [
            ("/local-model", "Bearer fixture-token", b'{"prompt": "private fixture"}'),
            ("/redirect", "Bearer fixture-token", b""),
        ])
        self.assertEqual(unrelated.received, [])

    def test_timed_out_generation_reaps_worker(self):
        (self.state / "token").write_text("fixture-token")
        model = self.model()
        with patch.object(model, "_request", return_value={"status": "ok"}):
            model._start()
            worker = model.process
        count = io.BytesIO(b'{"input_tokens": 20}')
        count.status = 200
        with patch("folder_memory.neural.http.client.HTTPConnection") as connection:
            connection.return_value.getresponse.side_effect = [count, TimeoutError()]
            with self.assertRaisesRegex(NeuralError, "Истекло время ответа"):
                model.complete_text([{"role": "user", "content": "Привет"}], 25)
        self.assertIsNotNone(worker.poll())
        self.assertIsNone(model.process)

    def test_http_rejection_reports_only_status_and_safe_reason(self):
        (self.state / "token").write_text("fixture token")
        model = self.model()
        for body, expected in [
            ('{"error":{"message":"SECRET_PROMPT exceeds context size n_ctx=4096"}}', "превышен размер контекста"),
            ('{"error":"SECRET_PROMPT input exceeds n_batch"}', "недостаточный размер batch"),
            ('{"error":"SECRET_PROMPT unknown private failure"}', "модель отклонила запрос"),
        ]:
            response = io.BytesIO(body.encode())
            response.status = 400
            with patch("folder_memory.neural.http.client.HTTPConnection") as connection:
                connection.return_value.getresponse.return_value = response
                with self.assertRaises(NeuralError) as raised:
                    model._request("/v1/chat/completions", {"private": "SECRET_PROMPT"})
                connection.return_value.close.assert_called_once()
            self.assertIn("HTTP 400", str(raised.exception))
            self.assertIn(expected, str(raised.exception))
            self.assertNotIn("SECRET_PROMPT", str(raised.exception))
            self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
