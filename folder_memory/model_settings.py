"""Native settings bridge: one private stdin request, bounded NDJSON responses.

The menu app and CLI share ModelRegistry and the exact same activation path.
There is no HTTP management listener, and API keys never appear in arguments,
JSON snapshots, process errors or provider responses shown to the user.
"""

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

from .control import engine_settings, engine_status
from .embeddings import Embedder, EmbeddingError
from .model_activation import activate_models
from .model_backend import model_config_digest
from .model_registry import ModelRegistry, _PROVIDER_CONNECTION, profile_category
from .model_setup import _index_ready
from .neural import NeuralError
from .remote_model import OpenAIModel, validate_base_url


_REQUEST_LIMIT = 1024 * 1024
_PUBLIC_FIELDS = {"id", "ready", "selected", "download_bytes", "index_ready", "auth", "category"}


def _integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name}: нужно целое число от {minimum} до {maximum}.")
    return value


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError("ID: латинские буквы, цифры, точка, дефис или подчёркивание; до 80 символов.")
    return value


def _auth(profile):
    return "env" if profile.get("api_key_env") else "file" if profile.get("api_key_file") else "none"


def _key(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 8192
            or any(not 33 <= ord(char) <= 126 for char in value.strip())):
        raise ValueError("Введи корректный ключ API без пробелов и переносов строк.")
    return value.strip()


def _request(model, path, body=None, *, seconds=15):
    """Bound the entire network operation, including slow headers/body."""
    cancelled = threading.Event()
    timer = threading.Timer(seconds, cancelled.set)
    timer.daemon = True
    timer.start()
    try:
        return model._request(path, body, timeout=seconds, cancelled=cancelled)
    except NeuralError:
        if cancelled.is_set():
            raise NeuralError("Истекло время проверки подключения.") from None
        raise
    finally:
        timer.cancel()


class _DraftModel(OpenAIModel):
    """An unsaved connection check keeps its entered key only in memory."""
    def __init__(self, state, config, key=None):
        super().__init__(state, config=config)
        self._draft_key = key

    def _key(self):
        return self._draft_key if self._draft_key is not None else super()._key()


class ModelSettings:
    def __init__(self, state, progress=None):
        self.registry = ModelRegistry(state)
        self.state = self.registry.state
        self.progress = progress or (lambda *_: None)

    def snapshot(self, *, data=None, credentials=None, revision=None, probe=True):
        data = self.registry.load() if data is None else deepcopy(data)
        revision = revision or self.registry.settings_revision(data)
        for identifier, profile in data["profiles"].items():
            data["profiles"][identifier] = {key: value for key, value in profile.items() if key not in _PUBLIC_FIELDS}
        credentials = credentials or {}

        def record(identifier, original):
            profile = {"id": identifier, **deepcopy(original)}
            profile["ready"] = self.registry.profile_ready(original, credential=credentials.get(original.get("provider_id")))
            profile["selected"] = data.get("selected_" + profile["kind"]) == identifier
            profile["download_bytes"] = sum(item.get("bytes", 0) for item in profile.get("download", {}).get("files", []))
            profile["auth"] = "key" if original.get("provider_id") in credentials else _auth(profile)
            profile["category"] = profile_category(identifier, original)
            if profile["kind"] == "embedding":
                profile["index_ready"] = profile["ready"] and _index_ready(original)
            return profile

        profiles = []
        for identifier, original in data["profiles"].items():
            profiles.append(record(identifier, original))
        providers = [{"id": identifier, **provider, "auth": "key" if identifier in credentials else _auth(provider)}
                     for identifier, provider in data.get("providers", {}).items()]
        try:
            service = engine_status(self.state, engine_settings(self.state)) if probe else {"status": "unknown"}
        except (OSError, ValueError):
            service = {"status": "unknown", "message": "Не удалось проверить состояние сервиса."}
        desired = model_config_digest(self.registry._resolved(data))
        if service["status"] == "running":
            applied = service.get("model_config") == desired
        elif service["status"] == "stopped":
            try:
                applied = model_config_digest(json.loads((self.state / "neural.json").read_text())) == desired
            except (OSError, ValueError):
                applied = False
        else:
            applied = False
        return {"config_root": str(self.registry.root), "state_dir": str(self.state),
                "selected_chat": data.get("selected_chat"), "selected_embedding": data.get("selected_embedding"),
                "profiles": profiles, "providers": providers, "service": service,
                "applied": applied,
                "binary": next((profile.get("binary") for profile in data["profiles"].values()
                                if profile.get("backend", "local") == "local" and profile.get("binary")
                                and Path(profile["binary"]).is_file()), ""),
                "settings": deepcopy(data), "revision": revision,
                "defaults": self.registry.defaults(data),
                "catalogue": [record(identifier, profile) for identifier, profile in self.registry.catalogue(data).items()]}

    def _provider(self, request):
        if request.get("provider_id") is not None:
            identifier = _identifier(request["provider_id"])
            provider = self.registry.providers().get(identifier)
            if provider is None:
                raise ValueError("Провайдер не найден.")
            return deepcopy(provider), None
        draft = request.get("provider")
        if not isinstance(draft, dict):
            raise ValueError("Нужны настройки подключения провайдера.")
        name = draft.get("name", "API")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("Введи короткое название провайдера.")
        result = {"name": name.strip(), "base_url": validate_base_url(draft.get("base_url"))}
        if "visible" in draft:
            if type(draft["visible"]) is not bool:
                raise ValueError("visible должен содержать true или false.")
            result["visible"] = draft["visible"]
        auth = draft.get("auth", "none")
        key = None
        if auth == "env":
            environment = draft.get("api_key_env", "")
            if not isinstance(environment, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", environment):
                raise ValueError("Введи имя переменной окружения с ключом.")
            result["api_key_env"] = environment
        elif auth == "file":
            path = draft.get("api_key_file", "")
            if not isinstance(path, str) or not Path(path).expanduser().is_absolute():
                raise ValueError("Укажи абсолютный путь к приватному файлу ключа.")
            result["api_key_file"] = str(Path(path).expanduser())
        elif auth == "key":
            if request.get("api_key"):
                key = _key(request["api_key"])
            else:
                original = request.get("original_id")
                previous = self.registry.providers().get(original) if isinstance(original, str) else None
                if not previous or not previous.get("api_key_file"):
                    raise ValueError("Введи ключ API.")
                result["api_key_file"] = previous["api_key_file"]
        elif auth != "none":
            raise ValueError("Выбери способ авторизации провайдера.")
        return result, key

    def _model(self, provider, key=None, model_id="connection"):
        config = {**provider, "backend": "openai", "kind": "chat", "enabled": True,
                  "model_id": model_id, "context": 4096}
        model = _DraftModel(self.state, config, key)
        model._start()
        return model

    def save_provider(self, request):
        identifier = _identifier(request.get("id"))
        original = request.get("original_id")
        if original is not None and original != identifier:
            raise ValueError("ID существующего провайдера менять нельзя.")
        provider, key = self._provider({k: v for k, v in request.items() if k != "provider_id"})
        # Validate credential references without making any network request.
        model = self._model(provider, key)
        model.close()
        key_path = None
        try:
            if key is not None:
                directory = self.registry.root / "credentials"
                if directory.is_symlink():
                    raise ValueError("Каталог ключей не должен быть символической ссылкой.")
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory.chmod(0o700)
                key_path = directory / (identifier + "-" + secrets.token_hex(12) + ".key")
                descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(key)
                provider.pop("api_key_env", None)
                provider["api_key_file"] = str(key_path)
            self.registry.save_provider(identifier, provider, replace=original is not None)
        except BaseException:
            if key_path is not None:
                key_path.unlink(missing_ok=True)
            raise
        return self.snapshot()

    def discover(self, request):
        provider, key = self._provider(request)
        model = self._model(provider, key)
        try:
            result = _request(model, "/v1/models")
        finally:
            model.close()
        records = result.get("data") if isinstance(result, dict) else None
        if not isinstance(records, list):
            raise ValueError("Провайдер не вернул список OpenAI /models. Модель можно добавить вручную по ID.")
        if len(records) > 10000:
            raise ValueError("Список провайдера слишком большой. Добавь модель вручную по ID.")
        models, seen = [], set()
        for record in records:
            identifier = record.get("id") if isinstance(record, dict) else None
            if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 1024
                    or any(ord(char) < 32 or ord(char) == 127 for char in identifier)):
                continue
            if identifier in seen:
                continue
            seen.add(identifier)
            item = {"id": identifier}
            name = record.get("name")
            if isinstance(name, str) and 0 < len(name) <= 200:
                item["name"] = name
            for field in ("context_length", "context_window", "max_model_len"):
                context = record.get(field)
                if type(context) is int and 512 <= context <= 2 ** 24:
                    item["context"] = context
                    break
            models.append(item)
        models.sort(key=lambda item: item["id"].casefold())
        return {"models": models, "count": len(models)}

    def test_provider(self, request):
        started = time.monotonic()
        result = self.discover(request)
        return {"message": "Подключено",
                "models": result["models"], "count": result["count"], "milliseconds": round((time.monotonic() - started) * 1000)}

    def refresh_models(self, request):
        """Refresh a saved connection's inventory without changing preferences.

        Discovery stays read-only for unsaved connections. Here the explicit
        operation records only missing models under one compare-and-swap, so
        merely viewing a saved provider need not create an editable UI draft.
        """
        identifier = _identifier(request.get("provider_id"))
        previous = self.registry.load()
        revision = self.registry.settings_revision(previous)
        expected = request.get("revision", revision)
        if expected != revision:
            raise ValueError("Настройки уже изменились в другом окне или терминале. Обнови список перед получением моделей.")
        provider = previous.get("providers", {}).get(identifier)
        if provider is None:
            raise ValueError("Сначала сохрани подключение провайдера.")
        # Use the captured connection throughout the GET; a change during the
        # network request is rejected by replace_settings before any write.
        result = self.discover({"provider": {**provider, "auth": _auth(provider)}})
        candidate = deepcopy(previous)
        known = {profile.get("model_id", profile.get("model")) for profile in candidate["profiles"].values()
                 if profile.get("provider_id") == identifier and profile.get("backend") == "openai"
                 and profile["kind"] == "chat"}
        added = 0
        for model in result["models"]:
            if model["id"] in known:
                continue
            base_id = identifier[:48] + "-" + hashlib.sha256(model["id"].encode()).hexdigest()[:16]
            profile_id = base_id
            suffix = 1
            while profile_id in candidate["profiles"]:
                suffix += 1
                profile_id = base_id + "-" + str(suffix)
            profile = {"kind": "chat", "backend": "openai", "enabled": True, "visible": True,
                       "name": (str(model.get("name") or model["id"]).strip() or model["id"])[:200],
                       "provider_id": identifier, "model_id": model["id"],
                       "context": model.get("context", 65536)}
            candidate["profiles"][profile_id] = self.registry._prepare_profile(
                profile_id, profile, providers=candidate.get("providers", {}), hash_local=False)
            known.add(model["id"])
            added += 1
        self.registry.replace_settings(candidate, expected_revision=expected)
        return {**self.snapshot(), **result, "added": added}

    def _prepare_profile(self, request, *, previous_settings=None, draft_settings=None, hash_local=True):
        identifier = _identifier(request.get("id"))
        original = request.get("original_id")
        if original is not None and original != identifier:
            raise ValueError("ID существующей модели менять нельзя.")
        value = request.get("profile")
        if not isinstance(value, dict):
            raise ValueError("Нужны настройки модели.")
        profile = {key: deepcopy(item) for key, item in value.items() if key not in _PUBLIC_FIELDS}
        name = profile.get("name", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("Введи короткое название модели.")
        profile["name"] = name.strip()
        kind = profile.get("kind")
        if kind not in {"chat", "embedding"}:
            raise ValueError("Выбери назначение модели: чат или эмбеддинги.")
        local = profile.get("backend", "local") == "local"
        profile["backend"] = "local" if local else profile.get("backend")
        if "enabled" in profile and type(profile["enabled"]) is not bool:
            raise ValueError("enabled должен содержать true или false.")
        profile.setdefault("enabled", True)
        previous_settings = self.registry.load() if previous_settings is None else previous_settings
        draft_settings = previous_settings if draft_settings is None else draft_settings
        previous = previous_settings["profiles"].get(identifier, {})
        _integer(profile.get("context"), "Контекст", 512 if kind == "chat" else 32,
                 (65536 if local else 2 ** 31 - 1) if kind == "chat" else 4096 if local else 32768)
        if local:
            profile.pop("provider_id", None)
            for field in _PROVIDER_CONNECTION:
                profile.pop(field, None)
            for field in ("model", "binary", "mmproj"):
                path = profile.get(field)
                if field == "mmproj" and not path:
                    profile.pop(field, None)
                    continue
                if field == "binary" and not path and profile.get("download"):
                    profile[field] = ""
                    continue
                if not isinstance(path, str) or not Path(path).expanduser().is_absolute():
                    raise ValueError(f"{field}: укажи абсолютный путь к файлу.")
                path = Path(path).expanduser()
                profile[field] = str(path)
                changed = bool(previous) and previous.get(field) != str(path)
                if not path.is_file() and (changed or (not previous and not profile.get("download"))):
                    raise ValueError(f"{field}: файл не найден.")
                if field == "binary" and path.is_file() and not os.access(path, os.X_OK):
                    raise ValueError("llama-server не является исполняемым файлом.")
                if field in {"model", "mmproj"} and path.suffix.casefold() != ".gguf":
                    raise ValueError("Для модели и проектора нужны файлы GGUF.")
            for field in ("model", "mmproj"):
                if previous.get(field) != profile.get(field):
                    pinned = next((item for item in profile.get("download", {}).get("files", [])
                                   if item.get("field") == field), {})
                    declared = (not previous and pinned.get("sha256")
                                and profile.get(field + "_sha256") == pinned["sha256"])
                    if not declared:
                        profile.pop(field + "_sha256", None)
                        profile.pop(field + "_bytes", None)
                    if previous:
                        # A profile edited to use other weights is now custom;
                        # "Download" must not put the old recommendation back.
                        profile.pop("download", None)
                        profile.pop("model_revision", None)
                        profile.pop("model_source", None)
            if "idle_sleep_seconds" in profile:
                value = profile["idle_sleep_seconds"]
                if value != -1:
                    _integer(value, "idle_sleep_seconds", 1, 86400)
            if "idle_terminate_seconds" in profile:
                _integer(profile["idle_terminate_seconds"], "idle_terminate_seconds", 0, 86400)
        else:
            provider_id = profile.get("provider_id")
            if provider_id:
                provider = draft_settings.get("providers", {}).get(provider_id)
                if provider is None:
                    raise ValueError("Сначала сохрани провайдера.")
                for field in _PROVIDER_CONNECTION:
                    profile.pop(field, None)
                    if field in provider:
                        profile[field] = provider[field]
            validate_base_url(profile.get("base_url"))
            checker = OpenAIModel(self.state, config=profile)
            try:
                checker._settings()
            finally:
                checker.close()
        if kind == "embedding":
            profile["embedding"] = True
            _integer(profile.get("dimensions"), "Размерность", 1, 65536)
            for field in ("query_prefix", "passage_prefix"):
                if not isinstance(profile.get(field, ""), str) or len(profile.get(field, "")) > 1000:
                    raise ValueError("Префикс эмбеддингов: не больше 1000 символов.")
            if local and profile.get("pooling", "mean") not in {"mean", "last", "cls"}:
                raise ValueError("Pooling: mean, last или cls.")
            profile.setdefault("max_input_chars", 1200)
            _integer(profile["max_input_chars"], "Длина входного текста", 1, 16000)
            # Use the real encoder policy, without inference, before publishing
            # a profile. Preparing also hashes changed local weights once.
            profile = self.registry._prepare_profile(identifier, profile,
                                                     providers=draft_settings.get("providers", {}), hash_local=hash_local)
            try:
                fingerprint = Embedder(SimpleNamespace(config=profile)).fingerprint
            except (EmbeddingError, TypeError, OverflowError):
                raise ValueError("Проверь бюджет токенов и настройки модели эмбеддингов.") from None
            try:
                previous_fingerprint = (Embedder(SimpleNamespace(config=previous)).fingerprint
                                        if previous.get("kind") == "embedding" else None)
            except (ValueError, TypeError, OverflowError):
                # A user must still be able to repair an older invalid profile.
                previous_fingerprint = None
            new_standard = not previous and profile_category(identifier, profile) == "standard"
            if "index_path" not in profile and fingerprint != previous_fingerprint and not new_standard:
                profile["index_path"] = str(self.registry.root / "indices" / (identifier + ".sqlite3"))
        return self.registry._prepare_profile(identifier, profile,
                                              providers=draft_settings.get("providers", {}), hash_local=hash_local)

    def save_profile(self, request):
        profile = self._prepare_profile(request)
        identifier = request["id"]
        if request.get("original_id") is None:
            self.registry.add(identifier, profile)
        else:
            self.registry.update(identifier, profile)
        return self.snapshot()

    def _draft_settings(self, request, *, hash_local=False, previous=None):
        raw = request.get("settings")
        if not isinstance(raw, dict):
            raise ValueError("Нужен полный объект настроек.")
        allowed = {"version", "selected_chat", "selected_embedding", "profiles", "providers", "standard_provider_visible"}
        if set(raw) - allowed:
            raise ValueError("Настройки содержат неподдерживаемые поля.")
        candidate = deepcopy(raw)
        profiles = candidate.get("profiles")
        providers = candidate.get("providers", {})
        if not isinstance(profiles, dict) or not isinstance(providers, dict):
            raise ValueError("Нужны объекты моделей и провайдеров.")
        for identifier, profile in profiles.items():
            if not isinstance(profile, dict):
                raise ValueError("Настройки модели должны быть объектом.")
            profiles[identifier] = {key: value for key, value in profile.items() if key not in _PUBLIC_FIELDS}
        for identifier, provider in providers.items():
            if not isinstance(provider, dict):
                raise ValueError("Настройки провайдера должны быть объектом.")
            providers[identifier] = {key: value for key, value in provider.items() if key not in {"id", "auth"}}
        pending = request.get("credentials", {})
        if not isinstance(pending, dict):
            raise ValueError("Ключи провайдеров должны быть отдельным объектом.")
        credentials = {}
        for identifier, value in pending.items():
            _identifier(identifier)
            if identifier not in providers:
                raise ValueError("Новый ключ относится к отсутствующему провайдеру.")
            credentials[identifier] = _key(value)
        self.registry._bind_providers(candidate)
        self.registry._validate(candidate)
        previous = self.registry.peek() if previous is None else previous
        for identifier, profile in list(profiles.items()):
            profiles[identifier] = self._prepare_profile(
                {"id": identifier, "profile": profile,
                 **({"original_id": identifier} if identifier in previous["profiles"] else {})},
                previous_settings=previous, draft_settings=candidate, hash_local=hash_local)
        self.registry._bind_providers(candidate)
        self.registry._validate(candidate)
        return candidate, credentials, previous

    def preview_settings(self, request):
        candidate, credentials, previous = self._draft_settings(request)
        base_revision = request.get("revision") or self.registry.settings_revision(previous)
        if not isinstance(base_revision, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", base_revision):
            raise ValueError("Нужна версия настроек.")
        return self.snapshot(data=candidate, credentials=credentials, revision=base_revision, probe=False)

    def _store_key(self, identifier, value):
        directory = self.registry.root / "credentials"
        if directory.is_symlink():
            raise ValueError("Каталог ключей не должен быть символической ссылкой.")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        path = directory / (identifier + "-" + secrets.token_hex(12) + ".key")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        return path

    def _needs_activation(self, configuration):
        expected = model_config_digest(configuration)
        status = engine_status(self.state, engine_settings(self.state))
        if status["status"] in {"running", "starting", "stopping"}:
            return status.get("model_config") != expected
        if status["status"] == "conflict":
            # Metadata-only changes remain safe with a foreign listener; a real
            # runtime change reaches activate_models, which rejects the conflict.
            return False
        try:
            cached = json.loads((self.state / "neural.json").read_text())
            return model_config_digest(cached) != expected
        except (OSError, ValueError):
            return True

    def apply_settings(self, request):
        previous = self.registry.peek()
        expected = request.get("revision")
        if self.registry.settings_revision(previous) != expected:
            raise ValueError("Настройки уже изменились в другом окне или терминале. Обнови список перед применением.")
        candidate, credentials, _ = self._draft_settings(request, hash_local=True, previous=previous)
        created, committed_revision = [], None
        try:
            for identifier, value in credentials.items():
                path = self._store_key(identifier, value)
                created.append(path)
                provider = candidate["providers"][identifier]
                provider.pop("api_key_env", None)
                provider["api_key_file"] = str(path)
            self.registry._bind_providers(candidate)
            committed = self.registry.replace_settings(candidate, expected_revision=expected)
            committed_revision = self.registry.settings_revision(committed)
            configuration = self.registry._resolved(committed)
            runtime_changed = (model_config_digest(configuration)
                               != model_config_digest(self.registry._resolved(previous)))
            if runtime_changed or self._needs_activation(configuration):
                if self.registry.revision() != committed_revision:
                    raise ValueError("Настройки изменились во время применения. Обнови окно и повтори действие.")
                activate_models(self.state, configuration, start=False)
        except BaseException:
            if committed_revision is not None:
                try:
                    self.registry.replace_settings(previous, expected_revision=committed_revision)
                except ValueError:
                    # Never roll an unrelated terminal/window edit back.
                    pass
                # A different checkout could have observed the committed key
                # reference already; retain private key files once published.
            else:
                for path in created:
                    path.unlink(missing_ok=True)
            raise
        return self.snapshot()

    def delete_profile(self, request):
        self.registry.remove(_identifier(request.get("id")))
        return self.snapshot()

    def delete_provider(self, request):
        removed = self.registry.remove_provider(_identifier(request.get("id")))
        if removed["selection_changed"]:
            activate_models(self.state, self.registry.resolved(), start=False)
        return self.snapshot()

    def select(self, request):
        identifier = request.get("id")
        kind = request.get("kind", "chat")
        if kind not in {"chat", "embedding"}:
            raise ValueError("kind должен быть chat или embedding.")
        if identifier is None and kind != "embedding":
            raise ValueError("Выбери основную модель.")
        previous = self.registry.load()
        if identifier is not None:
            _identifier(identifier)
            profile = previous["profiles"].get(identifier)
            if not profile or profile["kind"] != kind:
                raise ValueError("Выбери существующий профиль kind " + kind + ".")
            if not self.registry.profile_ready(profile):
                raise ValueError("Модель ещё не готова: проверь файлы, runtime или ключ провайдера.")
        candidate = deepcopy(previous)
        candidate["selected_" + kind] = identifier
        if identifier is not None:
            candidate["profiles"][identifier]["enabled"] = True
        committed = self.registry.replace_settings(candidate, expected_revision=self.registry.settings_revision(previous))
        revision = self.registry.settings_revision(committed)
        try:
            activate_models(self.state, self.registry._resolved(committed), start=False)
        except BaseException:
            try:
                self.registry.replace_settings(previous, expected_revision=revision)
            except ValueError:
                # Keep a concurrent terminal/window edit, as apply_settings does.
                pass
            raise
        return self.snapshot()

    def apply(self, request):
        selected = self.registry.load().get("selected_chat")
        if not selected or not self.registry.ready(selected):
            raise ValueError("Сначала установи или настрой основную модель.")
        activate_models(self.state, self.registry.resolved(), start=False, force=True)
        return self.snapshot()

    def start_service(self, request):
        """Start from the global choice, including an absent/stale local cache."""
        data = self.registry.load()
        selected = data.get("selected_chat")
        if selected is not None:
            profile = data["profiles"][selected]
            if not profile.get("enabled", True) or not self.registry.profile_ready(profile):
                raise ValueError("Сначала выбери и настрой основную модель: скачай веса или проверь подключение к API.")
        # None is an intentional no-chat configuration after a removed provider;
        # background tasks may still run, but an old provider must never return.
        activate_models(self.state, self.registry._resolved(data), start=True)
        return self.snapshot()

    def download(self, request):
        identifier = _identifier(request.get("id"))
        if identifier not in self.registry.profiles():
            factory = self.registry.catalogue().get(identifier)
            if factory is None:
                raise ValueError("Модель отсутствует в каталоге скачивания.")
            self.registry.add(identifier, factory)
        self.registry.download(identifier, self.progress)
        return self.snapshot()

    def restore_catalogue(self, request):
        self.registry.restore_catalogue()
        return self.snapshot()

    def test_profile(self, request):
        identifier = _identifier(request.get("id"))
        credentials = {}
        if "settings" in request:
            data, credentials, _ = self._draft_settings(request)
            profile = data["profiles"].get(identifier)
            if profile is None:
                raise ValueError("Модель не найдена в настройках.")
        else:
            profile = self.registry.get(identifier)
        credential = credentials.get(profile.get("provider_id"))
        if not self.registry.profile_ready(profile, credential=credential):
            raise ValueError("Модель пока не готова: проверь файлы, runtime и авторизацию.")
        started = time.monotonic()
        if profile.get("backend", "local") == "local":
            paths = [profile["model"]] + ([profile["mmproj"]] if profile.get("mmproj") else [])
            for path in paths:
                with Path(path).open("rb") as stream:
                    header = stream.read(8)
                if len(header) != 8 or header[:4] != b"GGUF" or struct.unpack("<I", header[4:])[0] not in {2, 3}:
                    raise ValueError("Файл модели или проектора не содержит поддерживаемый заголовок GGUF.")
            return {"message": "Файлы модели и llama-server найдены.",
                    "milliseconds": round((time.monotonic() - started) * 1000)}
        model = _DraftModel(self.state, profile, credential)
        try:
            if profile["kind"] == "embedding":
                result = _request(model, "/v1/embeddings", {"input": ["connection test"]}, seconds=30)
                records = result.get("data") if isinstance(result, dict) else None
                vector = records[0].get("embedding") if isinstance(records, list) and len(records) == 1 and isinstance(records[0], dict) else None
                if not isinstance(vector, list) or not vector or any(type(item) not in {float, int} for item in vector):
                    raise ValueError("API не вернул вектор эмбеддинга.")
                if len(vector) != profile["dimensions"]:
                    raise ValueError(f"API вернул размерность {len(vector)}, в профиле указана {profile['dimensions']}.")
            else:
                result = _request(model, "/v1/chat/completions", {"messages": [{"role": "user", "content": "Reply OK."}],
                                                              "max_tokens": 16, "stream": False}, seconds=30)
                records = result.get("choices") if isinstance(result, dict) else None
                message = records[0].get("message") if isinstance(records, list) and records and isinstance(records[0], dict) else None
                # Reasoning models can spend the deliberately small probe
                # budget before emitting final text. This still proves that
                # the selected model accepted and processed our request.
                if not isinstance(message, dict) or not any(isinstance(message.get(field), str) and message[field].strip()
                                                           for field in ("content", "reasoning_content", "reasoning")):
                    raise ValueError("API ответил без текста. Проверь совместимость модели с Chat Completions.")
        finally:
            model.close()
        return {"message": "Подключено", "milliseconds": round((time.monotonic() - started) * 1000)}

    def rebuild_index(self, request):
        identifier = _identifier(request.get("id"))
        profile = self.registry.get(identifier)
        if profile["kind"] != "embedding" or not self.registry.ready(identifier):
            raise ValueError("Выбери установленную модель эмбеддингов.")
        profile.setdefault("index_path", str(self.registry.root / "indices" / (identifier + ".sqlite3")))
        self.registry.update(identifier, profile)
        configuration = self.registry.resolved()
        configuration["embedding"] = {key: value for key, value in profile.items() if key not in {"kind", "download"}}
        from .embeddings import Embedder
        from .model_backend import create_model
        from scripts.build_semantic_index import build_index, documents
        with tempfile.TemporaryDirectory(prefix="handy-index-settings-") as temporary:
            state = Path(temporary)
            # A dedicated embedding worker never borrows the active core's port.
            with socket.socket() as connection:
                connection.bind(("127.0.0.1", 0))
                configuration["embedding"]["port"] = connection.getsockname()[1]
            config = state / "neural.json"
            config.write_text(json.dumps(configuration), encoding="utf-8")
            config.chmod(0o600)
            token = state / "token"
            token.write_text(secrets.token_urlsafe(32))
            token.chmod(0o600)
            model = create_model(state, profile="embedding")
            try:
                _, stats = build_index(profile["index_path"], Embedder(model), documents())
            finally:
                model.close()
        return {"message": "Индекс собран. Нажми «Применить», чтобы сервис использовал обновлённые настройки.",
                "index_ready": _index_ready(profile), "stats": stats}

    def perform(self, request):
        if not isinstance(request, dict):
            raise ValueError("Нужен объект запроса JSON.")
        operation = request.get("operation", request.get("op"))
        supported = {"snapshot", "preview_settings", "apply_settings", "start_service", "save_provider", "discover", "refresh_models", "test_provider", "save_profile", "delete_profile",
                     "delete_provider", "select", "apply", "download", "restore_catalogue", "test_profile", "rebuild_index"}
        if not isinstance(operation, str) or operation not in supported:
            raise ValueError("Неизвестное действие настроек.")
        return self.snapshot() if operation == "snapshot" else getattr(self, operation)(request)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    arguments = parser.parse_args()

    def emit(event, **fields):
        print(json.dumps({"event": event, **fields}, ensure_ascii=False, allow_nan=False), flush=True)

    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        raw = sys.stdin.buffer.read(_REQUEST_LIMIT + 1)
        if len(raw) > _REQUEST_LIMIT:
            raise ValueError("Запрос настроек слишком большой.")
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Не удалось прочитать запрос настроек.") from None
        manager = ModelSettings(arguments.state_dir,
                                lambda file, done, total: emit("progress", file=file, done=done, total=total))
        result = manager.perform(request)
        emit("result", data=result)
    except KeyboardInterrupt:
        emit("error", message="Действие отменено.")
        raise SystemExit(130)
    except (ValueError, NeuralError) as error:
        emit("error", message=str(error))
        raise SystemExit(1)
    except (OSError, subprocess.SubprocessError):
        emit("error", message="Не удалось выполнить действие. Проверь файлы и доступность сервиса.")
        raise SystemExit(1)
    except Exception:
        # A malformed request must never expose a traceback or provider content
        # through the process error channel used by the native app.
        emit("error", message="Не удалось обработать настройки. Обнови список и попробуй ещё раз.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
