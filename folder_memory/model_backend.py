"""Choose a configured transport without changing the model/tool conversation."""

import json
import hashlib
from pathlib import Path

from .neural import LocalModel, NeuralError


def read_model_config(state_dir):
    path = Path(state_dir) / 'neural.json'
    return json.loads(path.read_text()) if path.exists() else {}


def model_config_digest(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def create_model(state_dir, *, profile=None, config=None):
    config = read_model_config(state_dir) if config is None else config
    selected = config.get(profile, {}) if profile else config
    backend = selected.get('backend', 'local')
    if backend == 'local':
        return LocalModel(state_dir, profile=profile, config=config)
    if backend == 'openai':
        from .remote_model import OpenAIModel
        return OpenAIModel(state_dir, profile=profile, config=selected)
    raise NeuralError('Неизвестный backend модели: выбери local или openai.')
