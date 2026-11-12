"""Exercise the native pi bridge against real isolated settings and scheduler."""

import os
import json
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", type=Path, help="Also render the actual native SwiftUI views into this directory.")
    arguments = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("Native pi smoke requires macOS.")
    with tempfile.TemporaryDirectory(prefix="handy-pi-smoke-") as temporary:
        root = Path(temporary)
        environment = {**os.environ, "HANDY_CONFIG_HOME": str(root / "config"),
                       "HANDY_PI_SMOKE_ROOT": str(root), "HANDY_PI_SMOKE_PYTHON": str(Path(sys.executable).absolute()),
                       "HANDY_PI_SMOKE_CHECKOUT": str(PROJECT)}
        if arguments.render:
            environment["HANDY_PI_LAYOUT_OUTPUT"] = str(arguments.render.resolve())
        # A real user server must survive managed configuration and stay opt-in.
        agent = root / "state/pi/agent"
        agent.mkdir(parents=True)
        (agent / "mcp.json").write_text(json.dumps({"mcpServers": {"custom-fixture": {
            "command": str(Path(sys.executable).absolute()), "args": ["-c", "raise SystemExit('must not launch')"],
            "enabled": False, "exposure": "hidden"}}}), encoding="utf-8")
        # Real persisted result for the Swift → Python history-delete contract.
        from folder_memory.pi_scheduler import _database
        chat = root / 'saved-native-session.jsonl'
        chat.write_text('{"type":"session"}\n', encoding='utf-8')
        environment["HANDY_PI_SMOKE_SESSION"] = str(chat)
        with _database(root / 'state') as db:
            db.execute('INSERT INTO runs(id,job_id,title,cwd,session_path,status,started_at,finished_at,answer) VALUES(?,?,?,?,?,?,?,?,?)',
                       ('finished-fixture', 'fixture-job', 'Завершённая проверка', str(root), str(chat), 'succeeded', 1, 2, 'Тестовый сохранённый результат'))
        sources = [HERE / name for name in ("Control.swift", "SettingsData.swift", "VisibilityCheckbox.swift", "SettingsBridge.swift", "PiBridge.swift", "PiSettings.swift", "SettingsView.swift", "SettingsEditors.swift", "tests/PiSmoke.swift")]
        executable = root / "pi-smoke"
        subprocess.run(["xcrun", "swiftc", "-target", f"{os.uname().machine}-apple-macosx13.0", "-parse-as-library",
                        *(str(path) for path in sources), "-o", str(executable)], check=True, timeout=120)
        subprocess.run([str(executable)], cwd=PROJECT, env=environment, check=True, timeout=90)


if __name__ == "__main__":
    main()
