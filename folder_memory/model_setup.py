"""Explicit global model setup; opening a chat never downloads weights silently."""

import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from .model_registry import ModelRegistry


def _ask(prompt, text, default=''):
    value = prompt(text + (f' [{default}]' if default else '') + ' › ', cancel_on_escape=True).strip()
    return value or default


def _binary(registry):
    for profile in registry.profiles().values():
        path = profile.get('binary')
        if path and Path(path).is_file():
            return path
    return shutil.which('llama-server') or ''


def choose_profile(registry, kind, console, prompt):
    from .model_console import choose_profile as choose
    return choose(registry, kind, console, prompt)


def _index_ready(profile):
    from .embeddings import Embedder
    path = Path(profile.get('index_path') or Path(__file__).resolve().parents[1] / 'data/semantic.sqlite3')
    if not path.is_file():
        return False
    try:
        fingerprint = Embedder(SimpleNamespace(config=profile)).fingerprint
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            return connection.execute('SELECT 1 FROM documents WHERE model_hash=? LIMIT 1', (fingerprint,)).fetchone() is not None
        finally:
            connection.close()
    except (OSError, ValueError, sqlite3.Error):
        return False


def _offer_index(registry, identifier, console, prompt):
    profile = registry.get(identifier)
    profile.setdefault('index_path', str(registry.path.parent / 'indices' / (identifier + '.sqlite3')))
    registry.update(identifier, profile)
    console.print('Другому энкодеру нужен свой индекс. Справка и память доступны и без него.', markup=False)
    if profile['backend'] == 'openai':
        console.print('При сборке технические карточки будут отправлены выбранному API.', style='dim', markup=False)
    if _ask(prompt, 'Пересобрать индекс сейчас? да/нет').casefold() not in {'да', 'yes', 'y'}:
        return
    script = Path(__file__).resolve().parents[1] / 'scripts/build_semantic_index.py'
    with tempfile.TemporaryDirectory(prefix='handy-encoder-') as temporary:
        config = Path(temporary) / 'neural.json'
        config.write_text(json.dumps(registry.resolved()), encoding='utf-8')
        config.chmod(0o600)
        with console.status('Строю индекс…'):
            result = subprocess.run([sys.executable, str(script), '--config', str(config),
                                     '--output', profile['index_path']], capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise ValueError('Индекс не собран. Старый индекс сохранён; настройки нового энкодера оставлены.')
    console.print('Индекс собран.', markup=False)


def model_menu(state, console, prompt, **options):
    from .model_console import model_menu as browse
    return browse(state, console, prompt, **options)


def prepare_models(state, *, interactive, console=None, prompt=None):
    """Validate the desired global selection without changing the running service."""
    registry = ModelRegistry(state)
    data = registry.load()
    identifier = data['selected_chat']
    if not identifier or not registry.ready(identifier) or not registry.get(identifier).get('enabled', True):
        if not interactive:
            raise ValueError('Модель не настроена. Запусти handy --models: можно скачать веса или подключить API.')
        console.print('Выбери локальную модель или внешний API.', markup=False)
        choose_profile(registry, 'chat', console, prompt)
        data = registry.load()
        identifier = data['selected_chat']
        if not identifier or not registry.ready(identifier):
            raise ValueError('Основная модель не выбрана.')
    encoder = data['selected_embedding']
    if encoder and not registry.ready(encoder):
        if interactive:
            console.print('Энкодер не установлен. Можно скачать его, выбрать другой/API или работать без него.', markup=False)
            choose_profile(registry, 'embedding', console, prompt)
        # A missing optional encoder does not prevent chat or erase its global
        # preference. The core simply leaves vector retrieval inactive.
    return registry.resolved()
