"""Register Handy with the current Linux user's systemd manager."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
from folder_memory.systemd_unit import (  # noqa: E402
    UNIT, escape_unit_path, fragment_matches, read_managed_unit, unit_directory_lock, unit_file,
    unit_header, unit_properties, user_systemctl,
)


def _argument(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_unit(python, project, state_dir, header):
    executable = escape_unit_path(python)
    if any(char in executable for char in "\\\"'"):
        raise ValueError("systemd requires a Python executable path without quotes or backslashes.")
    # The colon prefix disables systemd's environment expansion in ExecStart.
    command = [_argument(":" + executable), "-m", "folder_memory.control"]
    if state_dir is not None:
        command.extend(("--state-dir", _argument(escape_unit_path(state_dir))))
    command.append("run")
    # WorkingDirectory is a path value and keeps spaces without word quoting.
    return header + f"""[Unit]
Description=Handy local service

[Service]
Type=exec
WorkingDirectory={escape_unit_path(project)}/
ExecStart={" ".join(command)}
Restart=on-failure
RestartSec=1s
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=45s

[Install]
WantedBy=default.target
""".encode("utf-8")


def _guard_unit(unit, *, installed):
    fields = unit_properties()
    if f"{UNIT}.d/" in fields["DropInPaths"]:
        raise RuntimeError("Handy has systemd overrides. Review them before installing.")
    if fields["LoadState"] == "not-found":
        return False
    if not installed or not fragment_matches(unit, fields):
        raise RuntimeError(f"Another unit already uses {UNIT}.")
    if fields["LoadState"] != "loaded":
        raise RuntimeError(f"systemd could not load {unit} ({fields['LoadState']}).")
    if fields["ActiveState"] not in ("inactive", "failed"):
        raise RuntimeError(f"Stop the managed service with 'systemctl --user stop {UNIT}' before reinstalling.")
    return True


def _write_unit(unit, content, *, replace):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=unit.parent, prefix=f".{UNIT}-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        if replace:
            os.replace(temporary, unit)
        else:
            os.link(temporary, unit)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _install(unit, python, project, state_dir, autostart):
    header = unit_header(project, state_dir)
    content = _render_unit(python, project, state_dir, header)
    configure = [str(python), "-m", "folder_memory.control"]
    if state_dir is not None:
        configure.extend(("--state-dir", str(state_dir)))
    configure.extend(("configure", "--launcher", "systemd"))
    unit.parent.mkdir(parents=True, exist_ok=True)
    with unit_directory_lock(unit, installing=True):
        previous = read_managed_unit(unit)
        if previous is not None and not previous.startswith(header):
            raise RuntimeError(f"{UNIT} is registered for another Handy checkout or state directory.")
        _guard_unit(unit, installed=previous is not None)
        changed = previous != content
        if changed:
            _write_unit(unit, content, replace=previous is not None)
        try:
            user_systemctl("daemon-reload")
            if not _guard_unit(unit, installed=True):
                raise RuntimeError(f"The user manager did not load {unit}. Check its configuration directory.")
            subprocess.run(configure, cwd=project, check=True)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            if changed and read_managed_unit(unit) == content:
                if previous is None:
                    unit.unlink()
                else:
                    _write_unit(unit, previous, replace=True)
                user_systemctl("daemon-reload")
            raise
        if autostart:
            user_systemctl("enable", UNIT)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, help="Use a different Handy state directory.")
    parser.add_argument("--autostart", action="store_true", help="Enable the service for future logins.")
    args = parser.parse_args(argv)
    if sys.platform != "linux" or os.geteuid() == 0:
        parser.error("Run this installer as a regular user in a Linux systemd session.")
    if sys.version_info < (3, 11):
        parser.error("Handy requires Python 3.11 or later.")
    try:
        unit = unit_file()
        state_dir = args.state_dir.expanduser().resolve() if args.state_dir is not None else None
        # Preserve a virtual environment's interpreter path when it is a symlink.
        python = Path(sys.executable).absolute()
        _install(unit, python, PROJECT, state_dir, args.autostart)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Handy systemd installation failed: {error}\n")
    print(f"Registered {unit}")
    print(f"Start with: systemctl --user start {UNIT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
