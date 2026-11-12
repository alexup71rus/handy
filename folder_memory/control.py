"""Start, stop and inspect the Handy service for this checkout."""

import argparse
from collections import deque
from contextlib import suppress
import hmac
import http.client
import json
import os
import re
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import time

from .runtime import (
    LAUNCHERS, OWNERS, engine_settings, get_token, lock_held, process_lock, read_token,
    runtime_dir, service_proof, model_config_proof, set_watches_paused, write_json,
)
from .systemd_unit import (
    UNIT, fragment_matches, read_managed_unit, unit_directory_lock, unit_file, unit_header,
    unit_properties, user_systemctl,
)


PROJECT = Path(__file__).resolve().parent.parent
LOGS = ("service.log", "service-error.log")


def response_json(connection):
    response = connection.getresponse()
    if response.status != 200:
        raise ValueError(f"Handy service returned HTTP {response.status}.")
    result = json.loads(response.read(4096))
    if not isinstance(result, dict):
        raise ValueError("Handy returned an invalid service response.")
    return result


def identify(connection, state, token):
    nonce = secrets.token_hex(16)
    connection.request("GET", "/api/status?nonce=" + nonce)
    result = response_json(connection)
    proof = result.get("proof")
    if (result.get("name") != "funny" or result.get("owner") not in OWNERS or type(result.get("pid")) is not int
            or type(result.get("stopped")) is not bool or not isinstance(proof, str) or not proof.isascii()
            or not hmac.compare_digest(proof, service_proof(token, nonce, state, result))):
        raise ValueError("The listener does not identify this Handy runtime.")
    if 'model_config' in result:
        fingerprint, proof = result['model_config'], result.get('model_proof')
        if (not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)
                or not isinstance(proof, str) or not proof.isascii()
                or not hmac.compare_digest(proof, model_config_proof(token, nonce, state, result))):
            raise ValueError('The listener did not prove its active model configuration.')
    return result


def engine_status(state, settings):
    port = settings["port"]
    info = {"status": "stopped", "port": port,
            "launcher": settings["launcher"], "pid": None, "owner": None}
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.6)
    try:
        try:
            connection.connect()
        except ConnectionRefusedError:
            if lock_held(state / "engine.lock"):
                info["status"] = "starting"
            return info
        result = identify(connection, state, read_token(state))
        return {**info, "status": "stopping" if result.get("stopped") else "running",
                "pid": result["pid"], "owner": result["owner"], 'model_config': result.get('model_config')}
    except (FileNotFoundError, TimeoutError, ConnectionResetError, http.client.RemoteDisconnected):
        if lock_held(state / "engine.lock"):
            return {**info, "status": "starting"}
    except (OSError, ValueError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    return {**info, "status": "conflict", "message": f"Port {port} is occupied by another listener or an older Handy process."}


def control_systemd(command, state):
    unit = unit_file()
    with unit_directory_lock(unit):
        fields = unit_properties()
        if fields["LoadState"] != "loaded" or not fragment_matches(unit, fields):
            raise ValueError(f"The user manager has no installer-managed {UNIT} for this Handy checkout.")
        if fields["NeedDaemonReload"] != "no":
            raise ValueError(f"{UNIT} changed on disk. Reload the systemd user manager and retry.")
        if f"{UNIT}.d/" in fields["DropInPaths"]:
            raise ValueError(f"{UNIT} has systemd overrides. Review them before using handyctl.")
        content = read_managed_unit(unit)
        if content is None or not content.startswith(unit_header(PROJECT, state)):
            raise ValueError(f"{UNIT} is registered for another Handy checkout or state directory.")
        if command == "stop":
            set_watches_paused(state, True)
        user_systemctl(command, UNIT)


def wait_ready(state, settings, *, child=None):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        info = engine_status(state, settings)
        if info["status"] == "running" and info["owner"] == settings["launcher"]:
            return info
        if info["status"] == "conflict":
            raise ValueError(info["message"])
        if child is not None and child.poll() is not None:
            break
        time.sleep(0.1)
    raise ValueError("Handy did not start. Check the service log with handyctl logs.")


def stop_engine(state, settings):
    if settings["launcher"] == "systemd":
        control_systemd("stop", state)
    else:
        set_watches_paused(state, True)
    deadline = time.monotonic() + 40
    requested = False
    while time.monotonic() < deadline:
        info = engine_status(state, settings)
        if info["status"] == "running" and not requested:
            connection = http.client.HTTPConnection("127.0.0.1", settings["port"], timeout=2)
            try:
                token = read_token(state)
                identity = identify(connection, state, token)
                if identity["stopped"]:
                    requested = True
                elif connection.sock is not None:
                    connection.request("POST", "/api/shutdown", json.dumps({"state_dir": str(state)}),
                                       {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                    response_json(connection)
                    requested = True
            except (ConnectionError, http.client.RemoteDisconnected):
                # A concurrently stopping owner may have already closed the socket.
                pass
            finally:
                connection.close()
        if not (lock_held(state / "engine.lock") or lock_held(state / "schedules-worker.lock")):
            if info["status"] == "conflict" and not requested:
                raise ValueError(info["message"])
            return
        time.sleep(0.1)
    raise ValueError("Handy or its watches are still stopping. Check handyctl status and logs.")


def open_log(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    if stat.S_ISREG(os.fstat(fd).st_mode):
        return fd
    os.close(fd)
    raise ValueError(f"Expected a regular log file: {path.name}")


def start_engine(state, settings):
    info = engine_status(state, settings)
    if info["status"] == "conflict":
        raise ValueError(info["message"])
    if settings["launcher"] == "systemd":
        if info["status"] == "running" and info["owner"] != "systemd":
            raise ValueError("Stop the other Handy instance before starting the user service.")
        control_systemd("start", state)
        return wait_ready(state, settings)
    if settings["launcher"] != "managed":
        settings = {**settings, "launcher": "managed"}
        write_json(state / "engine.json", settings)
        info = engine_status(state, settings)
    if info["status"] == "running" and info["owner"] == "managed":
        return info
    if info["status"] != "stopped":
        stop_engine(state, settings)
    stdout = open_log(state / LOGS[0])
    try:
        stderr = open_log(state / LOGS[1])
        try:
            child = subprocess.Popen(
                [settings["python"], "-m", "folder_memory", "--state-dir", str(state), "--owner", "managed"],
                cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                env={**os.environ, "PYTHONUNBUFFERED": "1"}, start_new_session=True,
            )
        finally:
            os.close(stderr)
    finally:
        os.close(stdout)
    try:
        return wait_ready(state, settings, child=child)
    except (OSError, ValueError, KeyboardInterrupt):
        if child.poll() is None:
            with suppress(ProcessLookupError):
                child.terminate()
            try:
                child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    child.kill()
                child.wait(timeout=2)
        raise


def show_logs(state, settings):
    if settings["launcher"] == "systemd":
        subprocess.run(["journalctl", "--user", "-u", UNIT, "-n", "40", "--no-pager"], check=True, timeout=10)
        return
    for name in LOGS:
        path = state / name
        if path.is_file():
            print(f"--- {name} ---")
            with path.open(encoding="utf-8", errors="replace") as stream:
                print("".join(deque(stream, 40)), end="")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, prog="handyctl")
    parser.add_argument("--state-dir", type=Path, default=runtime_dir())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("start", help="Start Handy's models and pi scheduler.")
    commands.add_parser("stop", help="Stop Handy and pause its watches.")
    commands.add_parser("logs", help="Show recent service logs.")
    commands.add_parser("run", help="Run the systemd service in the foreground.")
    commands.add_parser("status", help="Inspect the service.").add_argument("--json", action="store_true")
    configure = commands.add_parser("configure", help="Choose who starts and stops the service.")
    configure.add_argument("--launcher", choices=LAUNCHERS, required=True)
    args = parser.parse_args(argv)
    state = args.state_dir.expanduser().resolve()
    try:
        if args.command == "status":
            info = engine_status(state, engine_settings(state))
            info["watches_running"] = lock_held(state / "schedules-worker.lock")
            if args.json:
                print(json.dumps(info))
            else:
                print(info.get("message") or f"Handy {info['status']} · port {info['port']} · {info['launcher']}")
            return
        if args.command == "logs":
            show_logs(state, engine_settings(state))
            return
        if args.command == "run":
            if engine_settings(state)["launcher"] != "systemd":
                print("Handy: systemd startup skipped because another launcher is configured.", file=sys.stderr)
                return
            from .server import serve
            serve(state, owner="systemd")
            return
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.command == "stop":
            from .pi_scheduler import stop_worker
            stop_worker(state)
        with process_lock(state / "control.lock", wait=True):
            settings = engine_settings(state)
            if args.command == "start":
                info = start_engine(state, settings)
                from .pi_runtime import load_settings
                if load_settings(state)['scheduler_enabled']:
                    from .pi_scheduler import start_worker
                    start_worker(state)
                print(f"Handy running · PID {info['pid']} · port {info['port']}")
            elif args.command == "stop":
                if settings["launcher"] == "opencode":
                    settings = {**settings, "launcher": "managed"}
                    write_json(state / "engine.json", settings)
                stop_engine(state, settings)
                print("Handy stopped.")
            else:
                changing = settings["launcher"] != args.launcher
                if changing and (engine_status(state, settings)["status"] != "stopped"
                                 or lock_held(state / "schedules-worker.lock")):
                    raise ValueError("Stop Handy and its watches before changing the launcher.")
                get_token(state)
                write_json(state / "engine.json", {**settings, "launcher": args.launcher})
                if changing and args.launcher == "opencode":
                    set_watches_paused(state, False)
    except (OSError, ValueError, subprocess.SubprocessError, http.client.HTTPException) as error:
        parser.exit(1, f"Handy: {error}\n")


if __name__ == "__main__":
    main()
