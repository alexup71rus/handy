"""Compile and exercise the native settings state against an isolated backend.

No app window, model inference, real provider, or user's registry is used.
Run with the Handy virtualenv on macOS: .venv/bin/python integration/macos/test_settings.py
"""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import threading


HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parents[1]


def prepare(state, root, python, environment):
    """Create tiny valid headers and a runtime that must never be executed."""
    state.mkdir(mode=0o700)
    fixture = root / "fixtures"
    fixture.mkdir(mode=0o700)
    binary = fixture / "llama-server"
    binary.write_text('#!/bin/sh\ntouch "$HANDY_SETTINGS_TEST_ROOT/model-runtime-executed"\nexit 97\n')
    binary.chmod(0o700)
    files = {}
    for filename in ("LFM3-VL-2B-Q6_K.gguf", "mmproj-LFM3-VL-2B-F16.gguf", "Qwen3-Embedding-0.6B-Q8_0.gguf"):
        path = fixture / filename
        contents = b"GGUF" + struct.pack("<I", 3) + b"settings smoke fixture, not model weights"
        path.write_bytes(contents)
        files[filename] = {"path": str(path), "sha256": hashlib.sha256(contents).hexdigest(), "bytes": len(contents)}
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        port = connection.getsockname()[1]
    (state / "engine.json").write_text(json.dumps({"port": port, "launcher": "managed", "python": str(python)}))
    setup = root / "prepare.json"
    setup.write_text(json.dumps({"files": files, "binary": str(binary)}))
    source = """import json,os
from pathlib import Path
from folder_memory.model_registry import ModelRegistry
root=Path(os.environ['HANDY_SETTINGS_TEST_ROOT'])
fixture=json.loads((root/'prepare.json').read_text())
registry=ModelRegistry(Path(os.environ['HANDY_SETTINGS_TEST_STATE']))
for identifier,profile in registry.profiles().items():
 if profile.get('backend','local')!='local': continue
 profile['binary']=fixture['binary']
 for field in ('model','mmproj'):
  if field not in profile: continue
  item=fixture['files'].get(Path(profile[field]).name)
  if item is None: continue
  profile[field]=item['path'];profile[field+'_sha256']=item['sha256'];profile[field+'_bytes']=item['bytes']
 registry.update(identifier,profile)
registry.materialize()
"""
    subprocess.run([str(python), "-c", source], cwd=CHECKOUT, env=environment, check=True, timeout=20, capture_output=True)


def fault_bridge(root, python):
    """Fault-inject one asset/preview boundary without fetching model weights."""
    script = root / "asset_fault.py"
    script.write_text('''import json,os,shutil,sys,time
from pathlib import Path
sys.path.insert(0,os.environ['HANDY_SETTINGS_TEST_CHECKOUT'])
from folder_memory.model_settings import ModelSettings
root=Path(os.environ['HANDY_SETTINGS_TEST_ROOT'])
manager=ModelSettings(Path(os.environ['HANDY_SETTINGS_TEST_STATE']))
request=json.loads(sys.stdin.buffer.read())
pending=root/'asset-preview-fault'
def emit(event,**fields):
 print(json.dumps({'event':event,**fields}),flush=True)
if request['operation']=='download':
 profile=manager.registry.get(request['id'])
 source=Path(manager.registry.get('lfm3-vl')['model'])
 target=root/'downloaded-fixture'/Path(profile['model']).name
 target.parent.mkdir(exist_ok=True)
 shutil.copyfile(source,target)
 profile['model']=str(target)
 manager.registry.update(request['id'],profile)
 pending.write_text('pending')
 emit('result',data=manager.snapshot())
elif request['operation']=='preview_settings' and pending.exists():
 pending.unlink()
 mode=root/'asset-fault-mode'
 if mode.exists() and mode.read_text()=='cancel':
  (root/'asset-preview-waiting').write_text('waiting')
  time.sleep(30)
 emit('error',message='Intentional asset preview failure.')
 sys.exit(1)
else:
 emit('result',data=manager.perform(request))
''')
    executable = root / "asset-fault-bridge"
    executable.write_text("#!/bin/sh\nexec " + shlex.quote(str(python)) + " " + shlex.quote(str(script)) + ' "$@"\n')
    executable.chmod(0o700)
    return executable


def run(python):
    if sys.platform != "darwin":
        raise ValueError("The native settings smoke requires macOS.")
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            payload = {"data": [{"id": "smoke-chat-one", "context_length": 65536}, {"id": "smoke-chat-two", "context_length": 32768}]}
            inventory_size = root / "api-inventory-size"
            if inventory_size.exists():
                for index in range(3, int(inventory_size.read_text()) + 1):
                    payload["data"].append({"id": f"smoke-chat-{index}", "context_length": 65536})
            accepted = self.path == "/v1/models" and self.headers.get("Authorization") == "Bearer ONLY_A_TEST_SECRET"
            raw = json.dumps(payload if accepted else {"error": "Invalid smoke request"}).encode()
            self.send_response(200 if accepted else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    serving.start()
    try:
        with tempfile.TemporaryDirectory(prefix="handy-native-settings-") as temporary:
            root = Path(temporary)
            home, config, state = root / "home", root / "config", root / "state"
            home.mkdir(mode=0o700)
            environment = {**os.environ, "HOME": str(home), "HANDY_CONFIG_HOME": str(config),
                           "HANDY_SETTINGS_TEST_ROOT": str(root), "HANDY_SETTINGS_TEST_STATE": str(state),
                           "HANDY_SETTINGS_TEST_CHECKOUT": str(CHECKOUT), "HANDY_SETTINGS_TEST_PYTHON": str(python),
                           "HANDY_SETTINGS_TEST_URL": f"http://127.0.0.1:{server.server_port}/v1"}
            environment["HANDY_SETTINGS_FAULT_BRIDGE"] = str(fault_bridge(root, python))
            prepare(state, root, python, environment)
            executable = root / "SettingsSmoke"
            sources = [HERE / name for name in ("Control.swift", "SettingsData.swift", "VisibilityCheckbox.swift", "SettingsBridge.swift", "PiBridge.swift", "tests/SettingsSmoke.swift")]
            compiler = subprocess.run(["xcrun", "swiftc", "-target", f"{os.uname().machine}-apple-macosx13.0", "-parse-as-library",
                                       *(str(path) for path in sources), "-o", str(executable)],
                                      check=False, capture_output=True, text=True, timeout=120)
            if compiler.returncode:
                raise ValueError("Swift compilation failed:\n" + compiler.stderr)
            result = subprocess.run([str(executable)], env=environment, cwd=CHECKOUT, check=False,
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise ValueError(result.stderr.strip() or "Native settings smoke failed.")
            if len(requests) != 4 or any(path != "/v1/models" or auth != "Bearer ONLY_A_TEST_SECRET" for path, auth in requests):
                raise ValueError("The smoke did not use exactly the expected local API requests.")
            output = result.stdout.strip()
        print(output)
        print("Local provider: 4 authenticated checks passed. Temporary files removed.")
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=Path(sys.executable), help="Handy Python interpreter.")
    arguments = parser.parse_args()
    try:
        run(arguments.python.expanduser().absolute())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
