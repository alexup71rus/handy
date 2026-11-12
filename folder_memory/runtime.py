"""Private, shared configuration and process locks for a Handy checkout."""

from contextlib import contextmanager
import fcntl
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time


DEFAULT_PORT = 18746
LAUNCHERS = ("opencode", "managed", "systemd")
OWNERS = ("foreground", *LAUNCHERS)
ENGINE_BUSY = 73


def runtime_dir():
    return Path(__file__).resolve().parent.parent / ".runtime"


def engine_settings(state):
    try:
        config = json.loads((state / "engine.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("Handy engine.json must contain a JSON object.")
    port = config.get("port", DEFAULT_PORT)
    launcher = config.get("launcher", "opencode")
    python = config.get("python", sys.executable)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Handy engine port must be between 1 and 65535.")
    if launcher not in LAUNCHERS:
        raise ValueError("Invalid Handy launcher in engine.json.")
    if not isinstance(python, str) or not os.path.isabs(python):
        raise ValueError("Handy engine.json requires an absolute Python path.")
    return {**config, "python": python, "port": port, "launcher": launcher}


def write_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_token(directory, *, secure=False):
    fd = os.open(directory / "token", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as existing:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Файл локального токена должен быть обычным файлом.")
        raw = existing.read(256)
        if len(raw) == 256 or not re.fullmatch(rb"[A-Za-z0-9_-]{32,128}", raw.strip()):
            raise ValueError("Локальный токен Handy повреждён. Восстанови файл token из закрытой резервной копии Handy.")
        if secure:
            os.fchmod(fd, 0o600)
        return raw.strip().decode("ascii")


def get_token(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "token"
    if not path.exists():
        # Publish a complete token. Concurrent starts use the same winner.
        with tempfile.NamedTemporaryFile("w", dir=directory, prefix=".token-", encoding="ascii") as new:
            new.write(secrets.token_urlsafe(32))
            new.flush()
            try:
                os.link(new.name, path)
            except FileExistsError:
                pass
    return read_token(directory, secure=True)


def service_proof(token, nonce, state, status):
    """Prove the listener owns this runtime without sending it the API token."""
    fields = ["handy-service", nonce, str(state), str(status["pid"]), status["owner"], str(int(status["stopped"]))]
    return hmac.digest(token.encode("ascii"), "\n".join(fields).encode("utf-8"), "sha256").hex()


def model_config_proof(token, nonce, state, status):
    return service_proof(token, 'model-config:' + nonce + ':' + status['model_config'], state, status)


@contextmanager
def process_lock(path, *, wait=False, create=True, retry=False):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags | (os.O_CREAT if create else 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"Expected a regular lock file: {path.name}")
        mode = fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, mode)
        except BlockingIOError:
            if not retry:
                raise
            # A status probe can hold the lock for an instant to test it.
            time.sleep(0.025)
            fcntl.flock(fd, mode)
        yield fd
    finally:
        os.close(fd)


def lock_held(path):
    try:
        with process_lock(path, create=False):
            return False
    except FileNotFoundError:
        return False
    except BlockingIOError:
        return True


def watches_paused(state):
    return (state / "watches.paused").exists()


def set_watches_paused(state, paused):
    path = state / "watches.paused"
    if paused:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        os.close(fd)
    else:
        path.unlink(missing_ok=True)
