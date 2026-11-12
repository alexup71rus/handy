"""Checkbox semantics for provider trees, independent of terminal or native UI.

The parent mask is retained for older configurations. Checking a child under
an old disabled parent first clears its siblings, then enables that one child.
All functions mutate and return the supplied settings document; no IO occurs.
"""

from copy import deepcopy


STANDARD_PROVIDER = "builtin-standard"


def _collections(settings):
    if not isinstance(settings, dict):
        raise ValueError("Нужен объект настроек.")
    profiles = settings.get("profiles", {})
    providers = settings.get("providers", {})
    if (not isinstance(profiles, dict) or not isinstance(providers, dict)
            or any(not isinstance(value, dict) for value in profiles.values())
            or any(not isinstance(value, dict) for value in providers.values())):
        raise ValueError("Настройки моделей и провайдеров должны быть объектами.")
    return profiles, providers


def _visible(value):
    if type(value) is not bool:
        raise ValueError("Видимость должна быть true или false.")
    return value


def _owner(identifier, profile):
    if profile.get("backend", "local") == "local":
        return STANDARD_PROVIDER
    return profile.get("provider_id") or "legacy:" + identifier


def _provider(settings, identifier):
    profiles, providers = _collections(settings)
    if identifier == STANDARD_PROVIDER:
        return profiles, providers, settings.get("standard_provider_visible", True)
    if identifier in providers:
        return profiles, providers, providers[identifier].get("visible", True)
    if isinstance(identifier, str) and identifier.startswith("legacy:"):
        key = identifier[len("legacy:"):]
        profile = profiles.get(key)
        if profile and profile.get("backend") == "openai" and not profile.get("provider_id"):
            return profiles, providers, True
    raise ValueError("Провайдер не найден.")


def _catalogue(catalogue):
    if catalogue is None:
        return {}
    if isinstance(catalogue, dict):
        entries = catalogue.items()
    elif isinstance(catalogue, (list, tuple)):
        if any(not isinstance(value, dict) for value in catalogue):
            raise ValueError("Каталог должен содержать профили моделей.")
        entries = ((value.get("id"), {key: item for key, item in value.items() if key != "id"}) for value in catalogue)
    else:
        raise ValueError("Каталог должен содержать профили моделей.")
    result = {}
    for identifier, value in entries:
        if not isinstance(identifier, str) or not identifier or not isinstance(value, dict):
            raise ValueError("В каталоге отсутствует ID или профиль модели.")
        if value.get("backend", "local") == "local":
            result[identifier] = deepcopy(value)
    return result


def set_provider_visibility(settings, identifier, visible, catalogue=None):
    """Check/uncheck the parent and all its children, including other kinds."""
    visible = _visible(visible)
    profiles, providers, _ = _provider(settings, identifier)
    additions = _catalogue(catalogue) if identifier == STANDARD_PROVIDER else {}
    # Validate first; an unknown target or malformed catalogue changes nothing.
    if additions:
        if "profiles" not in settings:
            settings["profiles"] = profiles
        for key, value in additions.items():
            profiles.setdefault(key, value)
    if identifier == STANDARD_PROVIDER:
        settings["standard_provider_visible"] = visible
    elif identifier in providers:
        providers[identifier]["visible"] = visible
    for key, profile in profiles.items():
        if _owner(key, profile) == identifier:
            profile["visible"] = visible
    return settings


def set_model_visibility(settings, identifier, visible):
    """Change one child; enabling it never revives hidden siblings."""
    visible = _visible(visible)
    profiles, _ = _collections(settings)
    if identifier not in profiles:
        raise ValueError("Модель не найдена.")
    profile = profiles[identifier]
    owner = _owner(identifier, profile)
    _, providers, mask = _provider(settings, owner)
    if visible and not mask:
        for key, sibling in profiles.items():
            if _owner(key, sibling) == owner:
                sibling["visible"] = False
        if owner == STANDARD_PROVIDER:
            settings["standard_provider_visible"] = True
        elif owner in providers:
            providers[owner]["visible"] = True
    profile["visible"] = visible
    return settings


def provider_visibility_state(settings, identifier, kind=None):
    """Return off/mixed/on from effective child visibility, not just the mask."""
    if kind not in {None, "chat", "embedding"}:
        raise ValueError("Назначение модели: chat или embedding.")
    profiles, _, mask = _provider(settings, identifier)
    children = [profile for key, profile in profiles.items()
                if _owner(key, profile) == identifier and (kind is None or profile.get("kind") == kind)]
    if not mask or not children:
        return "off"
    shown = sum(profile.get("visible", True) for profile in children)
    if not shown:
        return "off"
    return "on" if shown == len(children) else "mixed"
