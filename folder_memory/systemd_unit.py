"""Shared identity and inspection for the installer-managed user unit."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import subprocess


UNIT = "handy.service"
_MARKER = b"# Managed by the Handy systemd installer.\n"
_PROPERTIES = ("LoadState", "ActiveState", "FragmentPath", "DropInPaths", "NeedDaemonReload")


def unit_file():
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config_home.is_absolute():
        config_home = Path.home() / ".config"
    return (config_home / "systemd" / "user").resolve() / UNIT


def escape_unit_path(path):
    value = str(path)
    if not path.is_absolute() or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError(f"Unsupported path for systemd: {value!r}")
    return value.replace("%", "%%")


def unit_header(project, state=None):
    runtime = state if state is not None else (project / ".runtime").resolve()
    identity = (f"# Checkout: {escape_unit_path(project)!r}\n"
                f"# State directory: {escape_unit_path(runtime)!r}\n")
    return _MARKER + identity.encode("utf-8")


def read_managed_unit(unit):
    try:
        fd = os.open(unit, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"Expected a regular systemd unit file at {unit}.")
        content = stream.read()
    if not content.startswith(_MARKER):
        raise ValueError(f"Refusing to use an unmanaged {unit}.")
    return content


@contextmanager
def unit_directory_lock(unit, *, installing=False):
    try:
        fd = os.open(unit.parent, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        raise ValueError(f"No installer-managed {UNIT} is registered in {unit.parent}.") from None
    try:
        # The installer can call configure while holding its lock. A CLI
        # command already holds the runtime lock, so it must never wait here.
        flags = fcntl.LOCK_EX if installing else fcntl.LOCK_SH | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            raise ValueError("A Handy systemd install is in progress. Retry after it finishes.") from None
        yield
    finally:
        os.close(fd)


def user_systemctl(*arguments, capture=False):
    return subprocess.run(
        ["systemctl", "--user", "--no-pager", *arguments],
        check=True, text=True, stdout=subprocess.PIPE if capture else None, timeout=55,
    )


def unit_properties():
    result = user_systemctl("show", "--property=" + ",".join(_PROPERTIES), UNIT, capture=True)
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if any(key not in fields for key in _PROPERTIES):
        raise ValueError("Could not verify the Handy unit in the systemd user manager.")
    return fields


def fragment_matches(unit, fields):
    fragment = Path(fields["FragmentPath"])
    return fragment.is_absolute() and fragment.resolve() == unit
