"""A tiny stubborn worker stands in for llama.cpp during crash cleanup."""

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from folder_memory.neural import LocalModel


def running(pid):
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
    status = result.stdout.strip()
    return bool(status and not status.startswith("Z"))


class ModelSupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve()
        self.pid_file = self.state / "worker.pid"
        self.binary = self.state / "fake-llama"
        self.binary.write_text("#!" + sys.executable + "\nimport os,signal,time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nPath(" + repr(str(self.pid_file)) + ").write_text(str(os.getpid()))\nwhile True: time.sleep(.1)\n")
        self.binary.chmod(0o700)
        self.weights = self.state / "fixture.gguf"
        self.weights.write_bytes(b"fixture, not actual model weights")
        self.config = {"enabled": True, "binary": str(self.binary), "model": str(self.weights)}
        (self.state / "neural.json").write_text(json.dumps(self.config))

    def start(self):
        model = LocalModel(self.state)
        self.addCleanup(model.shutdown)
        with patch.object(model, "_request", side_effect=lambda *a, **kw: {"status": "ok" if self.pid_file.exists() else "loading"}):
            model._start()
        return model

    def test_lifeline_eof_reaps_worker_and_allows_a_fresh_start(self):
        model = self.start()
        supervisor = model.process
        worker = int(self.pid_file.read_text())
        started = time.monotonic()
        supervisor.stdin.close()
        supervisor.wait(timeout=3)
        self.assertLess(time.monotonic() - started, 2.9)
        self.assertFalse(running(worker), "EOF stopped the supervisor but left its worker")
        self.assertTrue(supervisor.stdin.closed)
        self.pid_file.unlink()
        with patch.object(model, "_request", side_effect=lambda *a, **kw: {"status": "ok" if self.pid_file.exists() else "loading"}):
            model._start()
        replacement = model.process
        replacement_worker = int(self.pid_file.read_text())
        self.assertIsNot(supervisor, replacement)
        self.assertNotEqual(worker, replacement_worker)
        model.close()
        self.assertIsNone(model.process)
        self.assertIsNotNone(replacement.poll())
        self.assertTrue(replacement.stdin.closed)
        self.assertFalse(running(replacement_worker))

    def test_stats_include_worker_memory_and_survive_concurrent_eviction(self):
        model = self.start()
        supervisor = model.process
        worker = int(self.pid_file.read_text())
        stats = model.stats()
        self.assertEqual(stats["supervisor_pid"], supervisor.pid)
        self.assertIn(worker, stats["worker_pids"])
        own = [item for item in stats["accounting"]["processes"] if item["pid"] in {supervisor.pid, worker}]
        self.assertEqual(stats["model_rss_bytes"], sum(item["rss_bytes"] or 0 for item in own))
        self.assertGreater(stats["model_rss_bytes"], 0)
        poll = supervisor.poll

        def evict():
            model.process = None
            return poll()

        try:
            with patch.object(supervisor, "poll", side_effect=evict):
                observed = model.stats()
            self.assertEqual(observed["pid"], supervisor.pid)
        finally:
            model.process = supervisor

    def test_closed_lifeline_cannot_reuse_a_still_stopping_supervisor(self):
        model = self.start()
        supervisor = model.process
        worker = int(self.pid_file.read_text())
        supervisor.stdin.close()
        self.assertIsNone(supervisor.poll())
        self.pid_file.unlink()
        with patch.object(model, "_request", side_effect=lambda *a, **kw: {"status": "ok" if self.pid_file.exists() else "loading"}):
            model._start()
        self.assertIsNot(model.process, supervisor)
        self.assertIsNotNone(supervisor.poll())
        self.assertTrue(supervisor.stdin.closed)
        self.assertFalse(running(worker))

    @unittest.skipUnless(os.name == "posix", "SIGKILL and Unix pipe ownership contract")
    def test_sigkill_of_core_stops_supervisor_and_stubborn_worker(self):
        root = Path(__file__).resolve().parent.parent
        source = """
import json,sys,time
from pathlib import Path
from unittest.mock import patch
from folder_memory.neural import LocalModel
model=LocalModel(sys.argv[1])
pid_file=Path(sys.argv[1])/'worker.pid'
with patch.object(model,'_request',side_effect=lambda *a,**kw:{'status':'ok' if pid_file.exists() else 'loading'}): model._start()
print(json.dumps({'supervisor':model.process.pid,'worker':int(pid_file.read_text())}),flush=True)
time.sleep(30)
"""
        core = subprocess.Popen([sys.executable, "-B", "-u", "-c", source, str(self.state)], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        owned = []
        try:
            self.assertTrue(select.select([core.stdout], [], [], 4)[0], "Core fixture did not start its worker")
            pids = json.loads(core.stdout.readline())
            owned = list(pids.values())
            self.assertTrue(all(running(pid) for pid in owned))
            started = time.monotonic()
            core.kill()
            core.wait(timeout=2)
            deadline = started + 2.9
            while any(running(pid) for pid in owned) and time.monotonic() < deadline:
                time.sleep(0.03)
            self.assertTrue(all(not running(pid) for pid in owned), "Model worker survived SIGKILL of its core")
            self.assertLess(time.monotonic() - started, 3)
        finally:
            if core.poll() is None:
                core.kill()
            core.communicate(timeout=2)
            for pid in owned:
                if running(pid):
                    # These exact fixture command lines establish ownership
                    # before emergency cleanup after a failing assertion.
                    command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout
                    if str(self.binary) in command or str(self.state) in command:
                        os.kill(pid, signal.SIGKILL)

    def test_wrong_owner_never_starts_a_worker(self):
        supervisor = Path(__file__).resolve().parent.parent / "folder_memory/model_supervisor.py"
        process = subprocess.Popen([sys.executable, "-I", str(supervisor), str(2**30), str(self.binary)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertEqual(process.wait(timeout=2), 125)
            self.assertFalse(self.pid_file.exists())
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
