"""Private, global model profiles and explicit, verified GGUF downloads.

Profiles contain the same runtime settings as ``neural.json`` plus ``kind``,
``backend`` and optional download provenance. Merely loading or selecting a
profile never makes a network request or starts a model.
"""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import stat
import tempfile
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .runtime import process_lock


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_REVISION = re.compile(r"[0-9a-fA-F]{40}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_METADATA_LIMIT = 2 * 1024 * 1024
_CHUNK = 1024 * 1024
_DOWNLOAD_SECONDS = 6 * 60 * 60
_SECRET_KEYS = {"api_key", "apikey", "api_token", "access_token", "authorization"}
_PROVIDER_CONNECTION = ("base_url", "api_key_env", "api_key_file")
_RUNTIME_OPTIONS = {
    "context", "sampling", "enable_thinking", "max_output_tokens", "image_max_tokens",
    "batch_tokens", "micro_batch_tokens", "cache_type_k", "cache_type_v", "flash_attention",
    "idle_sleep_seconds", "idle_terminate_seconds", "dimensions", "pooling", "query_prefix",
    "passage_prefix", "max_input_chars", "send_dimensions",
}

# This catalogue is data. Runtime adapters dispatch on backend, never model ID.
_CATALOGUE = {
    "lfm3-vl": {
        "kind": "chat", "backend": "local", "enabled": True,
        "name": "LFM3-VL-2B-Q6_K", "port": 18747, "context": 65536,
        "image_max_tokens": 512, "enable_thinking": True,
        "sampling": {"temperature": 0.1, "top_k": 50, "repeat_penalty": 1.1},
        "batch_tokens": 512, "micro_batch_tokens": 256,
        "cache_type_k": "q8_0", "cache_type_v": "q8_0", "flash_attention": "on",
        "idle_sleep_seconds": 60, "idle_terminate_seconds": 120,
        "license": "LFM Open License v1.0",
        "download": {
            "repo": "LiquidAI/LFM3-VL-2B-GGUF",
            "revision": "1add221a5ea6c047fcf1eaaf2d247939d7cf8849",
            "files": [
                {"field": "model", "filename": "LFM3-VL-2B-Q6_K.gguf", "bytes": 1427638304,
                 "sha256": "ca44f8ccafd906c359d603b74689c50a939ea099b4f414e7ab86c02dd36c2769"},
                {"field": "mmproj", "filename": "mmproj-LFM3-VL-2B-F16.gguf", "bytes": 378996032,
                 "sha256": "1d8e6e58eefc093e09d6332f081516e1ddbd5b35cfca7f35463fb270db230b1e"},
            ],
        },
    },
    "spark": {
        "kind": "chat", "backend": "local", "enabled": True,
        "name": "Spark-X2.6-4B-Q8_0", "port": 18747, "context": 32768,
        "enable_thinking": False, "batch_tokens": 512, "micro_batch_tokens": 256,
        "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": -1, "repeat_penalty": 1.0},
        "cache_type_k": "q8_0", "cache_type_v": "q8_0", "flash_attention": "on",
        "idle_sleep_seconds": 60, "idle_terminate_seconds": 120,
        "download": {
            "repo": "XHToken/Spark-X2.6-4B-GGUF",
            "revision": "9d716770ac7477776de5f891f70cdf98b003a5cd",
            "files": [
                {"field": "model", "filename": "Spark-X2.6-4B-Q8_0.gguf", "bytes": 4375021824,
                 "sha256": "145c2aa606820663f26ad530b8fc141ed885e2e9ca29f9be7c3bfe8931d58b81"},
            ],
        },
    },
    "qwen3-embedding": {
        "kind": "embedding", "backend": "local", "enabled": True,
        "name": "Qwen3-Embedding-0.6B-Q8_0", "port": 18749, "embedding": True,
        "context": 512, "batch_tokens": 512, "micro_batch_tokens": 512,
        "dimensions": 1024, "pooling": "last", "passage_prefix": "",
        "query_prefix": "Instruct: Given a user request, retrieve relevant technical reference information or examples that help fulfill the request.\nQuery: ",
        "max_input_chars": 1200, "idle_terminate_seconds": 30,
        "license": "Apache-2.0",
        "download": {
            "repo": "Qwen/Qwen3-Embedding-0.6B-GGUF",
            "revision": "370f27d7550e0def9b39c1f16d3fbaa13aa67728",
            "files": [
                {"field": "model", "filename": "Qwen3-Embedding-0.6B-Q8_0.gguf", "bytes": 639150592,
                 "sha256": "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439"},
            ],
        },
    },
}


def config_root():
    """Return the exact HANDY_CONFIG_HOME override, or XDG's handy directory."""
    name = 'HANDY_CONFIG_HOME' if 'HANDY_CONFIG_HOME' in os.environ else 'XDG_CONFIG_HOME'
    value = os.environ.get(name, '~/.config')
    directory = Path(value).expanduser()
    if not value.strip() or not directory.is_absolute():
        raise ValueError(name + ' должен содержать непустой абсолютный путь.')
    root = directory if name == 'HANDY_CONFIG_HOME' else directory / 'handy'
    resolved = root.resolve()
    if resolved.parent == resolved:
        raise ValueError('Корень файловой системы нельзя использовать как каталог настроек Handy.')
    return root


def file_sha256(path):
    """Hash local weights without reading them all into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_binary(profile):
    binary = profile.get("binary")
    if isinstance(binary, str) and binary and Path(binary).expanduser().is_file():
        return str(Path(binary).expanduser())
    return shutil.which("llama-server") or ""


def _local_ready(profile):
    """Filesystem-only readiness; safe inside a registry mutation lock."""
    binary = _local_binary(profile)
    keys = ["model"] + (["mmproj"] if "mmproj" in profile else [])
    return (bool(binary) and os.access(Path(binary).expanduser(), os.X_OK)
            and all(isinstance(profile.get(key), str) and profile[key]
                    and Path(profile[key]).expanduser().is_file() for key in keys))


def profile_category(identifier, profile):
    """Presentation metadata, derived from provenance rather than user labels."""
    if profile.get("backend", "local") != "local":
        return "custom"
    standard = _CATALOGUE.get(identifier)
    if standard and profile.get("kind") == standard["kind"] and profile.get("download") == standard["download"]:
        return "standard"
    return "custom"


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Каталог настроек Handy должен быть обычным каталогом.")
    path.chmod(0o700)


def _write_json(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + "-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _no_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_") if isinstance(key, str) else ""
            if normalized in _SECRET_KEYS or normalized.endswith("_api_key"):
                raise ValueError("Ключ API нельзя хранить в models.json: используй api_key_env или api_key_file.")
            _no_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _no_secrets(item)


def _download_spec(profile):
    spec = profile.get("download")
    if not isinstance(spec, dict) or not _REPOSITORY.fullmatch(str(spec.get("repo", ""))):
        raise ValueError("Для скачивания нужен Hugging Face repo owner/name.")
    if not _REVISION.fullmatch(str(spec.get("revision", ""))):
        raise ValueError("Скачивание требует закреплённый commit Hugging Face.")
    files = spec.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 16:
        raise ValueError("Нужен список файлов модели для скачивания.")
    fields, names = set(), set()
    for item in files:
        if not isinstance(item, dict) or item.get("field") not in {"model", "mmproj"}:
            raise ValueError("Файл модели должен иметь field model или mmproj.")
        name = item.get("filename")
        if (not isinstance(name, str) or not name or "\\" in name or any(ord(c) < 32 for c in name)
                or PurePosixPath(name).is_absolute() or any(p in {"", ".", ".."} for p in name.split("/"))):
            raise ValueError("Недопустимое имя файла модели.")
        if item["field"] in fields or name in names:
            raise ValueError("Файлы и поля скачивания не должны повторяться.")
        fields.add(item["field"])
        names.add(name)
        if item.get("sha256") is not None and not _SHA256.fullmatch(str(item["sha256"])):
            raise ValueError("Недопустимый SHA256 модели.")
        if "bytes" in item and (type(item["bytes"]) is not int or item["bytes"] <= 0):
            raise ValueError("Размер файла модели должен быть положительным целым числом.")
    if "model" not in fields:
        raise ValueError("В скачивании отсутствуют веса model.")
    return spec


def _validate_profile(model_id, profile):
    if not isinstance(model_id, str) or not _ID.fullmatch(model_id):
        raise ValueError("ID модели: латинские буквы, цифры, точка, дефис и подчёркивание; до 80 символов.")
    if not isinstance(profile, dict) or profile.get("kind") not in {"chat", "embedding"}:
        raise ValueError("Профиль модели должен иметь kind chat или embedding.")
    if profile.get("backend", "local") not in {"local", "openai"}:
        raise ValueError("Поддерживаются backend local и openai.")
    if not isinstance(profile.get("name"), str) or not profile["name"].strip():
        raise ValueError("Профиль модели должен иметь непустое имя name.")
    if "visible" in profile and type(profile["visible"]) is not bool:
        raise ValueError("visible должен содержать true или false.")
    if "enabled" in profile and type(profile["enabled"]) is not bool:
        raise ValueError("enabled должен содержать true или false.")
    _no_secrets(profile)
    for key in ("api_key_env", "api_key_file"):
        if key in profile and (not isinstance(profile[key], str) or not profile[key].strip()):
            raise ValueError(key + " должен содержать имя переменной или путь к файлу.")
    if "api_key_env" in profile and not _ENV_NAME.fullmatch(profile["api_key_env"]):
        raise ValueError("Некорректное имя переменной api_key_env.")
    if "api_key_env" in profile and "api_key_file" in profile:
        raise ValueError("Используй одну ссылку на ключ: api_key_env или api_key_file.")
    if 'api_key_file' in profile and not Path(profile['api_key_file']).expanduser().is_absolute():
        raise ValueError('api_key_file в глобальном реестре должен содержать абсолютный путь.')
    if profile.get("backend", "local") == "openai":
        from .remote_model import validate_base_url
        validate_base_url(profile.get("base_url"))
        model = profile.get("model_id", profile.get("model"))
        if not isinstance(model, str) or not model.strip() or len(model) > 1024:
            raise ValueError("Для удалённого профиля нужен model_id провайдера.")
        for key in ("context", "dimensions"):
            if key in profile and (type(profile[key]) is not int or profile[key] <= 0):
                raise ValueError(key + " должен быть положительным целым числом.")
    if "download" in profile:
        if profile.get("backend", "local") != "local":
            raise ValueError("Удалённая модель не требует скачивания весов.")
        _download_spec(profile)
    try:
        json.dumps(profile, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Профиль модели должен содержать обычные значения JSON.") from exc


def _validate_provider(identifier, provider):
    if not isinstance(provider, dict):
        raise ValueError("Настройки провайдера должны быть объектом JSON.")
    unknown = set(provider) - {"name", "visible", *_PROVIDER_CONNECTION}
    if unknown:
        raise ValueError("Настройки провайдера содержат неподдерживаемые поля.")
    _validate_profile(identifier, {**provider, "kind": "chat", "backend": "openai", "model_id": "connection"})


class ModelRegistry:
    """Read/write global profiles; returned dictionaries are independent copies.

    ``load()`` (also the ``data`` property) creates the registry once, importing
    this runtime's existing neural.json. ``profiles(kind)`` returns an ID to
    profile mapping. Mutations reload under a cross-process lock, so separate
    Handy checkouts cannot silently overwrite each other's profile changes.
    """

    def __init__(self, state):
        self.state = Path(state).expanduser().resolve()
        self.root = config_root()
        self.path = self.root / "models.json"

    @contextmanager
    def _locked(self):
        _private_directory(self.root)
        with process_lock(self.root / "models.lock", wait=True) as fd:
            os.fchmod(fd, 0o600)
            yield

    def _initial(self):
        legacy_path = self.state / "neural.json"
        legacy = json.loads(legacy_path.read_text(encoding="utf-8")) if legacy_path.exists() else {}
        if not isinstance(legacy, dict):
            raise ValueError("neural.json должен содержать объект JSON.")
        nested = legacy.get("embedding")
        embedding = nested if isinstance(nested, dict) else {}
        binary = next((str(Path(candidate).expanduser()) for candidate in
                       (legacy.get("binary", ""), embedding.get("binary", ""))
                       if isinstance(candidate, str) and candidate and Path(candidate).expanduser().is_file()),
                      shutil.which("llama-server") or "")
        profiles = deepcopy(_CATALOGUE)
        for profile in profiles.values():
            profile["binary"] = binary
            spec = profile["download"]
            profile["model_revision"] = spec["revision"]
            profile["model_source"] = "https://huggingface.co/" + spec["repo"]
            for item in spec["files"]:
                field, filename = item["field"], item["filename"]
                old_paths = (legacy.get(field, ""), embedding.get(field, ""))
                candidates = [Path(p).expanduser() for p in old_paths if isinstance(p, str) and p
                              and Path(p).name == filename]
                candidates += [self.state / "models" / filename, self.root / "models" / filename]
                profile[field] = str(next((p for p in candidates if p.is_file()), self.root / "models" / filename))
                profile[field + "_sha256"] = item["sha256"]
                profile[field + "_bytes"] = item["bytes"]
        result = {"version": 1, "selected_chat": "lfm3-vl", "selected_embedding": "qwen3-embedding", "profiles": profiles}
        for kind, old, fallback in (("chat", {k: v for k, v in legacy.items() if k != "embedding"}, "imported"),
                                    ("embedding", embedding, "imported-embedding")):
            if not old:
                continue
            old = deepcopy(old)
            # Runtime caches contain flattened connection settings. If the
            # global catalogue is absent (new installation/moved checkout), its
            # provider IDs cannot be imported without provider records.
            old.pop("provider_id", None)
            match = next((key for key, p in profiles.items() if p["kind"] == kind
                          and old.get("backend", "local") == "local"
                          and isinstance(old.get("model"), str)
                          and Path(old["model"]).name == Path(p["model"]).name), None)
            if match:
                migrated = {**profiles[match], **deepcopy(old), "kind": kind, "backend": "local"}
                # Missing bundled files move to the shared model directory; paths
                # that currently work remain exactly where the user put them.
                for field in ("binary", "model", "mmproj"):
                    if field in migrated and not Path(str(migrated[field])).expanduser().is_file():
                        migrated[field] = profiles[match].get(field, migrated[field])
                profiles[match] = migrated
            else:
                match = fallback
                profiles[match] = {**deepcopy(old), "kind": kind,
                                   "backend": old.get("backend", "local"),
                                   "name": old.get("name", "Imported " + kind)}
            self._normalize_key_file(profiles[match])
            result["selected_" + kind] = match
        return result

    def _read_locked(self):
        if self.path.exists():
            if self.path.is_symlink() or not self.path.is_file():
                raise ValueError("models.json должен быть обычным файлом.")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.path.chmod(0o600)
        else:
            data = self._initial()
            self._bind_providers(data)
            self._validate(data)
            _write_json(self.path, data)
        self._bind_providers(data)
        self._validate(data)
        return data

    @staticmethod
    def _bind_connection(profile, providers):
        """Named provider settings are authoritative over compatibility copies."""
        provider_id = profile.get("provider_id")
        if provider_id is None:
            return
        if (not isinstance(provider_id, str) or provider_id not in providers
                or profile.get("backend", "openai") != "openai"):
            raise ValueError("Провайдер модели отсутствует или не поддерживает API.")
        profile.setdefault("backend", "openai")
        provider = providers[provider_id]
        for key in _PROVIDER_CONNECTION:
            profile.pop(key, None)
            if key in provider:
                profile[key] = deepcopy(provider[key])

    @classmethod
    def _bind_providers(cls, data):
        """Resolve references on read so hand-edited JSON need not repeat URLs.

        Flat connection fields remain in saved mutations for older consumers,
        but a manual edit of a provider applies to every linked model immediately
        in the returned snapshot. Loading alone never rewrites the user's JSON.
        """
        if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
            return  # The normal schema validator provides the public error.
        providers = data.get("providers", {})
        if not isinstance(providers, dict):
            raise ValueError("providers должен содержать объект провайдеров.")
        for identifier, provider in providers.items():
            _validate_provider(identifier, provider)
        for profile in data["profiles"].values():
            if isinstance(profile, dict):
                cls._bind_connection(profile, providers)

    @staticmethod
    def _validate(data):
        if (not isinstance(data, dict) or type(data.get("version")) is not int
                or data["version"] != 1 or not isinstance(data.get("profiles"), dict)):
            raise ValueError("models.json должен содержать реестр моделей версии 1.")
        if "standard_provider_visible" in data and type(data["standard_provider_visible"]) is not bool:
            raise ValueError("standard_provider_visible должен содержать true или false.")
        for model_id, profile in data["profiles"].items():
            _validate_profile(model_id, profile)
        providers = data.get("providers", {})
        if not isinstance(providers, dict):
            raise ValueError("providers должен содержать объект провайдеров.")
        for identifier, provider in providers.items():
            _validate_provider(identifier, provider)
        for profile in data["profiles"].values():
            provider_id = profile.get("provider_id")
            if provider_id is not None:
                if (not isinstance(provider_id, str) or profile.get("backend") != "openai"
                        or provider_id not in providers):
                    raise ValueError("Провайдер модели отсутствует или не поддерживает API.")
                provider = providers[provider_id]
                if any(profile.get(key) != provider.get(key) for key in _PROVIDER_CONNECTION):
                    raise ValueError("Настройки модели не согласованы с её провайдером.")
        for kind in ("chat", "embedding"):
            selected = data.get("selected_" + kind)
            if selected is not None and (not isinstance(selected, str) or selected not in data["profiles"]
                                         or data["profiles"][selected]["kind"] != kind):
                raise ValueError("Выбранный профиль отсутствует или имеет другой kind.")

    def load(self):
        with self._locked():
            return deepcopy(self._read_locked())

    def peek(self):
        """Read an atomic registry snapshot without creating/chmodding files."""
        if self.path.exists():
            if self.path.is_symlink() or not self.path.is_file():
                raise ValueError("models.json должен быть обычным файлом.")
            data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            data = self._initial()
        self._bind_providers(data)
        self._validate(data)
        return deepcopy(data)

    @property
    def data(self):
        return self.load()

    def profiles(self, kind=None):
        if kind is not None and kind not in {"chat", "embedding"}:
            raise ValueError("kind должен быть chat или embedding.")
        return {key: profile for key, profile in self.load()["profiles"].items()
                if kind is None or profile["kind"] == kind}

    def visible_profiles(self, kind=None):
        if kind is not None and kind not in {"chat", "embedding"}:
            raise ValueError("kind должен быть chat или embedding.")
        data = self.load()
        return {key: deepcopy(profile) for key, profile in data["profiles"].items()
                if (kind is None or profile["kind"] == kind) and profile.get("visible", True)
                and (profile.get("backend", "local") != "local" or data.get("standard_provider_visible", True))
                and data.get("providers", {}).get(profile.get("provider_id"), {}).get("visible", True)}

    @staticmethod
    def settings_revision(data):
        canonical = deepcopy(data)
        ModelRegistry._bind_providers(canonical)
        ModelRegistry._validate(canonical)
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def revision(self):
        return self.settings_revision(self.load())

    def replace_settings(self, settings, *, expected_revision):
        """Compare-and-swap the complete global document under one lock."""
        candidate = deepcopy(settings)
        self._bind_providers(candidate)
        self._validate(candidate)
        if not isinstance(expected_revision, str) or not _SHA256.fullmatch(expected_revision):
            raise ValueError("Нужна версия настроек. Обнови окно и повтори изменение.")
        with self._locked():
            current = self._read_locked()
            if self.settings_revision(current) != expected_revision:
                raise ValueError("Настройки уже изменились в другом окне или терминале. Обнови список перед применением.")
            if candidate != current:
                _write_json(self.path, candidate)
        return deepcopy(candidate)

    def catalogue(self, settings=None):
        """Factory recommendations independent of migrated runtime options."""
        data = self.load() if settings is None else settings
        installed = data.get("profiles", {})
        binary = next((_local_binary(profile) for profile in installed.values()
                       if profile.get("backend", "local") == "local" and _local_binary(profile)),
                      shutil.which("llama-server") or "")
        result = deepcopy(_CATALOGUE)
        for identifier, profile in result.items():
            current = installed.get(identifier, {})
            profile["binary"] = _local_binary(current) or binary
            profile["model_revision"] = profile["download"]["revision"]
            profile["model_source"] = "https://huggingface.co/" + profile["download"]["repo"]
            for item in profile["download"]["files"]:
                field = item["field"]
                filename = item["filename"]
                path = current.get(field) if profile_category(identifier, current) == "standard" else None
                candidates = ([Path(path).expanduser()] if isinstance(path, str) and path else [])
                candidates += [self.state / "models" / filename, self.root / "models" / filename]
                profile[field] = str(next((candidate for candidate in candidates if candidate.is_file()),
                                          self.root / "models" / filename))
                profile[field + "_sha256"] = item["sha256"]
                profile[field + "_bytes"] = item["bytes"]
        return result

    def defaults(self, settings=None):
        """Reset runtime options while retaining identities and chosen files."""
        data = self.load() if settings is None else settings
        result = {}
        for identifier, profile in data["profiles"].items():
            reset = {key: deepcopy(value) for key, value in profile.items() if key not in _RUNTIME_OPTIONS}
            if profile_category(identifier, profile) == "standard":
                options = {key: deepcopy(value) for key, value in _CATALOGUE[identifier].items()
                           if key in _RUNTIME_OPTIONS}
            elif profile["kind"] == "embedding":
                options = {"context": 512 if profile.get("backend", "local") == "local" else 8192,
                           "dimensions": profile.get("dimensions", 384), "query_prefix": "", "passage_prefix": "",
                           "max_input_chars": 1200}
                if profile.get("backend", "local") == "local":
                    options.update(pooling="mean", batch_tokens=512, micro_batch_tokens=512)
            else:
                options = {"context": 65536, "enable_thinking": False}
                if profile.get("backend", "local") == "local":
                    options.update(sampling={"temperature": 0.1, "top_k": 50, "repeat_penalty": 1.1},
                                   batch_tokens=512, micro_batch_tokens=256,
                                   idle_sleep_seconds=60, idle_terminate_seconds=120,
                                   cache_type_k="q8_0", cache_type_v="q8_0", flash_attention="on")
                else:
                    options["max_output_tokens"] = 1536
            reset.update(options)
            result[identifier] = reset
        return result

    def get(self, model_id):
        try:
            return self.load()["profiles"][model_id]
        except KeyError as exc:
            raise ValueError("Неизвестный профиль модели: " + str(model_id)) from exc

    def _normalize_key_file(self, profile):
        value = profile.get("api_key_file")
        if isinstance(value, str) and value:
            path = Path(value).expanduser()
            # A global profile must keep using the key from its original runtime.
            # Keep the final path component intact so readiness still rejects symlinks.
            profile["api_key_file"] = str(path if path.is_absolute() else self.state / path)

    def _prepare_profile(self, model_id, profile, *, providers=None, hash_local=True):
        profile = deepcopy(profile)
        if isinstance(profile, dict):
            if profile.get("provider_id") is not None:
                self._bind_connection(profile, self.providers() if providers is None else providers)
            profile.setdefault("backend", "local")
            profile.setdefault("name", model_id)
            profile.setdefault("enabled", True)
            if profile["backend"] == "openai" and "model_id" not in profile and "model" in profile:
                profile["model_id"] = profile.pop("model")
            if (hash_local and profile.get("kind") == "embedding" and profile["backend"] == "local"
                    and not profile.get("model_sha256") and isinstance(profile.get("model"), str)
                    and Path(profile["model"]).expanduser().is_file()):
                profile["model_sha256"] = file_sha256(Path(profile["model"]).expanduser())
            self._normalize_key_file(profile)
        _validate_profile(model_id, profile)
        return profile

    def add(self, model_id, profile):
        profile = self._prepare_profile(model_id, profile)
        with self._locked():
            data = self._read_locked()
            if model_id in data["profiles"]:
                raise ValueError("Профиль с таким ID уже существует: " + model_id)
            data["profiles"][model_id] = profile
            self._bind_providers(data)
            self._validate(data)
            _write_json(self.path, data)
        return deepcopy(data["profiles"][model_id])

    def update(self, model_id, profile):
        """Replace an existing profile without changing either selected ID."""
        profile = self._prepare_profile(model_id, profile)
        with self._locked():
            data = self._read_locked()
            if model_id not in data["profiles"]:
                raise ValueError("Неизвестный профиль модели: " + str(model_id))
            data["profiles"][model_id] = profile
            self._bind_providers(data)
            self._validate(data)
            _write_json(self.path, data)
        return deepcopy(data["profiles"][model_id])

    def select(self, model_id, kind="chat"):
        if kind not in {"chat", "embedding"}:
            raise ValueError("kind должен быть chat или embedding.")
        with self._locked():
            data = self._read_locked()
            if model_id is not None and (model_id not in data["profiles"] or data["profiles"][model_id]["kind"] != kind):
                raise ValueError("Выбери существующий профиль kind " + kind + ".")
            changed = False
            if model_id is not None and data["profiles"][model_id].get("enabled") is not True:
                data["profiles"][model_id]["enabled"] = True
                changed = True
            if data.get("selected_" + kind) != model_id:
                data["selected_" + kind] = model_id
                changed = True
            if changed:
                _write_json(self.path, data)
        return model_id

    def remove(self, model_id):
        """Remove only metadata; a selected model must first be replaced."""
        with self._locked():
            data = self._read_locked()
            if model_id not in data["profiles"]:
                raise ValueError("Неизвестный профиль модели.")
            if model_id in {data.get("selected_chat"), data.get("selected_embedding")}:
                raise ValueError("Сначала выбери другую модель или отключи этот энкодер.")
            del data["profiles"][model_id]
            _write_json(self.path, data)

    def providers(self):
        return self.load().get("providers", {})

    def save_provider(self, identifier, provider, *, replace=False):
        provider = deepcopy(provider)
        self._normalize_key_file(provider)
        _validate_provider(identifier, provider)
        with self._locked():
            data = self._read_locked()
            providers = data.setdefault("providers", {})
            if (identifier in providers) != replace:
                raise ValueError("Провайдер уже существует." if identifier in providers else "Провайдер не найден.")
            providers[identifier] = provider
            for profile in data["profiles"].values():
                if profile.get("provider_id") == identifier:
                    for key in _PROVIDER_CONNECTION:
                        profile.pop(key, None)
                        if key in provider:
                            profile[key] = provider[key]
            self._validate(data)
            _write_json(self.path, data)
        return deepcopy(provider)

    def remove_provider(self, identifier):
        with self._locked():
            data = self._read_locked()
            providers = data.get("providers", {})
            if identifier not in providers:
                raise ValueError("Провайдер не найден.")
            removed = [key for key, profile in data["profiles"].items() if profile.get("provider_id") == identifier]
            chat_removed = data.get("selected_chat") in removed
            embedding_removed = data.get("selected_embedding") in removed
            for key in removed:
                del data["profiles"][key]
            if chat_removed:
                # Removing one API connection must never start using a different
                # paid provider. Prefer installed recommendations, then another
                # explicitly enabled local model; otherwise disable chat safely.
                candidates = ["lfm3-vl", "spark", *data["profiles"]]
                fallback = next((key for key in candidates if key in data["profiles"]
                                 and data["profiles"][key]["kind"] == "chat"
                                 and data["profiles"][key].get("backend", "local") == "local"
                                 and data["profiles"][key].get("enabled", True)
                                 and _local_ready(data["profiles"][key])), None)
                data["selected_chat"] = fallback
            if embedding_removed:
                data["selected_embedding"] = None
            del providers[identifier]
            self._validate(data)
            _write_json(self.path, data)
        return {"removed_models": removed, "selection_changed": chat_removed or embedding_removed,
                "selected_chat": data.get("selected_chat"), "selected_embedding": data.get("selected_embedding")}

    def restore_catalogue(self):
        """Restore missing recommendations without replacing user edits."""
        initial = self.catalogue()
        with self._locked():
            data = self._read_locked()
            for identifier in _CATALOGUE:
                data["profiles"].setdefault(identifier, initial[identifier])
            self._validate(data)
            _write_json(self.path, data)

    @staticmethod
    def _resolved(data):
        def settings(kind):
            selected = data.get("selected_" + kind)
            if selected is None:
                return {"enabled": False}
            result = {key: deepcopy(value) for key, value in data["profiles"][selected].items()
                      if key not in {"kind", "download", "visible"}}
            result.setdefault("enabled", True)
            if result.get("backend", "local") == "local":
                result["binary"] = _local_binary(result)
            if kind == "embedding":
                result["embedding"] = True
            return result
        config = settings("chat")
        config["embedding"] = settings("embedding")
        return config

    def resolved(self):
        return self._resolved(self.load())

    def materialize(self, *, configuration=None):
        with self._locked():
            resolved = self._resolved(self._read_locked()) if configuration is None else deepcopy(configuration)
            _private_directory(self.state)
            path = self.state / "neural.json"
            if path.is_symlink():
                raise ValueError("neural.json должен быть обычным файлом.")
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                current = None
            if current == resolved:
                path.chmod(0o600)
                return False
            _write_json(path, resolved)
            return True

    def profile_ready(self, profile, *, credential=None):
        """Readiness for a saved or in-memory draft profile; never starts it."""
        if profile.get("backend", "local") == "local":
            return _local_ready(profile)
        from .remote_model import validate_base_url
        try:
            validate_base_url(profile.get("base_url"))
        except ValueError:
            return False
        if not str(profile.get("model_id", profile.get("model", ""))).strip():
            return False
        if credential is not None:
            return True
        if "api_key_env" in profile and not os.environ.get(profile["api_key_env"], "").strip():
            return False
        if "api_key_file" in profile:
            try:
                path = Path(profile["api_key_file"]).expanduser()
                if not path.is_absolute():
                    path = self.state / path
                info = path.stat(follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077 or path.resolve() == self.state / "token"):
                    return False
                with path.open("r", encoding="utf-8") as stream:
                    value = stream.read(8193)
                if not value.strip() or len(value) > 8192:
                    return False
            except (OSError, UnicodeError):
                return False
        return True

    def ready(self, model_id):
        return self.profile_ready(self.get(model_id))

    def download(self, model_id, progress=None):
        """Explicitly fetch pinned GGUFs, publishing only fully verified files.

        ``progress(filename, bytes_read, total_bytes_or_none)`` is optional.
        Selection is never changed. Existing valid weights are reused and every
        temporary .part file is removed on failure.
        """
        profile = self.get(model_id)
        spec = deepcopy(_download_spec(profile))
        _private_directory(self.root / "models")
        with process_lock(self.root / ("download-" + model_id + ".lock"), wait=True) as fd:
            os.fchmod(fd, 0o600)
            return self._download_locked(model_id, profile, spec, progress)

    def _download_locked(self, model_id, profile, spec, progress):
        deadline = time.monotonic() + _DOWNLOAD_SECONDS
        missing_hashes = any(not item.get("sha256") for item in spec["files"])
        if missing_hashes:
            _resolve_hashes(spec, deadline)
        prepared, paths = [], {}
        try:
            for item in spec["files"]:
                existing = profile.get(item["field"])
                target = self.root / "models" / item["filename"]
                candidates = [Path(existing).expanduser()] if isinstance(existing, str) and existing else []
                candidates.append(target)
                valid = next((p for p in candidates if p.is_file()
                              and ("bytes" not in item or p.stat().st_size == item["bytes"])
                              and file_sha256(p) == item["sha256"].lower()), None)
                if valid is not None:
                    paths[item["field"]] = str(valid)
                    if progress:
                        progress(item["filename"], valid.stat().st_size, item.get("bytes"))
                    continue
                _private_directory(target.parent)
                if not target.parent.resolve().is_relative_to((self.root / "models").resolve()):
                    raise ValueError("Каталог весов не должен выходить за общий каталог моделей Handy.")
                temporary = _fetch_file(spec, item, target, deadline, progress)
                prepared.append((temporary, target))
                paths[item["field"]] = str(target)
            with self._locked():
                data = self._read_locked()
                current = data["profiles"].get(model_id)
                if current != profile:
                    raise ValueError("Профиль изменился во время скачивания; повтори выбор модели.")
                # All members verify before replacing any previous weights.
                for temporary, target in prepared:
                    os.replace(temporary, target)
                current.update(paths)
                current["download"] = spec
                for item in spec["files"]:
                    current[item["field"] + "_sha256"] = item["sha256"].lower()
                    if "bytes" in item:
                        current[item["field"] + "_bytes"] = item["bytes"]
                _write_json(self.path, data)
                return deepcopy(current)
        finally:
            for temporary, _ in prepared:
                temporary.unlink(missing_ok=True)


def _ssl_context():
    # Some macOS Python distributions have no system CA bundle. certifi is an
    # optional installed trust store, never a reason to disable verification.
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _open_url(url, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Истекло время скачивания модели.")
    request = Request(url, headers={"User-Agent": "Handy/0.1", "Accept-Encoding": "identity"})
    return urlopen(request, timeout=min(30, remaining), context=_ssl_context())


def _resolve_hashes(spec, deadline):
    url = "https://huggingface.co/api/models/" + spec["repo"] + "/revision/" + spec["revision"] + "?blobs=true"
    metadata_deadline = min(deadline, time.monotonic() + 60)
    chunks, size = [], 0
    with _open_url(url, metadata_deadline) as response:
        while True:
            chunk = _read_response(response, min(65536, _METADATA_LIMIT + 1 - size), metadata_deadline)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _METADATA_LIMIT:
                raise ValueError("Метаданные Hugging Face превышают допустимый размер.")
    metadata = json.loads(b"".join(chunks))
    if (not isinstance(metadata, dict) or not isinstance(metadata.get("sha"), str)
            or metadata["sha"].lower() != spec["revision"].lower()):
        raise ValueError("Hugging Face вернул другой commit модели.")
    raw_siblings = metadata.get("siblings")
    if not isinstance(raw_siblings, list):
        raise ValueError("Hugging Face не предоставил список файлов модели.")
    siblings = {item["rfilename"]: item for item in raw_siblings
                if isinstance(item, dict) and isinstance(item.get("rfilename"), str)}
    for item in spec["files"]:
        if item.get("sha256"):
            continue
        upstream = siblings.get(item["filename"], {})
        lfs = upstream.get("lfs")
        if not isinstance(lfs, dict):
            raise ValueError("Hugging Face не предоставил SHA256 весов модели.")
        digest = lfs.get("sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("Hugging Face не предоставил SHA256 весов модели.")
        item["sha256"] = digest.lower()
        size = lfs.get("size", upstream.get("size"))
        if type(size) is int and size > 0:
            if "bytes" in item and item["bytes"] != size:
                raise ValueError("Размер модели не совпадает с закреплённым описанием.")
            item["bytes"] = size


def _read_response(response, size, deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("Истекло время скачивания модели.")
    # HTTPResponse.read1 returns after one socket read, allowing a deadline check
    # between chunks even when a peer sends a slow continuous response.
    return getattr(response, "read1", response.read)(size)


def _fetch_file(spec, item, target, deadline, progress):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=target.parent, prefix="." + target.name + "-",
                                         suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            digest, size = hashlib.sha256(), 0
            url = "https://huggingface.co/" + spec["repo"] + "/resolve/" + spec["revision"] + "/" + quote(item["filename"], safe="/")
            with _open_url(url, deadline) as response:
                while True:
                    chunk = _read_response(response, _CHUNK, deadline)
                    if not chunk:
                        break
                    size += len(chunk)
                    if "bytes" in item and size > item["bytes"]:
                        raise ValueError("Размер скачанной модели превышает ожидаемый.")
                    stream.write(chunk)
                    digest.update(chunk)
                    if progress:
                        progress(item["filename"], size, item.get("bytes"))
            if "bytes" in item and size != item["bytes"]:
                raise ValueError("Модель скачалась не полностью: размер не совпадает.")
            if digest.hexdigest() != item["sha256"].lower():
                raise ValueError("SHA256 скачанной модели не совпадает.")
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
