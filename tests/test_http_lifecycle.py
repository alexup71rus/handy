"""HTTP children stop with their owning App, cancellation or parent process."""

import os
import select
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from folder_memory.http_client import WorkerScope, bounded_request


class ControlledHeaders(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        with self.server.condition:
            self.server.requests += 1
            self.server.condition.notify_all()
        self.server.release.wait(6)
        try:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        except (BrokenPipeError, ConnectionResetError):
            pass


class HTTPLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ControlledHeaders)
        self.server.condition = threading.Condition()
        self.server.requests = 0
        self.server.release = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/fixture"
        self.args = {"url": self.url, "method": "GET", "body": "", "content_type": "text/plain"}
        self.limits = {"timeout_seconds": 5, "response_bytes": 100, "same_origin_redirects": 0}

    def close_server(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def entered(self, count):
        with self.server.condition:
            return self.server.condition.wait_for(lambda: self.server.requests >= count, timeout=3)

    def test_shutdown_reaps_owned_request_without_stopping_another_app(self):
        scopes = [WorkerScope(), WorkerScope()]
        for scope in scopes:
            self.addCleanup(scope.shutdown)
        done, outcomes, workers = [threading.Event(), threading.Event()], [{}, {}], []
        popen = subprocess.Popen

        def record(*args, **kwargs):
            process = popen(*args, **kwargs)
            workers.append(process)
            return process

        def request(index):
            try:
                with scopes[index].activate():
                    outcomes[index]["value"] = bounded_request(self.args, self.limits)
            except BaseException as error:
                outcomes[index]["error"] = error
            finally:
                done[index].set()

        with patch("folder_memory.http_client.subprocess.Popen", side_effect=record):
            threads = [threading.Thread(target=request, args=(index,), daemon=True) for index in range(2)]
            for thread in threads:
                thread.start()
            self.assertTrue(self.entered(2))
            scopes[0].shutdown()
            self.assertTrue(done[0].wait(2))
            self.assertIsInstance(outcomes[0].get("error"), OSError)
            self.assertFalse(done[1].is_set(), "Closing one App cancelled another App's HTTP request")
            self.server.release.set()
            for thread in threads:
                thread.join(3)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(outcomes[1]["value"]["body"], "ok")
            with scopes[0].activate(), self.assertRaises(OSError):
                bounded_request(self.args, self.limits)
        self.assertEqual(len(workers), 2)
        self.assertTrue(all(process.poll() is not None and process.stdin.closed and process.stdout.closed for process in workers))
        self.assertTrue(all(not scope.processes for scope in scopes))
        self.assertEqual(self.server.requests, 2)

    def test_keyboard_interrupt_reaps_worker_and_closes_pipes(self):
        scope = WorkerScope()
        self.addCleanup(scope.shutdown)
        workers = []
        popen = subprocess.Popen

        def interrupted(*args, **kwargs):
            process = popen(*args, **kwargs)
            workers.append(process)
            communicate = process.communicate
            calls = 0

            def call(*values, **options):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise KeyboardInterrupt()
                return communicate(*values, **options)

            process.communicate = call
            return process

        with scope.activate(), patch("folder_memory.http_client.subprocess.Popen", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                bounded_request(self.args, self.limits)
        self.assertEqual(len(workers), 1)
        self.assertIsNotNone(workers[0].poll())
        self.assertTrue(workers[0].stdin.closed and workers[0].stdout.closed)
        self.assertEqual(scope.processes, set())

    @unittest.skipUnless(os.name == "posix", "Parent-death watchdog is a Unix process contract")
    def test_worker_exits_when_parent_is_killed_mid_request(self):
        root = Path(__file__).resolve().parent.parent
        script = """
import subprocess, sys
from folder_memory.http_client import bounded_request
real_popen = subprocess.Popen
def spawn(*args, **kwargs):
    process = real_popen(*args, **kwargs)
    print(process.pid, flush=True)
    return process
subprocess.Popen = spawn
bounded_request({'url':sys.argv[1], 'method':'GET', 'body':'', 'content_type':'text/plain'}, {'timeout_seconds':5, 'response_bytes':100, 'same_origin_redirects':0})
"""
        parent = subprocess.Popen([sys.executable, "-B", "-u", "-c", script, self.url], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child = None
        try:
            self.assertTrue(select.select([parent.stdout], [], [], 3)[0], "Fixture parent failed to start its HTTP worker")
            child = int(parent.stdout.readline())
            self.assertTrue(self.entered(1))
            parent.kill()
            parent.wait(timeout=2)
            deadline = time.monotonic() + 2
            while self.running(child) and time.monotonic() < deadline:
                time.sleep(0.03)
            self.assertFalse(self.running(child), "HTTP worker survived its parent process")
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=2)
            # An assertion failure must still leave only this known fixture
            # child to its independently enforced five-second deadline.
            if child:
                deadline = time.monotonic() + 5
                while self.running(child) and time.monotonic() < deadline:
                    time.sleep(0.05)

    @staticmethod
    def running(pid):
        result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        status = result.stdout.strip()
        return bool(status and not status.startswith("Z"))


if __name__ == "__main__":
    unittest.main()
