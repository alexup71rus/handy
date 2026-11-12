import concurrent.futures
import http.client
import json
import stat
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

from folder_memory.server import App, Handler, MODEL, Server, get_token


class TokenTests(unittest.TestCase):
    def test_parallel_start_publishes_one_complete_private_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                tokens = list(pool.map(get_token, [state] * 16))
            self.assertRegex(tokens[0], r"^[A-Za-z0-9_-]{43}$")
            self.assertEqual(set(tokens), {tokens[0]})
            self.assertEqual(list(state.iterdir()), [state / "token"])
            self.assertEqual(stat.S_IMODE((state / "token").stat().st_mode), 0o600)

    def test_empty_token_blocks_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "token").touch()
            with self.assertRaisesRegex(ValueError, "токен Handy повреждён"):
                App(state)


class TransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        app = App(root / "state")
        self.app = app
        self.addCleanup(app.shutdown)
        class Quiet(Handler):
            def log_message(self, *_):
                pass
        server = Server(("127.0.0.1", 0), Quiet)
        server.app = app
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.port = server.server_port
        self.headers = {"Authorization": "Bearer " + app.token}


    def worker(self, answer):
        requests = []

        class Fixture(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, self.headers, body))
                answer(self, body)

        server = Server(("127.0.0.1", 0), Fixture)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.app.neural_model.config["port"] = server.server_port
        start = patch.object(self.app.neural_model, "_start")
        start.start()
        self.addCleanup(start.stop)
        return requests

    def request_chat(self, body):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("POST", "/v1/chat/completions", json.dumps(body), {
            **self.headers, "Content-Type": "application/json", "x-caller-header": "private",
        })
        return connection.getresponse()

    def request_json(self, route, body, *, authorized=True):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers.update(self.headers)
        with closing(http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)) as connection:
            connection.request("POST", route, json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())

    def test_native_long_history_reaches_the_worker(self):
        def answer(worker, _):
            data = b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
            worker.send_response(200)
            worker.send_header("Content-Length", str(len(data)))
            worker.end_headers()
            worker.wfile.write(data)

        received = self.worker(answer)
        messages = [{"role": "system", "content": "Fixture instructions"}]
        for index in range(70):
            messages.extend([
                {"role": "user", "content": f"Заметка {index}. " + "Наблюдение о море. " * 20},
                {"role": "assistant", "content": f"Ответ {index}."},
            ])
        messages.append({"role": "user", "content": "О чём мы говорили в самом начале?"})
        body = {"model": MODEL, "messages": messages}
        self.assertEqual(self.request_json("/v1/chat/completions", body)[0], 200)
        self.assertEqual(received[0][2], body)

    def test_typed_artifact_uses_local_auth_and_legacy_runner_is_gone(self):
        body = {"tool": "artifact", "root": str(self.workspace), "session": "fixture",
                "arguments": {"kind": "timer", "options": {"seconds": 95}}}
        self.assertEqual(self.request_json("/api/tools", body, authorized=False)[0], 401)
        self.assertFalse((self.workspace / ".handy").exists())
        status, reply = self.request_json("/api/tools", body)
        self.assertEqual((status, reply["status"]), (200, "ok"))
        artifact = reply["data"]["artifact"]
        self.assertTrue(Path(artifact["path"]).is_file())
        self.assertEqual(json.loads(Path(artifact["data_path"]).read_text())["durationSeconds"], 95)
        for endpoint in ("/api/artifacts", "/api/native-tool", "/api/native-read-start", "/api/native-receipt"):
            self.assertEqual(self.request_json(endpoint, body)[0], 404)

    def test_native_stream_forwards_tools_context_and_upstream_frames(self):
        release = threading.Event()
        first = b'data: {"id":"worker-1","choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        rest = (b'data: {"id":"worker-1","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_live",'
                b'"type":"function","function":{"name":"skill","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":341,"completion_tokens":19,"total_tokens":360}}\n\n'
                b'data: [DONE]\n\n')

        def answer(worker, _):
            worker.send_response(200)
            worker.send_header("Content-Type", "text/event-stream")
            worker.send_header("Transfer-Encoding", "chunked")
            worker.end_headers()
            for chunk in (first, rest):
                worker.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                worker.wfile.flush()
                if chunk == first and not release.wait(4):
                    return
            worker.wfile.write(b"0\r\n\r\n")

        received = self.worker(answer)
        call = {"id": "call_prior", "type": "function", "function": {"name": "mcp_probe", "arguments": "{}"}}
        tools = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
                 for name in ("skill", "mcp_probe")]
        request = {"model": MODEL, "messages": [{"role": "system", "content": "Harness policy"},
                   {"role": "developer", "content": "Harness instructions"},
                   {"role": "user", "content": [{"type": "text", "text": "Probe the fixture."}]},
                   {"role": "assistant", "content": None, "tool_calls": [call]},
                   {"role": "tool", "tool_call_id": "call_prior", "content": "observed result"}],
                   "tools": tools, "tool_choice": "auto", "parallel_tool_calls": True, "stream": True,
                   "stream_options": {"include_usage": True}, "temperature": .3, "max_completion_tokens": 170}
        with patch("folder_memory.server.execute_tool", side_effect=AssertionError("native chat invoked Handy tools")):
            response = self.request_chat(request)
            self.addCleanup(release.set)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
            self.assertIsNone(response.getheader("Transfer-Encoding"))
            self.assertEqual(response.readline() + response.readline(), first)
            release.set()
            self.assertEqual(response.read(), rest)
        path, headers, body = received[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], self.headers["Authorization"])
        self.assertNotIn("x-caller-header", headers)
        self.assertEqual(body, request)

    def test_native_json_preserves_status_and_retries_rejected_evidence(self):
        success = b'{"id":"worker-2","choices":[{"message":{"role":"assistant","content":"ok"}}],"usage":{"total_tokens":42}}'
        failure = b'{"error":{"code":"invalid_request","message":"fixture rejected request"}}'

        def answer(worker, body):
            status, result = (422, failure) if body["messages"][0]["content"] == "extra" else (200, success)
            worker.close_connection = True
            worker.send_response(status)
            worker.send_header("Content-Type", "application/json")
            worker.send_header("Content-Length", str(len(result)))
            worker.end_headers()
            worker.wfile.write(result)

        received = self.worker(answer)
        original = {"model": MODEL, "messages": [{"role": "user", "content": "raw"}]}
        augmented = {**original, "messages": [{"role": "system", "content": "extra"}, *original["messages"]]}
        with patch("folder_memory.chat_evidence.augment_chat", return_value=augmented):
            response = self.request_chat(original)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), success)
        response = self.request_chat(augmented)
        self.assertEqual(response.status, 422)
        self.assertEqual(response.read(), failure)
        self.assertEqual([body for _, _, body in received], [augmented, original, augmented])


    def test_rejects_malformed_authorization_and_browser_origin(self):
        for headers, expected in [(self.headers, 200), ({"Authorization": "Bearer café"}, 404),
                                  ({**self.headers, "Origin": ""}, 404)]:
            with self.subTest(expected=expected):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
                try:
                    connection.request("GET", "/v1/models", headers=headers)
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                finally:
                    connection.close()

    def test_rejects_ambiguous_request_framing(self):
        for extra in (("Content-Length", "3"), ("Transfer-Encoding", "chunked")):
            with self.subTest(header=extra[0]):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
                try:
                    connection.putrequest("POST", "/api/query")
                    connection.putheader("Authorization", self.headers["Authorization"])
                    connection.putheader("Content-Length", "2")
                    connection.putheader(*extra)
                    connection.endheaders(b"{}")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    self.assertTrue(response.will_close)
                    response.read()
                finally:
                    connection.close()


    def test_rejects_invalid_role_in_chat_history(self):
        body = json.dumps({"model": MODEL, "messages": [
            {"role": [], "content": "Previous message"},
            {"role": "user", "content": "Привет!"},
        ]})
        headers = {**self.headers, "Content-Type": "application/json", "x-funny-root": str(self.workspace),
                   "x-funny-kind": "primary", "x-funny-session": "bad-role"}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("POST", "/v1/chat/completions", body, headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            connection.close()
