"""Real wall-clock HTTP deadlines against a local trickling server."""

import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from folder_memory.http_client import WorkerScope, bounded_request


class SlowHeaders(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _trickle(self):
        # Socket reads keep succeeding, so a per-read timeout cannot bound
        # the total response time.
        try:
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            self.wfile.flush()
            for _ in range(100):
                if self.server.stop.wait(0.05):
                    return
                self.wfile.write(b"X-Slow: yes\r\n")
                self.wfile.flush()
            self.wfile.write(b"Content-Length: 0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self.server.calls.append(self.path)
        self._trickle()


class HTTPDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHeaders)
        self.server.calls = []
        self.server.stop = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.args = {"url": f"http://127.0.0.1:{self.server.server_port}/slow", "method": "GET", "body": ""}
        self.limits = {"timeout_seconds": 0.8, "response_bytes": 1024, "same_origin_redirects": 0}

    def close_server(self):
        self.server.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_trickling_headers_obey_total_deadline_and_worker_is_reaped(self):
        workers = []
        popen = subprocess.Popen

        def record_worker(*args, **kwargs):
            process = popen(*args, **kwargs)
            workers.append(process)
            return process

        started = time.monotonic()
        with patch("folder_memory.http_client.subprocess.Popen", side_effect=record_worker), self.assertRaises(TimeoutError):
            bounded_request(self.args, self.limits)
        self.assertLess(time.monotonic() - started, 1.8)
        self.assertEqual(self.server.calls, ["/slow"])
        self.assertEqual(len(workers), 1)
        self.assertIsNotNone(workers[0].poll())
        self.assertTrue(workers[0].stdin.closed)
        self.assertTrue(workers[0].stdout.closed)

    def test_capacity_wait_obeys_deadline_without_starting_a_request(self):
        scope = WorkerScope()
        self.addCleanup(scope.shutdown)
        for _ in range(4):
            self.assertTrue(scope.slots.acquire(blocking=False))
        try:
            started = time.monotonic()
            with scope.activate(), self.assertRaisesRegex(TimeoutError, "capacity deadline"):
                bounded_request(self.args, self.limits)
            self.assertLess(time.monotonic() - started, 1.8)
            self.assertEqual(self.server.calls, [])
        finally:
            for _ in range(4):
                scope.slots.release()


if __name__ == "__main__":
    unittest.main()
