"""Small CLI editors using the same validated model operations as the app.

The form is collected before its one save operation. A cancelled field therefore
cannot leave a provider, model, or entered API key behind.
"""

from copy import deepcopy
import math
from pathlib import Path
import shutil
from urllib.parse import urlsplit
import uuid

from .terminal_picker import PickerItem, pick


class _Cancelled(Exception):
    pass


def _display(value):
    return " ".join("".join(char if char.isprintable() else " " for char in str(value)).split())[:160]


def _input(prompt, label, default=None, *, secret=False, preserve=False):
    shown = None if secret else default
    if shown is not None and shown != "":
        # A manually edited display name/prefix must not inject terminal control
        # sequences into a prompt. Its original value is still kept on Enter.
        displayed = _display(shown)
        label += " [" + displayed + "]"
    options = {"cancel_on_escape": True}
    if secret:
        options["is_password"] = True
    value = prompt(label + " › ", **options)
    if value.strip().casefold() == "/cancel":
        raise _Cancelled
    if value == "" or not preserve and not value.strip():
        return default if default is not None else ""
    return value if preserve else value.strip()


def _number(prompt, label, default, minimum, maximum, *, integer=True):
    value = _input(prompt, label, str(default))
    try:
        result = int(value) if integer else float(value.replace(",", "."))
    except (ValueError, TypeError, OverflowError):
        raise ValueError(label + ": нужно число.") from None
    if (not integer and not math.isfinite(result)) or not minimum <= result <= maximum:
        raise ValueError(f"{label}: допустимый диапазон {minimum}–{maximum}.")
    return result


def _yes(prompt, label, default=False):
    answer = _input(prompt, label, "да" if default else "нет").casefold()
    if answer in {"да", "yes", "y", "1"}:
        return True
    if answer in {"нет", "no", "n", "0"}:
        return False
    raise ValueError("Ответь «да» или «нет».")


def _save(prompt):
    return _yes(prompt, "Сохранить?", True)


def _path(value):
    return str(Path(value).expanduser().resolve()) if value else ""


def _key_path(value):
    # Keep the final component for the backend's O_NOFOLLOW check. A key-file
    # symlink must not become an apparently ordinary file during form editing.
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else Path.cwd() / path)


def _auth_fields(profile, prompt, *, existing=False, allow_key=True):
    """Return connection fields and an optional in-memory newly entered key."""
    result = deepcopy(profile)
    choices = "ключ / env / файл / нет"
    default = "оставить" if existing else "ключ" if allow_key else "нет"
    if existing:
        choices = "оставить / " + choices
    if not allow_key:
        choices = choices.replace("ключ / ", "")
    answer = _input(prompt, "Авторизация: " + choices, default).casefold()
    aliases = {"key": "ключ", "file": "файл", "none": "нет", "keep": "оставить", "переменная": "env"}
    answer = aliases.get(answer, answer)
    key = None
    if answer == "оставить" and existing:
        return result, None
    if answer == "ключ" and allow_key:
        label = "Новый API-ключ; Enter — оставить" if existing and profile.get("api_key_file") else "API-ключ; Enter — без ключа"
        key = _input(prompt, label, secret=True)
        if not key:
            # Keep an existing private file. An empty new key is deliberately
            # anonymous, useful for Ollama and other local OpenAI endpoints.
            if existing and profile.get("api_key_file"):
                result["auth"] = "key"
                return result, None
            answer = "нет"
            key = None
        else:
            result.pop("api_key_env", None)
            result.pop("api_key_file", None)
            result["auth"] = "key"
            return result, key
    if answer == "env":
        name = _input(prompt, "Переменная с ключом", profile.get("api_key_env", ""))
        if not name:
            raise ValueError("Укажи имя переменной с ключом.")
        result.pop("api_key_file", None)
        result.update(auth="env", api_key_env=name)
    elif answer == "файл":
        filename = _input(prompt, "Приватный файл ключа", profile.get("api_key_file", ""))
        if not filename:
            raise ValueError("Укажи файл с ключом.")
        result.pop("api_key_env", None)
        result.update(auth="file", api_key_file=_key_path(filename))
    elif answer == "нет":
        result.pop("api_key_env", None)
        result.pop("api_key_file", None)
        result["auth"] = "none"
    else:
        raise ValueError("Выбери способ авторизации из списка.")
    return result, key


def provider_form(manager, console, prompt, *, identifier=None):
    """Create/edit a connection. Entered credentials are never displayed."""
    try:
        providers = manager.registry.peek().get("providers", {})
        original = providers.get(identifier) if identifier is not None else None
        if identifier is not None and original is None:
            raise ValueError("Провайдер не найден.")
        console.print("Изменить провайдера" if original else "Добавить провайдера", style="bold", markup=False)
        console.print("Esc / Ctrl+C — отмена.", style="dim", markup=False)
        profile = deepcopy(original or {})
        base = _input(prompt, "Адрес API", profile.get("base_url", ""))
        if not base:
            raise ValueError("Укажи адрес API, например http://127.0.0.1:11434/v1.")
        parsed = urlsplit(base)
        host = parsed.hostname or "API"
        if parsed.port and parsed.port != {"http": 80, "https": 443}.get(parsed.scheme):
            host += ":" + str(parsed.port)
        suggested = profile.get("name") or host[:200]
        profile["base_url"] = base
        profile["name"] = _input(prompt, "Название", suggested)
        profile, key = _auth_fields(profile, prompt, existing=original is not None)
        if "auth" not in profile:
            profile["auth"] = "env" if profile.get("api_key_env") else "file" if profile.get("api_key_file") else "none"
        if not _save(prompt):
            return None
        model_id = identifier or "provider-" + uuid.uuid4().hex
        request = {"operation": "save_provider", "id": model_id, "provider": profile}
        if identifier is not None:
            request["original_id"] = identifier
        if key is not None:
            request["api_key"] = key
        manager.perform(request)
        console.print("Провайдер сохранён.", markup=False)
        return model_id
    except (KeyboardInterrupt, EOFError, _Cancelled):
        return None


def _choose_provider(providers):
    if not providers:
        raise ValueError("Сначала добавь провайдера.")
    if len(providers) == 1:
        return next(iter(providers))
    items = [PickerItem(identifier, _display(value["name"]), _display(value.get("base_url", "")))
             for identifier, value in sorted(providers.items(), key=lambda item: item[1]["name"].casefold())]
    choice = pick("Провайдер", items)
    if choice.action != "select" or choice.id is None:
        raise _Cancelled
    if choice.id not in providers:
        raise ValueError("Выбери провайдера из списка.")
    return choice.id


def _optional_number(profile, key, prompt, label, minimum, maximum, *, integer=False, allow_minus_one=False):
    value = _input(prompt, label + "; /default — сбросить", str(profile[key]) if key in profile else "")
    if value.casefold() == "/default":
        profile.pop(key, None)
        return
    if value == "":
        return
    try:
        result = int(value) if integer else float(value.replace(",", "."))
    except (ValueError, TypeError, OverflowError):
        raise ValueError(label + ": нужно число.") from None
    if (not integer and not math.isfinite(result)) or not (minimum <= result <= maximum or allow_minus_one and result == -1):
        raise ValueError(label + ": значение вне допустимого диапазона.")
    profile[key] = result


def _prefix(prompt, label, previous=""):
    value = _input(prompt, label + "; /empty — без префикса", previous, preserve=True)
    return "" if value == "/empty" else value


def _embedding(profile, prompt):
    profile["embedding"] = True
    profile["dimensions"] = _number(prompt, "Размерность вектора", profile.get("dimensions", 1024), 1, 65536)
    profile["query_prefix"] = _prefix(prompt, "Префикс запроса", profile.get("query_prefix", ""))
    profile["passage_prefix"] = _prefix(prompt, "Префикс документа", profile.get("passage_prefix", ""))
    if profile["backend"] == "local":
        pooling = _input(prompt, "Pooling: mean / last / cls", profile.get("pooling", "mean")).casefold()
        if pooling not in {"mean", "last", "cls"}:
            raise ValueError("Pooling: mean, last или cls.")
        profile["pooling"] = pooling
        profile.setdefault("batch_tokens", 512)
        profile.setdefault("micro_batch_tokens", 512)
    else:
        profile["send_dimensions"] = _yes(prompt, "Передавать размерность в API?", profile.get("send_dimensions", False))
    profile.setdefault("max_input_chars", 1200)


def _advanced(profile, prompt):
    if profile["kind"] == "chat":
        sampling = deepcopy(profile.get("sampling", {}))
        _optional_number(sampling, "temperature", prompt, "Temperature", 0, 2)
        _optional_number(sampling, "top_p", prompt, "Top P", .000001, 1)
        if profile["backend"] == "local":
            _optional_number(sampling, "top_k", prompt, "Top K", 1, 1000, integer=True, allow_minus_one=True)
            _optional_number(sampling, "repeat_penalty", prompt, "Repeat penalty", .01, 10)
            profile["enable_thinking"] = _yes(prompt, "Рассуждения модели?", profile.get("enable_thinking", False))
        if sampling:
            profile["sampling"] = sampling
        else:
            profile.pop("sampling", None)
        _optional_number(profile, "max_output_tokens", prompt, "Максимум токенов ответа", 1, profile["context"], integer=True)
    if profile["backend"] == "local":
        _optional_number(profile, "idle_sleep_seconds", prompt, "Сон после простоя, секунд", 1, 86400, integer=True, allow_minus_one=True)
        _optional_number(profile, "idle_terminate_seconds", prompt, "Выгрузка после простоя, секунд", 0, 86400, integer=True)
    profile["visible"] = _yes(prompt, "Показывать в /models?", profile.get("visible", True))


def model_form(manager, console, prompt, *, identifier=None, provider_id=None, kind="chat", backend=None):
    """Create/edit a model without exposing its internal registry identifier."""
    try:
        data = manager.registry.peek()
        original = data["profiles"].get(identifier) if identifier is not None else None
        if identifier is not None and original is None:
            raise ValueError("Модель не найдена.")
        console.print("Изменить модель" if original else "Добавить модель", style="bold", markup=False)
        console.print("Esc / Ctrl+C — отмена.", style="dim", markup=False)
        profile = deepcopy(original or {})
        kind = profile.get("kind", kind)
        backend = profile.get("backend", backend or ("openai" if provider_id else None))
        if backend is None:
            source = _input(prompt, "Источник: API / GGUF", "API").casefold()
            if source not in {"api", "gguf"}:
                raise ValueError("Выбери API или GGUF.")
            backend = "local" if source == "gguf" else "openai"
        profile.update(kind=kind, backend=backend)
        if original is None:
            profile["enabled"] = True
        local = backend == "local"
        if local:
            filename = _input(prompt, "Файл GGUF", profile.get("model", ""))
            if not filename:
                raise ValueError("Выбери файл модели GGUF.")
            profile["model"] = _path(filename)
            suggested = Path(profile["model"]).stem[:200]
            binary = profile.get("binary") or next((value.get("binary") for value in data["profiles"].values()
                                                    if value.get("binary") and Path(value["binary"]).is_file()), "") or shutil.which("llama-server") or ""
            if not binary and not (original and profile.get("download")):
                binary = _input(prompt, "Путь к llama-server")
            profile["binary"] = _path(binary)
            if kind == "chat":
                projector = _input(prompt, "Vision-проектор; /empty — без него", profile.get("mmproj", ""))
                if projector and projector != "/empty":
                    profile["mmproj"] = _path(projector)
                else:
                    profile.pop("mmproj", None)
            profile.setdefault("port", 18747 if kind == "chat" else 18749)
        else:
            provider_id = provider_id or profile.get("provider_id")
            if not provider_id and not original:
                provider_id = _choose_provider(data.get("providers", {}))
            if provider_id:
                if provider_id not in data.get("providers", {}):
                    raise ValueError("Провайдер не найден.")
                profile["provider_id"] = provider_id
            else:
                profile["base_url"] = _input(prompt, "Адрес API", profile.get("base_url", ""))
            model_id = _input(prompt, "ID модели", profile.get("model_id", profile.get("model", "")))
            if not model_id:
                raise ValueError("Укажи ID модели у провайдера.")
            profile["model_id"] = model_id
            suggested = model_id[:200]
        profile["name"] = _input(prompt, "Название", profile.get("name", suggested))
        profile["context"] = _number(prompt, "Контекст, токенов", profile.get("context", 65536 if kind == "chat" else 512 if local else 8192),
                                      512 if kind == "chat" else 32,
                                      (65536 if local else 2**31 - 1) if kind == "chat" else 4096 if local else 32768)
        if kind == "embedding" and (original is None or _yes(prompt, "Изменить настройки эмбеддингов?")):
            _embedding(profile, prompt)
        if _yes(prompt, "Дополнительные настройки?"):
            _advanced(profile, prompt)
            if not local and not profile.get("provider_id"):
                changed, _ = _auth_fields(profile, prompt, existing=True, allow_key=False)
                profile = {key: value for key, value in changed.items() if key != "auth"}
        if not _save(prompt):
            return None
        model_id = identifier or ("local-" if local else "api-") + uuid.uuid4().hex
        request = {"operation": "save_profile", "id": model_id, "profile": profile}
        if identifier is not None:
            request["original_id"] = identifier
        manager.perform(request)
        console.print("Модель сохранена.", markup=False)
        return model_id
    except (KeyboardInterrupt, EOFError, _Cancelled):
        return None
