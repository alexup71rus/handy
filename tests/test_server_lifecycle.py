"""Stop admission and keep maintenance alive when one resource is busy."""

import contextlib
import io
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from folder_memory.server import App, Handler, MODEL, Server
from folder_memory.neural import LocalModel


class NoModel:
    enabled = False

    def __init__(self, *args, **kwargs):
        self.closed = False

    def reap_idle(self):
        pass

    def shutdown(self):
        self.closed = True


class ServerLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.root = base / "workspace"
        self.root.mkdir()
        with patch("folder_memory.server.create_model", NoModel):
            self.app = App(base / "state")
        self.addCleanup(self.app.shutdown)
        self.body = {"model": MODEL, "messages": [{"role": "user", "content": "Привет"}]}
        self.headers = {"x-funny-session": "lifecycle", "x-funny-root": str(self.root), "x-funny-kind": "primary"}


    def test_shutdown_rejects_new_model_and_tool_requests(self):
        from folder_memory.tool_api import execute_tool

        self.app.shutdown()
        self.assertTrue(all(model.closed for model in self.app._model_workers().values()))
        with self.assertRaises(OSError), self.app.model_completion(self.body, self.headers):
            self.fail("A stopped model accepted a request")
        with self.assertRaises(OSError):
            execute_tool(self.app, {"tool": "artifact", "root": str(self.root), "session": "lifecycle",
                                    "arguments": {"kind": "timer", "options": {"seconds": 10}}})
        self.assertFalse((self.root / ".handy").exists())

    def test_http_healthz_does_not_wait_for_the_busy_model_lock(self):
        model = LocalModel(self.app.state_dir)
        self.app.neural_model = model
        self.app.embedding_model = LocalModel(self.app.state_dir, profile="embedding")

        class BusySemantic:
            def stats(self):
                with model.lock:
                    return {"ready": False}

        self.app.semantic = BusySemantic()
        entered, release = threading.Event(), threading.Event()

        def inference():
            with model.lock:
                entered.set()
                release.wait(3)

        worker = threading.Thread(target=inference, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(1))
        server = Server(("127.0.0.1", 0), Handler)
        server.app = self.app
        serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        serving.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=0.4)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                data = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(set(data), {"name", "version", "pid", "stopped", "model_config"})
            self.assertEqual(data["name"], "funny")
            self.assertEqual(data["pid"], os.getpid())
            self.assertFalse(data["stopped"])
            self.assertFalse(release.is_set())
            self.app.stopped.set()
            self.assertTrue(self.app.healthz()["stopped"])
        finally:
            connection.close()
            release.set()
            worker.join(1)
            server.shutdown()
            server.server_close()
            serving.join(1)






if __name__ == "__main__":
    unittest.main()
