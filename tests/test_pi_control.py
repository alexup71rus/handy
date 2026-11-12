"""Explicit MCP probes obey the native report and clean up owned children."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from folder_memory import pi_control


class MCPProbeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.executable = self.root / 'pi'
        self.enterContext(patch.object(pi_control.pi, 'executable', return_value=str(self.executable)))

    def child(self, source):
        self.executable.write_text(f'#!{sys.executable}\n' + source)
        self.executable.chmod(0o700)

    def test_native_report_retains_actual_results_without_launch_credentials(self):
        report = {'servers': [{'name': 'fixture', 'state': 'connected', 'enabled': True,
                              'tools': ['search'], 'exposure': 'direct', 'scope': 'user',
                              'transport': 'server --password secret', 'source': '/private/config'}], 'errors': []}
        self.child(f'print({json.dumps(report)!r})\n')
        result = pi_control.check_mcp(self.state)
        self.assertEqual(result['servers'][0]['tools'], ['search'])
        self.assertEqual(result['servers'][0]['state'], 'connected')
        self.assertNotIn('transport', result['servers'][0])
        self.assertNotIn('source', result['servers'][0])
        self.assertNotIn('secret', json.dumps(result))

    def test_nonzero_exit_and_invalid_or_oversized_reports_never_look_successful(self):
        self.child('import sys\nprint(\'{"servers":[],"errors":[]}\')\nsys.exit(2)\n')
        self.assertIn('2', pi_control.check_mcp(self.state)['errors'][0])
        for source, message in [('print("not JSON")\n', 'корректный'),
                                ('print("x" * 5000)\n', 'слишком большой'),
                                ('import sys\nprint("x" * 5000, file=sys.stderr)\nprint(\'{"servers":[],"errors":[]}\')\n', 'слишком большой')]:
            self.child(source)
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, message):
                pi_control.check_mcp(self.state, max_bytes=1024)

    def test_deadline_reaps_the_probe_process(self):
        self.child('import time\ntime.sleep(60)\n')
        children = []
        original = pi_control.subprocess.Popen
        def start(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        before = time.monotonic()
        with patch.object(pi_control.subprocess, 'Popen', side_effect=start), self.assertRaisesRegex(ValueError, 'время проверки'):
            pi_control.check_mcp(self.state, timeout=0.2)
        self.assertLess(time.monotonic() - before, 5)
        self.assertIsNotNone(children[0].poll())
        with self.assertRaises(ProcessLookupError):
            os.kill(children[0].pid, 0)

    def test_bridge_termination_also_cleans_the_owned_probe(self):
        pidfile = self.root / 'pid'
        self.child(f'import os,time\nfrom pathlib import Path\nPath({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n')
        source = ('from folder_memory import pi_control\n'
                  f'pi_control.pi.executable = lambda: {str(self.executable)!r}\n'
                  f'raise SystemExit(pi_control.main(["--state-dir", {str(self.state)!r}, "check-mcp"]))\n')
        bridge = subprocess.Popen([sys.executable, '-c', source], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and bridge.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pidfile.exists(), 'The bridge never launched its probe')
            bridge.send_signal(signal.SIGTERM)
            _, stderr = bridge.communicate(timeout=5)
            self.assertEqual(bridge.returncode, 130, stderr.decode())
            self.assertIn('проверка MCP отменена', stderr.decode())
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
        finally:
            if bridge.poll() is None:
                bridge.kill()
                bridge.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
