"""Real loopback disconnects cancel one inference socket, never its model."""

import http.client
import json
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

from folder_memory.neural import NeuralError
from folder_memory.server import App, Handler, MODEL, Server


class ModelCancellationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve()
        self.app = App(self.state)
        self.model = self.app.neural_model
        self.fronts = []
        self.entered = {}
        self.relay_entered = {}
        self.relay_done = {}
        self.disconnected = {}
        self.release = {}
        self.received = []
        self.errors = []
        for tag in ("stall-headers", "stall-stream", "first-active", "queued", "ok", "disconnect"):
            for mapping in (self.entered, self.relay_entered, self.relay_done, self.disconnected, self.release):
                mapping[tag] = threading.Event()
        fixture = self

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                tag = body["messages"][0]["content"]
                fixture.received.append(tag)
                self.close_connection = True
                if tag == "disconnect":
                    fixture.entered[tag].set()
                    return
                streaming = tag == "stall-stream"
                if streaming:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n')
                    self.wfile.flush()
                fixture.entered[tag].set()
                if tag.startswith("stall-") or tag == "first-active":
                    deadline = time.monotonic() + 5
                    while not fixture.release[tag].is_set() and time.monotonic() < deadline:
                        ready, _, _ = select.select([self.connection], [], [], .02)
                        if ready:
                            try:
                                closed = self.connection.recv(1, socket.MSG_PEEK) == b""
                            except OSError:
                                closed = True
                            if closed:
                                fixture.disconnected[tag].set()
                                return
                    if not fixture.release[tag].is_set():
                        return
                try:
                    if streaming:
                        self.wfile.write(b"data: [DONE]\n\n")
                    else:
                        data = json.dumps({"choices": [{"message": {"role": "assistant", "content": tag}}]}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(data)
                    self.wfile.flush()
                except OSError:
                    fixture.disconnected[tag].set()

        class Frontend(Handler):
            def log_message(self, *_):
                pass

            def error(self, status, text):
                fixture.errors.append(status)
                super().error(status, text)

            def relay_completion(self, body):
                tag = body["messages"][0]["content"]
                fixture.relay_entered[tag].set()
                try:
                    return super().relay_completion(body)
                finally:
                    fixture.relay_done[tag].set()

        self.upstream = Server(("127.0.0.1", 0), Upstream)
        self.server = Server(("127.0.0.1", 0), Frontend)
        self.server.app = self.app
        self.serving = []
        for server in (self.upstream, self.server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            self.serving.append(thread)
        self.model.config["port"] = self.upstream.server_port
        # A real small child catches accidental process termination without
        # loading any neural weights or interfering with the live service.
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                      stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.model.process = self.child
        self.start = patch.object(self.model, "_start")
        self.started = self.start.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for release in self.release.values():
            release.set()
        for connection in self.fronts:
            connection.close()
        for server in (self.server, self.upstream):
            server.shutdown()
            server.server_close()
        for thread in self.serving:
            thread.join(1)
        self.start.stop()
        self.app.shutdown()

    def body(self, tag):
        return {"model": MODEL, "messages": [{"role": "user", "content": tag}], "stream": tag == "stall-stream"}

    def request(self, tag):
        peer = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        self.fronts.append(peer)
        raw = json.dumps(self.body(tag)).encode()
        headers = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   f"Authorization: Bearer {self.app.token}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(raw)}\r\n\r\n").encode()
        peer.sendall(headers + raw)
        return peer

    def read(self, peer):
        response = http.client.HTTPResponse(peer)
        response.begin()
        content = response.read()
        response.close()
        return response.status, json.loads(content)

    def assert_model_survives(self):
        self.assertIs(self.model.process, self.child)
        self.assertIsNone(self.child.poll(), "Cancellation terminated the model process")

    def assert_next_request_completes(self):
        status, reply = self.read(self.request("ok"))
        self.assertEqual(status, 200)
        self.assertEqual(reply["choices"][0]["message"]["content"], "ok")
        self.assertTrue(self.relay_done["ok"].wait(1))
        self.assert_model_survives()

    def test_disconnect_before_headers_aborts_upstream_and_frees_the_lock(self):
        peer = self.request("stall-headers")
        self.assertTrue(self.entered["stall-headers"].wait(1))
        started = time.monotonic()
        peer.close()
        self.assertTrue(self.disconnected["stall-headers"].wait(1.5))
        self.assertTrue(self.relay_done["stall-headers"].wait(1))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.errors, [], "Cancelled peer must not receive a synthetic 503")
        self.assert_model_survives()
        self.assert_next_request_completes()

    def test_stream_disconnect_aborts_the_same_upstream_slot(self):
        peer = self.request("stall-stream")
        self.assertTrue(self.entered["stall-stream"].wait(1))
        data = b""
        while b"data:" not in data:
            data += peer.recv(4096)
        peer.close()
        self.assertTrue(self.disconnected["stall-stream"].wait(1.5))
        self.assertTrue(self.relay_done["stall-stream"].wait(1))
        self.assertEqual(self.errors, [])
        self.assert_next_request_completes()

    def test_cancelling_a_queued_request_does_not_abort_the_active_one(self):
        first = self.request("first-active")
        self.assertTrue(self.entered["first-active"].wait(1))
        queued = self.request("queued")
        self.assertTrue(self.relay_entered["queued"].wait(1))
        queued.close()
        self.assertTrue(self.relay_done["queued"].wait(1.5), "Cancelled request stayed blocked on the model lock")
        self.assertFalse(self.disconnected["first-active"].is_set())
        self.assertFalse(self.release["first-active"].is_set())
        self.assertEqual(self.received, ["first-active"])
        self.assert_model_survives()
        self.release["first-active"].set()
        self.assertEqual(self.read(first)[0], 200)
        self.assertTrue(self.relay_done["first-active"].wait(1))
        self.assert_next_request_completes()
        self.assertNotIn("queued", self.received)

    def test_already_cancelled_request_never_starts_a_model_or_upstream(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(NeuralError, "отменён"):
            with self.model.chat_completion(self.body("ok"), cancelled=cancelled):
                self.fail("A cancelled request reached inference")
        self.started.assert_not_called()
        self.assertEqual(self.received, [])
        self.assert_model_survives()

    def test_finished_watchers_do_not_cancel_later_requests(self):
        self.assert_next_request_completes()
        self.assertFalse(any(thread.is_alive() and thread.name in {"handy-peer-cancel", "handy-upstream-cancel"}
                             for thread in threading.enumerate()))
        first = self.request("first-active")
        self.assertTrue(self.entered["first-active"].wait(1))
        self.assertFalse(self.disconnected["first-active"].wait(.15))
        self.release["first-active"].set()
        self.assertEqual(self.read(first)[0], 200)
        self.assertTrue(self.relay_done["first-active"].wait(1))
        self.assert_model_survives()

    def test_real_upstream_failure_still_reports_503_to_a_connected_client(self):
        status, reply = self.read(self.request("disconnect"))
        self.assertEqual(status, 503)
        self.assertIn("модель недоступна", reply["error"]["message"])
        self.assert_next_request_completes()


if __name__ == "__main__":
    unittest.main()
