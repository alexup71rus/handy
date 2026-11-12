"""Provider-first model inventory and explicit selection for Handy's pi UI."""

from copy import deepcopy
import ipaddress
from pathlib import Path
import time
from urllib.parse import urlsplit

from .model_activation import activate_models
from .model_registry import ModelRegistry


SYSTEM_PROVIDER = 'handy-system'


def api_endpoint(state, profile):
    """Check plain HTTP before pi receives a provider URL or its credentials."""
    from .remote_model import OpenAIModel, _home_address, validate_base_url

    base = validate_base_url(profile.get('base_url'))
    parsed = urlsplit(base)
    if parsed.scheme == 'https':
        return base
    try:
        ipaddress.ip_address(parsed.hostname)
        return base
    except ValueError:
        pass
    resolver = OpenAIModel(state, config=profile)
    try:
        addresses = resolver._addresses(parsed.hostname, parsed.port or 80, time.monotonic() + 5, None)
    finally:
        resolver.close()
    if not addresses or any(not _home_address(item[4][0]) for item in addresses):
        raise ValueError('Обычный HTTP разрешён только для localhost и домашней сети; используй HTTPS.')
    return base


def model_identity(identifier, profile, settings):
    if profile.get('backend', 'local') == 'local':
        return SYSTEM_PROVIDER, identifier
    owner = profile.get('provider_id')
    name = owner or identifier
    standalone = settings.get('profiles', {}).get(name, {})
    collision = (name == 'system' or name in settings.get('providers', {})
                 and standalone.get('backend') == 'openai' and not standalone.get('provider_id'))
    # ':' cannot occur in registry IDs, so escaped connections stay distinct.
    provider = ('handy-provider:' if owner else 'handy-profile:') + name if collision else 'handy-' + name
    return provider, profile['model_id']


def _downloadable(profile):
    spec = profile.get('download')
    return profile.get('backend', 'local') == 'local' and isinstance(spec, dict) and bool(spec.get('files'))


def inventory(state):
    """Return display metadata only; no downloads, credentials or model requests."""
    registry = ModelRegistry(state)
    data = registry.load()
    profiles = {**registry.catalogue(data), **data['profiles']}
    groups = {}
    for identifier, profile in profiles.items():
        if profile.get('kind') != 'chat' or not profile.get('visible', True):
            continue
        system = profile.get('backend', 'local') == 'local'
        owner = profile.get('provider_id')
        provider = data.get('providers', {}).get(owner, {})
        if (system and not data.get('standard_provider_visible', True)) or (not system and not provider.get('visible', True)):
            continue
        pi_provider, pi_model = model_identity(identifier, profile, data)
        group_id = pi_provider
        name = 'Системные модели Handy' if system else provider.get('name') or profile.get('name') or 'API'
        group = groups.setdefault(group_id, dict(id=group_id, name=name, system=system, models=[]))
        files = profile.get('download', {}).get('files', []) if _downloadable(profile) else []
        group['models'].append({
            'id': identifier, 'name': profile.get('name') or identifier,
            'ready': registry.profile_ready(profile), 'downloadable': _downloadable(profile),
            'download_bytes': sum(item.get('bytes', 0) for item in files),
            'pi_provider': pi_provider, 'pi_model': pi_model,
        })
    for group in groups.values():
        group['models'].sort(key=lambda model: (model['name'].casefold(), model['id']))
    return {'groups': sorted(groups.values(), key=lambda group: (not group['system'], group['name'].casefold(), group['id'])),
            'selected': data.get('selected_chat')}


def select_model(state, identifier, download=False):
    """Select, activate and export; preserve concurrent changes if activation fails."""
    if not isinstance(identifier, str) or not identifier or type(download) is not bool:
        raise ValueError('Нужны ID модели и download: true или false.')
    state = Path(state).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    registry = ModelRegistry(state)
    data = registry.load()
    profile = data['profiles'].get(identifier) or registry.catalogue(data).get(identifier)
    if not profile or profile.get('kind') != 'chat':
        raise ValueError('Выбери существующую модель чата.')
    if not registry.profile_ready(profile):
        if not _downloadable(profile):
            raise ValueError('Модель не готова: проверь файлы, llama.cpp или ключ провайдера в настройках Handy.')
        if not download:
            raise ValueError('Сначала подтверди скачивание этой модели.')
        if identifier not in data['profiles']:
            registry.add(identifier, profile)
        registry.download(identifier)
        profile = registry.get(identifier)
        if not registry.profile_ready(profile):
            raise ValueError('Веса скачаны, но модель не готова. Проверь llama-server в настройках Handy.')
    elif identifier not in data['profiles']:
        # A factory profile can already have verified weights on disk after
        # metadata was removed. Selecting it does not require a network call.
        registry.add(identifier, profile)
    previous = registry.load()
    profile = previous['profiles'].get(identifier)
    if not profile or profile.get('kind') != 'chat' or not registry.profile_ready(profile):
        raise ValueError('Модель изменилась или больше не готова. Обнови список и повтори выбор.')
    candidate = deepcopy(previous)
    candidate['selected_chat'] = identifier
    candidate['profiles'][identifier]['enabled'] = True
    committed = registry.replace_settings(candidate, expected_revision=registry.settings_revision(previous))
    revision = registry.settings_revision(committed)
    from .pi_runtime import configure
    activated = False
    try:
        activate_models(state, registry._resolved(committed), force=False)
        activated = True
        configure(state)
    except BaseException:
        try:
            registry.replace_settings(previous, expected_revision=revision)
        except ValueError:
            # Another terminal/app owns its newer choice. Never overwrite it.
            pass
        else:
            if activated:
                activate_models(state, registry._resolved(previous), start=False)
                configure(state)
        raise
    pi_provider, pi_model = model_identity(identifier, profile, committed)
    return {'id': identifier, 'name': profile.get('name') or identifier,
            'pi_provider': pi_provider, 'pi_model': pi_model}


def ensure_local_selected(state, identifier):
    """Reconcile a session's local worker before a turn, without implicit downloads."""
    registry = ModelRegistry(state)
    profile = registry.get(identifier)
    if profile.get('backend', 'local') != 'local':
        raise ValueError('Ожидался локальный профиль модели.')
    return select_model(state, identifier)
