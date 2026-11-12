"""Exercise the installed CLI and local service with private state and ports."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import call, patch

from folder_memory import control
from folder_memory.runtime import get_token, write_json
from folder_memory.schedules import Schedules
from folder_memory.systemd_unit import UNIT, unit_directory_lock, unit_file, unit_header


PROJECT = Path(__file__).resolve().parent.parent


class ServiceControlTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.settings = self.state / "engine.json"
        write_json(self.settings, {"port": self.port, "custom": "preserved"})

    def command(self, module, *arguments):
        return [sys.executable, "-m", module, "--state-dir", str(self.state), *arguments]

    def cli(self, *arguments, check=True):
        return subprocess.run(self.command("folder_memory.control", *arguments), cwd=PROJECT,
                              capture_output=True, text=True, check=check, timeout=15)

    def status(self):
        return json.loads(self.cli("status", "--json").stdout)

    def test_concurrent_start_and_stop_pause_watches_then_resume(self):
        self.addCleanup(self.cli, "stop", check=False)
        schedules = Schedules(self.state)
        workspace = "a" * 24
        job = schedules.create(workspace, url="https://example.invalid", interval={"count": 1, "unit": "day"},
                               now=time.time() + 3600)
        with subprocess.Popen(self.command("folder_memory.control", "start"), cwd=PROJECT, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as first:
            second = self.cli("start")
            output, errors = first.communicate(timeout=15)
            self.assertEqual(first.returncode, 0, errors)
            self.assertEqual(output, second.stdout)
        running = self.status()
        self.assertEqual((running["status"], running["owner"]), ("running", "managed"))
        self.assertTrue(running["watches_running"])
        self.assertEqual(json.loads(self.settings.read_text())["custom"], "preserved")
        self.cli("stop")
        self.cli("stop")
        stopped = self.status()
        self.assertEqual((stopped["status"], stopped["pid"], stopped["launcher"]), ("stopped", None, "managed"))
        self.assertFalse(stopped["watches_running"])
        self.assertTrue(schedules.get(workspace, job["id"])["enabled"])
        # A previously loaded OpenCode plugin may still use the old start command.
        blocked = subprocess.run(self.command("folder_memory", "--port", str(self.port)), cwd=PROJECT,
                                 capture_output=True, text=True, timeout=5)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("launcher managed", blocked.stderr)
        self.cli("start")
        self.assertNotEqual(self.status()["pid"], running["pid"])
        self.assertTrue(self.status()["watches_running"])

    def test_shutdown_rejects_foreign_state_and_browser_origin(self):
        self.addCleanup(self.cli, "stop", check=False)
        self.cli("start")
        token = get_token(self.state)
        for body, headers, expected in [
            ({"state_dir": str(self.state)}, {}, 401),
            ({"state_dir": str(self.state)}, {"Authorization": "Bearer " + token, "Origin": "http://example.test"}, 401),
            ({"state_dir": "/another-runtime"}, {"Authorization": "Bearer " + token}, 400),
        ]:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            try:
                connection.request("POST", "/api/shutdown", json.dumps(body), headers)
                self.assertEqual(connection.getresponse().status, expected)
            finally:
                connection.close()
        self.assertEqual(self.status()["status"], "running")

    def test_stop_does_not_send_credentials_or_signal_to_a_lookalike(self):
        seen = []

        class Foreign(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                body = json.dumps({"name": "funny", "pid": 123, "owner": "managed", "stopped": False,
                                   "proof": "0" * 64}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        get_token(self.state)
        with ThreadingHTTPServer(("127.0.0.1", 0), Foreign) as foreign:
            write_json(self.settings, {"port": foreign.server_port})
            server = threading.Thread(target=foreign.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
            server.start()
            try:
                self.assertEqual(self.status()["status"], "conflict")
                self.assertNotEqual(self.cli("start", check=False).returncode, 0)
                self.assertNotEqual(self.cli("stop", check=False).returncode, 0)
                self.assertEqual(self.status()["status"], "conflict")
                self.assertEqual(seen, [None] * 4)
            finally:
                foreign.shutdown()
                server.join(3)

    def test_manual_configuration_creates_token_without_starting(self):
        self.settings.unlink()
        self.cli("configure", "--launcher", "opencode")
        self.assertEqual(json.loads(self.settings.read_text())["launcher"], "opencode")
        self.assertEqual((self.state / "token").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.state / "engine.lock").exists())

    def test_acknowledged_shutdown_can_close_the_listener_before_final_status(self):
        get_token(self.state)
        settings = control.engine_settings(self.state)
        with patch.object(control, 'engine_status', side_effect=[
                {'status': 'running'}, {'status': 'conflict', 'message': 'listener is closing'}]), \
                patch.object(control, 'identify', return_value={'stopped': False}), \
                patch.object(control, 'response_json', return_value={'stopping': True}), \
                patch.object(control, 'lock_held', side_effect=[True, False, False]), \
                patch.object(control.time, 'sleep'), \
                patch.object(control.http.client, 'HTTPConnection') as connection:
            control.stop_engine(self.state, settings)
        self.assertEqual(connection.return_value.request.call_args.args[:2], ('POST', '/api/shutdown'))

    def test_systemd_command_does_not_start_after_switching_to_on_demand(self):
        self.cli("configure", "--launcher", "systemd")
        self.cli("configure", "--launcher", "opencode")
        self.assertIn("systemd startup skipped", self.cli("run").stderr)
        self.assertEqual(self.status()["status"], "stopped")


class SystemdControlTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = self.root / "registered state %n"
        self.state.mkdir()
        self.enterContext(patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}))
        self.unit = unit_file()
        self.unit.parent.mkdir(parents=True)
        self.unit.write_bytes(unit_header(PROJECT, self.state) + b"[Service]\n")
        self.properties = {
            "LoadState": "loaded",
            "FragmentPath": str(self.unit),
            "DropInPaths": "",
            "NeedDaemonReload": "no",
        }
        self.enterContext(patch("folder_memory.control.unit_properties", return_value=self.properties))
        self.systemctl = self.enterContext(patch("folder_memory.control.user_systemctl"))
        self.enterContext(patch("folder_memory.control.engine_status", return_value={"status": "stopped"}))
        self.settings = {"launcher": "systemd"}

    def test_start_and_stop_only_send_the_verified_unit_to_systemctl(self):
        with patch("folder_memory.control.wait_ready"):
            control.start_engine(self.state, self.settings)
        control.stop_engine(self.state, self.settings)
        self.assertEqual(self.systemctl.call_args_list, [call("start", UNIT), call("stop", UNIT)])
        self.assertTrue((self.state / "watches.paused").exists())

    def test_stale_checkout_or_state_cannot_control_the_unit(self):
        old_state = self.root / "old state"
        old_state.mkdir()
        for project, state in ((PROJECT, old_state), (self.root / "old checkout", self.state)):
            with self.subTest(project=project, state=state), patch.object(control, "PROJECT", project):
                for operation in (control.start_engine, control.stop_engine):
                    with self.assertRaisesRegex(ValueError, "another Handy checkout or state directory"):
                        operation(state, self.settings)
                self.assertFalse((state / "watches.paused").exists())
        self.systemctl.assert_not_called()

    def test_unmanaged_unit_foreign_fragment_and_pending_reload_cannot_control_service(self):
        for field, value in (("FragmentPath", str(self.root / UNIT)), ("NeedDaemonReload", "yes")):
            with self.subTest(field=field), patch.dict(self.properties, {field: value}):
                for operation in (control.start_engine, control.stop_engine):
                    with self.assertRaises(ValueError):
                        operation(self.state, self.settings)
        self.unit.write_text("[Service]\nExecStart=/usr/bin/true\n")
        for operation in (control.start_engine, control.stop_engine):
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                operation(self.state, self.settings)
        self.systemctl.assert_not_called()
        self.assertFalse((self.state / "watches.paused").exists())

    def test_systemctl_actions_fail_without_waiting_for_an_install(self):
        with unit_directory_lock(self.unit, installing=True):
            with self.assertRaisesRegex(ValueError, "install is in progress"):
                control.stop_engine(self.state, self.settings)
        self.systemctl.assert_not_called()
        self.assertFalse((self.state / "watches.paused").exists())
