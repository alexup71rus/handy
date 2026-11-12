"""Provider transport contracts exercised against an actual loopback server."""

import errno
import json
import os
import select
import socket
import ssl
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from folder_memory.embeddings import Embedder, EmbeddingError
from folder_memory.model_backend import create_model
from folder_memory.neural import ContextBudgetError, NeuralError, ResponseLengthError
from folder_memory.remote_model import OpenAIModel, _tls_context


class RemoteModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        (self.state / "token").write_text("HANDY_LOCAL_TOKEN")
        self.received = []
        self.status = 200
        self.response = {"choices": [{"message": {"role": "assistant", "content": "reply"},
                                      "finish_reason": "stop"}]}
        self.raw_response = None
        self.content_type = "application/json"
        self.location = None
        self.count_response = {"input_tokens": 37}
        self.entered, self.disconnected, self.release = (threading.Event() for _ in range(3))
        self.stall_stream = False
        self.threads = []
        fixture = self

        class Provider(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.received.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                self.close_connection = True
                if self.path == "/v1/count":
                    self.respond(200, json.dumps(fixture.count_response).encode())
                    return
                messages = body.get("messages", [])
                if messages and messages[0].get("content") == "stall":
                    if fixture.stall_stream:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n')
                        self.wfile.flush()
                    fixture.entered.set()
                    deadline = time.monotonic() + 5
                    while not fixture.release.is_set() and time.monotonic() < deadline:
                        ready, _, _ = select.select([self.connection], [], [], .02)
                        if ready:
                            try:
                                closed = self.connection.recv(1, socket.MSG_PEEK) == b""
                            except OSError:
                                closed = True
                            if closed:
                                fixture.disconnected.set()
                                return
                    if fixture.stall_stream:
                        return
                raw = fixture.raw_response
                if raw is None:
                    raw = json.dumps(fixture.response).encode()
                try:
                    self.respond(fixture.status, raw)
                except OSError:
                    fixture.disconnected.set()

            def respond(self, status, raw):
                self.send_response(status)
                self.send_header("Content-Type", fixture.content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                if fixture.location:
                    self.send_header("Location", fixture.location)
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.serving = threading.Thread(target=self.server.serve_forever,
                                        kwargs={"poll_interval": .01}, daemon=True)
        self.serving.start()
        self.config = {"enabled": True, "backend": "openai", "name": "Provider display",
                       "base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
                       "model_id": "actual/provider-model", "context": 32768,
                       "api_key_env": "HANDY_TEST_PROVIDER_KEY"}
        self.environment = patch.dict(os.environ, {"HANDY_TEST_PROVIDER_KEY": "PROVIDER_SECRET"})
        self.environment.start()
        self.model = OpenAIModel(self.state, config=self.config)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.release.set()
        self.model.shutdown()
        for thread in self.threads:
            thread.join(2)
        self.server.shutdown()
        self.server.server_close()
        self.serving.join(1)
        self.environment.stop()

    @staticmethod
    def body(content="hello", **options):
        return {"model": "memory-engine", "messages": [{"role": "user", "content": content}],
                "max_tokens": 64, **options}

    def call(self, body=None):
        with self.model.chat_completion(body or self.body()) as response:
            return response.status, response.read()

    def start_stalled(self, *, cancelled=None, wait_for_provider=True):
        result = {"done": threading.Event(), "streaming": threading.Event(),
                  "started": threading.Event()}

        def run():
            try:
                result["started"].set()
                with self.model.chat_completion(self.body("stall"), cancelled=cancelled) as response:
                    if self.stall_stream:
                        result["chunk"] = response.read1(4096)
                        result["streaming"].set()
                    result["response"] = response.read()
            except Exception as error:
                result["error"] = error
            finally:
                result["done"].set()

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        if wait_for_provider:
            self.assertTrue(self.entered.wait(1), "Provider did not receive the request")
        return result

    def test_native_tools_images_and_provider_model_are_preserved(self):
        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        tool_call = {"id": "call-7", "type": "function",
                     "function": {"name": "lookup", "arguments": '{"value":7}'}}
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
            {"role": "assistant", "content": None, "tool_calls": [tool_call]},
            {"role": "tool", "tool_call_id": "call-7", "content": "found"}]
        self.response = {"choices": [{"message": {"role": "assistant", "tool_calls": [tool_call]},
                                      "finish_reason": "tool_calls"}]}
        body = {**self.body(), "messages": messages, "tools": tools, "tool_choice": "auto",
                "parallel_tool_calls": False, "temperature": .2, "top_p": .8,
                "top_k": 40, "repeat_penalty": 1.1, "cache_prompt": True,
                "chat_template_kwargs": {"enable_thinking": False}}
        original = json.loads(json.dumps(body))
        status, raw = self.call(body)
        request = self.received[0]
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), self.response)
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["body"]["model"], "actual/provider-model")
        self.assertEqual(request["body"]["messages"], messages)
        self.assertEqual(request["body"]["tools"], tools)
        self.assertEqual(request["body"]["temperature"], .2)
        self.assertEqual(request["body"]["top_p"], .8)
        self.assertFalse(request["body"]["parallel_tool_calls"])
        for key in ("top_k", "repeat_penalty", "cache_prompt", "chat_template_kwargs"):
            self.assertNotIn(key, request["body"])
        self.assertEqual(request["headers"]["Authorization"], "Bearer PROVIDER_SECRET")
        self.assertNotIn("HANDY_LOCAL_TOKEN", json.dumps(request))
        self.assertEqual(body, original, "Provider conversion mutated the caller request")

    def test_native_stream_is_not_parsed_or_rewritten(self):
        self.content_type = "text/event-stream"
        self.raw_response = (b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1",'
                             b'"type":"function","function":{"name":"read","arguments":"{}"}}]}}]}\n\n'
                             b'data: [DONE]\n\n')
        status, raw = self.call(self.body(stream=True, stream_options={"include_usage": True}))
        self.assertEqual((status, raw), (200, self.raw_response))
        self.assertTrue(self.received[0]["body"]["stream"])
        self.assertEqual(self.received[0]["body"]["stream_options"], {"include_usage": True})

    def test_embeddings_use_openai_format_and_configured_model(self):
        self.model.config.update(embedding=True, dimensions=3, send_dimensions=True)
        self.response = {"data": [{"index": 0, "embedding": [1., 2., 3.]}]}
        result = self.model._generate("/v1/embeddings", {
            "model": "Display embedding name", "input": ["passage: example"],
            "encoding_format": "float", "truncate": False})
        self.assertEqual(result, self.response)
        self.assertEqual(self.received[0]["path"], "/v1/embeddings")
        self.assertEqual(self.received[0]["body"], {
            "model": "actual/provider-model", "input": ["passage: example"],
            "encoding_format": "float", "dimensions": 3})

    def test_provider_api_base_can_use_a_custom_prefix_or_bare_server(self):
        for base_path, expected in (("/api/v1", "/api/v1/chat/completions"),
                                    ("/v1beta/openai", "/v1beta/openai/chat/completions"),
                                    ("", "/v1/chat/completions")):
            with self.subTest(base_path=base_path):
                self.model.config["base_url"] = f"http://127.0.0.1:{self.server.server_port}" + base_path
                self.assertEqual(self.call()[0], 200)
                self.assertEqual(self.received[-1]["path"], expected)

    def test_factory_remote_embedder_bounds_unicode_and_normalizes_provider_index_order(self):
        embedding = {**self.config, "embedding": True, "context": 128, "dimensions": 3,
                     "query_prefix": "Запрос: ", "passage_prefix": "Документ: "}
        (self.state / "neural.json").write_text(json.dumps({"embedding": embedding}))
        with patch("subprocess.Popen", side_effect=AssertionError("External encoders cannot spawn workers")):
            model = create_model(self.state, profile="embedding")
            self.addCleanup(model.shutdown)
            self.assertIsInstance(model, OpenAIModel)
            encoder = Embedder(model)
            self.assertTrue(encoder.enabled)
            self.response = {"data": [{"index": 1, "embedding": [0., 0., 5.]},
                                      {"index": 0, "embedding": [3., 4., 0.]}]}
            source = "Привет🙂 Проверяем Unicode 🚀 " * 200
            vectors = encoder.encode([source, "Другой текст🙂" * 200], query=True)
            self.assertEqual(vectors, [[.6, .8, 0.], [0., 0., 1.]])
            request = self.received[-1]
            self.assertEqual(request["path"], "/v1/embeddings")
            self.assertEqual(request["body"]["model"], embedding["model_id"])
            for value in request["body"]["input"]:
                self.assertIsInstance(value, str)
                self.assertTrue(value.startswith("Запрос: "))
                self.assertLessEqual(len(value.encode("utf-8")), 112)
                self.assertNotIn("\ufffd", value)
            self.assertLess(len(request["body"]["input"][0]), len(source))
            self.response = {"data": [{"index": 0, "embedding": [1., 0., 0.]}]}
            self.assertEqual(encoder.encode([source]), [[1., 0., 0.]])
            self.assertTrue(self.received[-1]["body"]["input"][0].startswith("Документ: "))
            self.assertTrue(all(item["path"] == "/v1/embeddings" for item in self.received))
            self.response = {"data": [{"index": 1, "embedding": [1., 0., 0.]}]}
            with self.assertRaisesRegex(EmbeddingError, "count/order"):
                encoder.encode(["wrong provider index"])
            self.assertIsNone(model.process)

    def test_remote_encoder_fingerprint_tracks_endpoint_model_and_policy_without_keys(self):
        config = {**self.config, "embedding": True, "context": 128, "dimensions": 3,
                  "query_prefix": "", "passage_prefix": ""}

        def fingerprint(**changes):
            model = OpenAIModel(self.state, config={**config, **changes})
            self.addCleanup(model.shutdown)
            return Embedder(model).fingerprint

        original = fingerprint()
        with patch.dict(os.environ, {"HANDY_TEST_PROVIDER_KEY": "ROTATED_PROVIDER_SECRET"}):
            self.assertEqual(fingerprint(), original)
        key = self.state / "another-provider.key"
        key.write_text("FILE_PROVIDER_SECRET")
        key.chmod(0o600)
        self.assertEqual(fingerprint(api_key_env=None, api_key_file=str(key)), original)
        self.assertNotEqual(fingerprint(base_url=self.config["base_url"] + "/another-api"), original)
        self.assertNotEqual(fingerprint(model_id="a/different-encoder"), original)
        self.assertNotEqual(fingerprint(query_prefix="query: "), original)
        self.assertNotEqual(fingerprint(dimensions=4), original)
        self.assertEqual(self.received, [], "Fingerprinting must not contact a provider")

    def test_completion_helpers_keep_text_tool_and_length_validation(self):
        self.assertEqual(self.model.complete_text([{"role": "user", "content": "hi"}], 50), "reply")
        self.response = {"choices": [{"message": {"role": "assistant", "tool_calls": [{
            "id": "call", "type": "function", "function": {"name": "lookup", "arguments": '{"key":5}'}}]},
            "finish_reason": "tool_calls"}]}
        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        self.assertEqual(self.model.complete_tool([], tools), ("lookup", {"key": 5}))
        self.response["choices"][0]["finish_reason"] = "length"
        with self.assertRaises(ResponseLengthError):
            self.model.complete_tool([], tools)

    def test_estimated_context_is_explicit_and_oversize_never_reaches_provider(self):
        body = self.body("Привет")
        result = self.model.input_tokens(body)
        self.assertTrue(result["estimated"])
        self.assertEqual(result["token_count_method"], "utf8_upper_bound")
        self.assertEqual(result["context_size"], 32768)
        self.assertGreaterEqual(result["input_tokens"], len(json.dumps(body, ensure_ascii=False).encode()))
        self.assertEqual(self.received, [])
        self.model.config["context"] = 1000
        with self.assertRaises(ContextBudgetError):
            self.call()
        self.assertEqual(self.received, [])

    def test_optional_exact_count_is_used_for_guard_and_context(self):
        self.model.config["token_count_path"] = "/count"
        self.assertEqual(self.model.input_tokens(self.body()), {
            "input_tokens": 37, "context_size": 32768, "estimated": False,
            "token_count_method": "provider_endpoint"})
        self.call()
        self.assertEqual([item["path"] for item in self.received],
                         ["/v1/count", "/v1/count", "/v1/chat/completions"])
        self.assertTrue(all(item["body"]["model"] == "actual/provider-model" for item in self.received))
        self.model.config["context"] = 50
        with self.assertRaises(ContextBudgetError):
            self.call()
        self.assertEqual(self.received[-1]["path"], "/v1/count")
        for invalid in (True, 0, -1, "37", None):
            with self.subTest(invalid=invalid):
                self.count_response = {"input_tokens": invalid}
                with self.assertRaisesRegex(NeuralError, "размер запроса"):
                    self.model.input_tokens(self.body())

    def test_invalid_endpoint_credentials_and_public_plain_http_are_rejected(self):
        for base in ("https://user:SECRET@example.com/v1", "https://host/v1?api_key=SECRET",
                     "https://host/v1#SECRET", "file:///tmp/key", "http://8.8.8.8/v1",
                     "https://host/v1\r\nAuthorization:SECRET", "https://host\\@evil/v1"):
            with self.subTest(base=base):
                self.model.config["base_url"] = base
                self.assertFalse(self.model.enabled)
                with self.assertRaises(NeuralError) as caught:
                    self.model._start()
                self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(self.received, [])
        self.model.config["base_url"] = self.config["base_url"]
        for path in ("https://evil/v1/count", "//evil/count", "/count?key=SECRET", "/count#SECRET"):
            self.model.config["token_count_path"] = path
            with self.assertRaises(NeuralError):
                self.model._start()

    def test_plain_http_hostname_cannot_resolve_to_public_server(self):
        self.model.config["base_url"] = "http://home.example/v1"
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 80))]
        with patch("folder_memory.remote_model.socket.getaddrinfo", return_value=addresses), \
                self.assertRaisesRegex(NeuralError, "HTTPS"):
            self.call()
        self.assertEqual(self.received, [])

    def test_plain_http_connects_to_the_validated_address_without_resolving_again(self):
        self.model.config["base_url"] = f"http://home.example:{self.server.server_port}/v1"
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                      "", ("127.0.0.1", self.server.server_port))]
        with patch("folder_memory.remote_model.socket.getaddrinfo", return_value=addresses) as resolve:
            self.assertEqual(self.call()[0], 200)
        resolve.assert_called_once_with("home.example", self.server.server_port, type=socket.SOCK_STREAM)
        self.assertEqual(self.received[0]["headers"]["Host"], f"home.example:{self.server.server_port}")

    def test_proxy_environment_is_ignored_and_redirect_does_not_send_another_request(self):
        # A proxy on the same fixture would be visible as an absolute request path.
        proxy = f"http://127.0.0.1:{self.server.server_port}/proxy"
        with patch.dict(os.environ, {"http_proxy": proxy, "HTTP_PROXY": proxy,
                                     "HTTPS_PROXY": proxy, "ALL_PROXY": proxy, "NO_PROXY": ""}):
            self.call()
        self.assertEqual([item["path"] for item in self.received], ["/v1/chat/completions"])
        self.status, self.location = 307, f"http://127.0.0.1:{self.server.server_port}/leak"
        self.raw_response = b"redirect PROVIDER_SECRET HANDY_LOCAL_TOKEN private prompt"
        with self.assertRaisesRegex(NeuralError, "перенаправление") as caught:
            self.call()
        self.assertEqual(len(self.received), 2)
        self.assertNotIn("PROVIDER_SECRET", str(caught.exception))
        self.assertNotIn("HANDY_LOCAL_TOKEN", str(caught.exception))
        self.assertNotIn("private prompt", str(caught.exception))

    def test_provider_errors_and_bad_json_do_not_echo_secrets(self):
        self.raw_response = b"PROVIDER_SECRET HANDY_LOCAL_TOKEN private prompt"
        for status in (400, 401, 429, 500):
            with self.subTest(status=status):
                self.status = status
                with self.assertRaises(NeuralError) as caught:
                    self.call()
                message = str(caught.exception)
                self.assertIn(str(status), message)
                for secret in ("PROVIDER_SECRET", "HANDY_LOCAL_TOKEN", "private prompt"):
                    self.assertNotIn(secret, message)
        self.status = 200
        with self.assertRaisesRegex(NeuralError, "некорректный JSON") as caught:
            self.model._request("/v1/embeddings", {"input": ["private prompt"]})
        self.assertNotIn("PROVIDER_SECRET", str(caught.exception))

    def test_auth_is_optional_and_key_files_are_private_provider_files(self):
        self.model.config.pop("api_key_env")
        self.call()
        self.assertNotIn("Authorization", self.received[-1]["headers"])
        key = self.state / "provider.key"
        key.write_text("FILE_PROVIDER_KEY\n")
        key.chmod(0o600)
        self.model.config["api_key_file"] = str(key)
        self.call()
        self.assertEqual(self.received[-1]["headers"]["Authorization"], "Bearer FILE_PROVIDER_KEY")
        key.chmod(0o644)
        self.assertFalse(self.model.enabled)
        with self.assertRaisesRegex(NeuralError, "права 600"):
            self.call()
        self.model.config["api_key_file"] = str(self.state / "token")
        with self.assertRaisesRegex(NeuralError, "токен Handy"):
            self.call()
        self.model.config.pop("api_key_file")
        self.model.config["api_key_env"] = "HANDY_TEST_ABSENT_PROVIDER_KEY"
        with patch.dict(os.environ):
            os.environ.pop("HANDY_TEST_ABSENT_PROVIDER_KEY", None)
            self.assertFalse(self.model.enabled)
            with self.assertRaisesRegex(NeuralError, "Ключ провайдера недоступен"):
                self.call()

    def test_cancel_before_headers_closes_only_the_current_socket(self):
        cancelled = threading.Event()
        stalled = self.start_stalled(cancelled=cancelled)
        cancelled.set()
        self.assertTrue(self.disconnected.wait(1.5))
        self.assertTrue(stalled["done"].wait(1))
        self.assertIsInstance(stalled.get("error"), NeuralError)
        self.assertIn("отменён", str(stalled["error"]))
        self.assertIsNone(self.model.process)
        self.assertEqual(self.call()[0], 200)
        self.assertFalse(any(thread.is_alive() and thread.name == "handy-remote-cancel"
                             for thread in threading.enumerate()))

    def test_cancelled_dns_releases_the_model_lock_and_bounds_abandoned_resolvers(self):
        release = threading.Event()
        self.addCleanup(release.set)
        started = [threading.Event() for _ in range(4)]
        resolutions = []
        dns_threads = []
        guard = threading.Lock()

        def resolve(host, port, **_):
            with guard:
                index = len(resolutions)
                resolutions.append(host)
                dns_threads.append(threading.current_thread())
                if index < len(started):
                    started[index].set()
            release.wait(5)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     "", ("127.0.0.1", port))]

        try:
            with patch("folder_memory.remote_model.socket.getaddrinfo", side_effect=resolve):
                self.model.config["base_url"] = f"http://home.example:{self.server.server_port}/v1"
                for index in range(7):
                    cancelled = threading.Event()
                    pending = self.start_stalled(cancelled=cancelled, wait_for_provider=False)
                    self.assertTrue(pending["started"].wait(1))
                    if index < len(started):
                        self.assertTrue(started[index].wait(1))
                    else:
                        # Let this request wait for an occupied resolver slot.
                        self.assertFalse(pending["done"].wait(.08))
                    cancelled.set()
                    self.assertTrue(pending["done"].wait(.7), "Cancellation kept the model lock")
                    self.assertIn("отменён", str(pending.get("error")))
                self.assertEqual(len(resolutions), 4, "Abandoned DNS calls must be globally bounded")
                self.assertEqual(self.received, [])
                self.assertEqual(self.model._sockets, set())
                self.model.config["base_url"] = self.config["base_url"]
                self.assertEqual(self.call()[0], 200, "Cancelled DNS kept a later request blocked")
        finally:
            release.set()
            for thread in dns_threads:
                thread.join(1)
        self.assertFalse(any(thread.is_alive() for thread in dns_threads))
        self.assertEqual(len(self.received), 1, "A late DNS result started an abandoned request")

    def test_dns_shutdown_and_deadline_return_without_waiting_for_the_resolver(self):
        for mode in ("shutdown", "deadline"):
            with self.subTest(mode=mode):
                release, entered = threading.Event(), threading.Event()
                dns_threads = []
                self.model = OpenAIModel(self.state, config={
                    **self.config, "base_url": f"http://home.example:{self.server.server_port}/v1"})

                def resolve(host, port, **_):
                    dns_threads.append(threading.current_thread())
                    entered.set()
                    release.wait(5)
                    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                             "", ("127.0.0.1", port))]

                try:
                    with patch("folder_memory.remote_model.socket.getaddrinfo", side_effect=resolve):
                        if mode == "shutdown":
                            pending = self.start_stalled(wait_for_provider=False)
                            self.assertTrue(entered.wait(1))
                            self.model.shutdown()
                            self.assertTrue(pending["done"].wait(.7))
                            self.assertIn("остановлен", str(pending.get("error")))
                        else:
                            before = time.monotonic()
                            with self.assertRaisesRegex(NeuralError, "Истекло время"):
                                self.model._request("/v1/embeddings", {"input": ["hi"]}, timeout=.12)
                            self.assertLess(time.monotonic() - before, .7)
                finally:
                    release.set()
                    for thread in dns_threads:
                        thread.join(1)
                    self.model.shutdown()
                self.assertEqual(self.received, [])

    def test_tcp_connect_cancel_shutdown_and_deadline_close_the_pending_socket(self):
        real_socket, real_select = socket.socket, select.select
        for mode in ("cancel", "shutdown", "deadline"):
            with self.subTest(mode=mode):
                self.entered.clear()
                pending_sockets = []
                self.model = OpenAIModel(self.state, config=self.config)
                fixture = self

                class PendingSocket:
                    def __init__(self, *args, **kwargs):
                        self.socket = real_socket(*args, **kwargs)
                        pending_sockets.append(self)

                    def __getattr__(self, name):
                        return getattr(self.socket, name)

                    def connect_ex(self, _):
                        fixture.entered.set()
                        return errno.EINPROGRESS

                def never_connected(read, write, exceptional, interval):
                    if any(upstream in pending_sockets for upstream in (*read, *write, *exceptional)):
                        threading.Event().wait(interval)
                        return [], [], []
                    return real_select(read, write, exceptional, interval)

                with patch("folder_memory.remote_model.socket.socket", PendingSocket), \
                        patch("folder_memory.remote_model.select.select", side_effect=never_connected):
                    if mode == "deadline":
                        with self.assertRaisesRegex(NeuralError, "Истекло время"):
                            self.model._request("/v1/embeddings", {"input": ["hi"]}, timeout=.12)
                    else:
                        cancelled = threading.Event()
                        pending = self.start_stalled(cancelled=cancelled)
                        self.model.shutdown() if mode == "shutdown" else cancelled.set()
                        self.assertTrue(pending["done"].wait(.7))
                        self.assertIn("остановлен" if mode == "shutdown" else "отменён",
                                      str(pending.get("error")))
                self.assertEqual(self.model._sockets, set())
                self.assertTrue(pending_sockets)
                self.assertTrue(all(upstream.fileno() == -1 for upstream in pending_sockets))
                self.assertEqual(self.received, [])
                if mode != "shutdown":
                    self.assertEqual(self.call()[0], 200)
                    self.received.clear()

    def test_stalled_real_tls_handshake_is_cancelled_and_closed(self):
        for mode in ("cancel", "shutdown"):
            with self.subTest(mode=mode):
                self.entered.clear()
                self.disconnected.clear()
                listener = socket.socket()
                self.addCleanup(listener.close)
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                listener.settimeout(1)
                self.model = OpenAIModel(self.state, config={
                    **self.config, "base_url": f"https://127.0.0.1:{listener.getsockname()[1]}/v1"})

                def stall_handshake():
                    try:
                        upstream, _ = listener.accept()
                        with upstream:
                            upstream.settimeout(1.5)
                            if upstream.recv(4096):
                                self.entered.set()  # A real TLS ClientHello arrived.
                            while upstream.recv(4096):
                                pass
                            self.disconnected.set()
                    except OSError:
                        pass

                server = threading.Thread(target=stall_handshake, daemon=True)
                self.threads.append(server)
                server.start()
                cancelled = threading.Event()
                pending = self.start_stalled(cancelled=cancelled)
                self.model.shutdown() if mode == "shutdown" else cancelled.set()
                self.assertTrue(pending["done"].wait(.7))
                self.assertTrue(self.disconnected.wait(.7), "Cancelled TLS socket stayed open")
                self.assertIn("остановлен" if mode == "shutdown" else "отменён", str(pending.get("error")))
                self.assertEqual(self.model._sockets, set())
                self.assertEqual(self.received, [])
                server.join(1)
                listener.close()

    def test_https_requires_verified_certificates_and_hostnames(self):
        context = _tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_stream_cancel_and_shutdown_interrupt_reads(self):
        self.stall_stream = True
        cancelled = threading.Event()
        stalled = self.start_stalled(cancelled=cancelled)
        self.assertTrue(stalled["streaming"].wait(1))
        self.assertIn(b"first", stalled["chunk"])
        cancelled.set()
        self.assertTrue(self.disconnected.wait(1.5))
        self.assertTrue(stalled["done"].wait(1))
        self.assertIsNone(self.model.process)
        # A stopped adapter is local state; it must not send a provider request.
        self.model.shutdown()
        count = len(self.received)
        with self.assertRaisesRegex(NeuralError, "остановлен"):
            self.call()
        self.assertEqual(len(self.received), count)

    def test_shutdown_aborts_inflight_non_cancelled_request_and_never_spawns(self):
        with patch("subprocess.Popen", side_effect=AssertionError("Remote services are unmanaged")):
            stalled = self.start_stalled()
            self.model.reap_idle()
            self.assertFalse(stalled["done"].is_set())
            self.model.shutdown()
            self.assertTrue(self.disconnected.wait(1.5))
            self.assertTrue(stalled["done"].wait(1))
        self.assertIsNone(self.model.process)
        self.assertIsInstance(stalled.get("error"), NeuralError)

    def test_already_cancelled_or_queued_request_never_reaches_upstream(self):
        first_cancelled = threading.Event()
        first_cancelled.set()
        with self.assertRaisesRegex(NeuralError, "отменён"):
            with self.model.chat_completion(self.body(), cancelled=first_cancelled):
                self.fail("Cancelled call reached the provider")
        self.assertEqual(self.received, [])
        first = self.start_stalled()
        queued_cancelled = threading.Event()
        queued = {"done": threading.Event()}

        def run_queued():
            try:
                with self.model.chat_completion(self.body("queued"), cancelled=queued_cancelled):
                    queued["reached"] = True
            except NeuralError as error:
                queued["error"] = error
            finally:
                queued["done"].set()

        thread = threading.Thread(target=run_queued, daemon=True)
        self.threads.append(thread)
        thread.start()
        queued_cancelled.set()
        self.assertTrue(queued["done"].wait(1))
        self.assertIn("отменён", str(queued["error"]))
        self.assertNotIn("reached", queued)
        self.assertFalse(self.disconnected.is_set())
        self.assertEqual(len(self.received), 1)
        self.release.set()
        self.assertTrue(first["done"].wait(1))
        self.assertEqual(self.call()[0], 200)


    def test_stats_are_passive_and_do_not_claim_local_process_ownership(self):
        with patch("subprocess.Popen", side_effect=AssertionError("Stats cannot launch providers")):
            stats = self.model.stats()
            self.model.reap_idle()
            self.model.close()
        self.assertTrue(stats["enabled"])
        self.assertTrue(stats["unmanaged"])
        self.assertEqual(stats["backend"], "openai")
        self.assertIsNone(stats["pid"])
        self.assertEqual(stats["worker_pids"], [])
        self.assertEqual(self.received, [])
        self.assertNotIn("PROVIDER_SECRET", json.dumps(stats))
        self.assertEqual(self.call()[0], 200, "Closing transport should not disable a provider")


if __name__ == "__main__":
    unittest.main()
