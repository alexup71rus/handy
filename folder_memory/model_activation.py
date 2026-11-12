"""Apply model settings without letting a chat silently reach the old backend."""

import os
from pathlib import Path
import tempfile

from .control import engine_settings, engine_status, start_engine, stop_engine
from .model_backend import model_config_digest
from .model_registry import ModelRegistry
from .runtime import process_lock


def _restore(path, contents):
    if contents is None:
        path.unlink(missing_ok=True)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('wb', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(contents)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def activate_models(state, configuration, *, start=True, force=False):
    """Stop before publishing; prove the started service uses this exact snapshot."""
    state = Path(state).expanduser().resolve()
    expected = model_config_digest(configuration)
    registry = ModelRegistry(state)
    with process_lock(state / 'control.lock', wait=True):
        settings = engine_settings(state)
        status = engine_status(state, settings)
        if status['status'] == 'conflict':
            raise ValueError(status['message'])
        path = state / 'neural.json'
        if path.is_symlink():
            raise ValueError('neural.json должен быть обычным файлом.')
        previous = path.read_bytes() if path.exists() else None
        was_running = status['status'] in {'running', 'starting', 'stopping'}
        restart = was_running and (force or status.get('model_config') != expected)
        if restart:
            # Keep the old disk configuration until shutdown has succeeded.
            stop_engine(state, settings)
        attempted_start = False
        try:
            registry.materialize(configuration=configuration)
            if not (start or was_running):
                return status
            attempted_start = True
            active = start_engine(state, settings)
            if active.get('model_config') != expected:
                raise ValueError('Служба не подтвердила выбранную модель. Диалог не запущен.')
            return active
        except BaseException:
            try:
                if attempted_start:
                    stop_engine(state, engine_settings(state))
                _restore(path, previous)
                if was_running:
                    start_engine(state, engine_settings(state))
            except Exception as restore_error:
                raise ValueError('Не удалось применить модель и восстановить службу. Проверь handyctl status и logs.') from restore_error
            raise
