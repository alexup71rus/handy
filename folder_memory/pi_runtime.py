"""Small adapter between Handy's app-owned services and the pi CLI."""

import json
import os
from pathlib import Path
import shlex
import shutil
import sys
from urllib.parse import urlsplit

from .runtime import engine_settings, process_lock, write_json

PROJECT = Path(__file__).resolve().parents[1]
PI_VERSION = '1.5.2'
BROWSER_VERSION = '1.13.0'
DEFAULTS = dict(browser_enabled=True, browser_cdp_url='', artifacts_enabled=True,
                scheduler_enabled=True, notifications_enabled=False, memory_enabled=True, rag_enabled=True,
                theme='system', thinking_level='off')


def agent_dir(state):
    return Path(state) / 'pi' / 'agent'


def session_dir(state):
    """One native pi session library, independent of project folders."""
    return Path(state) / 'pi' / 'sessions'


def executable(name='pi'):
    path = PROJECT / '.runtime/pi/node_modules/.bin' / name
    if not path.is_file():
        raise ValueError('Установи pi и MCP браузера: python3 scripts/install_cli.py')
    if name == 'pi':
        from .pi_localization import localized_cli
        return str(localized_cli(PROJECT / '.runtime/pi', PI_VERSION))
    return str(path)


def load_settings(state):
    try:
        data = json.loads((Path(state) / 'pi-settings.json').read_text())
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError('Настройки pi должны быть JSON-объектом.')
    data.pop('context_budget_tokens', None)  # Retired preview of early compaction.
    return validate_settings({**DEFAULTS, **data})


def validate_settings(data):
    if not isinstance(data, dict) or set(data) - DEFAULTS.keys():
        raise ValueError('Неизвестные настройки pi.')
    data = {**DEFAULTS, **data}
    for key in ('browser_enabled', 'artifacts_enabled', 'scheduler_enabled', 'notifications_enabled', 'memory_enabled', 'rag_enabled'):
        if type(data[key]) is not bool:
            raise ValueError(key + ' должен быть true или false.')
    if data['theme'] not in ('system', 'dark', 'light'):
        raise ValueError('Выбери системную, светлую или тёмную тему.')
    if data['thinking_level'] not in ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('Неизвестный уровень рассуждений.')
    url = data['browser_cdp_url']
    if not isinstance(url, str):
        raise ValueError('Адрес Chrome должен быть строкой.')
    if url:
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1')
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(c.isspace() for c in url)):
            raise ValueError('Для Chrome нужен локальный HTTP-адрес, например http://127.0.0.1:9222.')
        try:
            parsed.port
        except ValueError:
            raise ValueError('Некорректный порт Chrome.') from None
    return data


def save_settings(state, values):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not isinstance(values, dict):
        raise ValueError('Ожидается объект настроек.')
    with process_lock(state / 'pi-settings.lock', wait=True):
        previous = load_settings(state)
        data = validate_settings({**previous, **values})
        write_json(state / 'pi-settings.json', data)
    configure(state)
    if data['browser_enabled'] != previous['browser_enabled']:
        _set_mcp_enabled(state, 'browser', data['browser_enabled'])
    from .pi_scheduler import start_worker, stop_worker
    (start_worker if data['scheduler_enabled'] else stop_worker)(state)
    return data


def _command(*args):
    return '!' + shlex.join([sys.executable, '-m', 'folder_memory.pi_control', *map(str, args)])


def configure(state):
    """Serialize Handy exports and preserve pi's unrelated preferences and servers."""
    state = Path(state).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with process_lock(state / 'pi-config.lock', wait=True):
        return _configure_locked(state)


def _configure_locked(state):
    from .model_registry import ModelRegistry
    from .model_backend import model_config_digest
    from .pi_models import api_endpoint, model_identity
    state = Path(state).resolve()
    directory = agent_dir(state)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    sessions = session_dir(state)
    sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings = load_settings(state)
    registry = ModelRegistry(state)
    data = registry.load()
    providers = {}
    endpoints = {}
    selected = data.get('selected_chat')
    default_provider = default_model = None
    profiles = {identifier: profile for identifier, profile in data['profiles'].items()
                if profile['kind'] == 'chat' and profile.get('visible', True)
                and (profile.get('backend', 'local') != 'local' or data.get('standard_provider_visible', True))
                and data.get('providers', {}).get(profile.get('provider_id'), {}).get('visible', True)}
    # Hiding the active profile must not silently route the next request to
    # another provider. Keep the selected model available as the default.
    if selected in data['profiles']:
        profiles.setdefault(selected, data['profiles'][selected])
    for identifier, profile in profiles.items():
        if not profile.get('enabled', True) or not registry.profile_ready(profile):
            continue
        local = profile.get('backend', 'local') == 'local'
        if not local:
            base = profile['base_url']
            if base not in endpoints:
                try:
                    endpoints[base] = api_endpoint(state, profile)
                except (OSError, ValueError) as error:
                    endpoints[base] = error
            endpoint = endpoints[base]
            if isinstance(endpoint, Exception):
                if identifier == selected:
                    raise endpoint
                continue
        provider, model = model_identity(identifier, profile, data)
        entry = {'name': profile.get('name', identifier), 'id': model,
                 'reasoning': bool(profile.get('enable_thinking', False)),
                 'input': ['text', 'image'] if profile.get('mmproj') else ['text'],
                 'contextWindow': profile.get('context', 32768),
                 'maxTokens': profile.get('max_output_tokens', min(4096 if local else 8192, profile.get('context', 32768) // 4)),
                 'samplingParams': profile.get('sampling', {})}
        if local:
            entry['headers'] = {'X-Handy-Model-Config': model_config_digest(registry._resolved({**data, 'selected_chat': identifier}))}
            entry['compat'] = {'supportsStore': False, 'supportsDeveloperRole': False,
                               'supportsReasoningEffort': False, 'maxTokensField': 'max_tokens'}
            entry['samplingParamsByThinkingLevel'] = {
                level: {'chat_template_kwargs': {'enable_thinking': level != 'off' and bool(profile.get('enable_thinking', False))}}
                for level in ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')}
        provider_name = ('Системные модели Handy' if local else
                         data.get('providers', {}).get(profile.get('provider_id'), {}).get('name', profile.get('name', identifier)))
        connection = providers.setdefault(provider, {'name': provider_name, 'api': 'openai-completions',
                               'baseUrl': f'http://127.0.0.1:{engine_settings(state)["port"]}/v1' if local else endpoint,
                               'apiKey': _command('--state-dir', state, 'credential', '--profile', identifier),
                               'authHeader': True, 'models': []})
        if not any(item['id'] == model for item in connection['models']):
            connection['models'].append(entry)
        elif identifier == selected:
            connection['models'] = [entry if item['id'] == model else item for item in connection['models']]
        if local:
            providers[provider]['headers'] = {'X-Handy-Client': 'pi'}
        if identifier == selected:
            default_provider, default_model = provider, model
    # Preserve pi's own non-managed preferences such as keybindings and packages.
    path = directory / 'settings.json'
    existing = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(existing, dict):
        raise ValueError('В настройках pi settings.json нужен JSON-объект.')
    existing.update(theme=settings['theme'], defaultThinkingLevel=settings['thinking_level'],
                    quietStartup='header', sessionDir=str(sessions),
                    codemode={'mode': 'on', 'inlineBudget': 0}, showHardwareCursor=True)
    compaction = dict(existing.get('compaction', {}))
    overrides = {key: value for key, value in compaction.get('modelOverrides', {}).items()
                 if not key.startswith('handy-')}
    for provider_id, provider in providers.items():
        for model in provider['models']:
            context = model['contextWindow']
            if provider_id == 'handy-system':
                # Reserve room for the actual output, rather than forcing a
                # fixed early threshold for small models. Leave API defaults alone.
                reserve = min(context // 2, model['maxTokens'] + 1024)
                overrides[f'{provider_id}/{model["id"]}'] = {
                    'reserveTokens': reserve, 'keepRecentTokens': min(8192, max(256, (context - reserve) // 3))}
    compaction.setdefault('enabled', True)
    compaction['modelOverrides'] = overrides
    existing['compaction'] = compaction
    if default_provider:
        existing.update(defaultProvider=default_provider, defaultModel=default_model)
    else:
        existing.pop('defaultProvider', None)
        existing.pop('defaultModel', None)
    mcp = _read_mcp(state)
    previous_servers = mcp.get('mcpServers', {})
    servers = {name: entry for name, entry in previous_servers.items() if name not in {'handy', 'browser'}}
    args = ['-m', 'folder_memory.mcp_server', '--state-dir', str(state), '--root', '.']
    if not settings['artifacts_enabled']:
        args.append('--no-artifacts')
    if not settings['scheduler_enabled']:
        args.append('--no-scheduler')
    servers['handy'] = {'command': sys.executable, 'args': args, 'cwd': '.',
                       'env': {'PYTHONPATH': str(PROJECT)}, 'exposure': 'direct',
                       'enabled': previous_servers.get('handy', {}).get('enabled', True),
                       'description': 'Handy artifacts (timers, charts, tables), scheduled chats, saved memory and knowledge search.'}
    args = ['--no-usage-statistics']
    if settings['browser_cdp_url']:
        args += ['--browserUrl', settings['browser_cdp_url']]
    servers['browser'] = {'command': executable('chrome-devtools-mcp'), 'args': args, 'exposure': 'deferred',
                          'enabled': previous_servers.get('browser', {}).get('enabled', settings['browser_enabled']),
                          'description': 'Browse pages, inspect Chrome, take screenshots and interact with websites.'}
    write_json(directory / 'models.json', {'providers': providers})
    write_json(path, existing)
    write_json(directory / 'mcp.json', {**mcp, 'mcpServers': servers})
    return directory



def _read_mcp(state):
    try:
        document = json.loads((agent_dir(state) / 'mcp.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'mcpServers': {}}
    servers = document.get('mcpServers', {}) if isinstance(document, dict) else None
    if not isinstance(servers, dict) or any(not isinstance(entry, dict) for entry in servers.values()):
        raise ValueError('В mcp.json нужен объект mcpServers с настройками серверов.')
    return document


def _set_mcp_enabled(state, identifier, enabled):
    if not isinstance(identifier, str) or not identifier or type(enabled) is not bool:
        raise ValueError('Нужны ID сервера и enabled: true или false.')
    with process_lock(Path(state) / 'pi-config.lock', wait=True):
        document = _read_mcp(state)
        server = document.get('mcpServers', {}).get(identifier)
        if server is None:
            raise ValueError('MCP-сервер не найден. Обнови список.')
        server['enabled'] = enabled
        write_json(agent_dir(state) / 'mcp.json', document)


def set_mcp_enabled(state, identifier, enabled):
    """MCP availability is independent of Handy features and background tasks."""
    state = Path(state).expanduser().resolve()
    configure(state)
    _set_mcp_enabled(state, identifier, enabled)
    if identifier == 'browser':
        with process_lock(state / 'pi-settings.lock', wait=True):
            settings = load_settings(state)
            settings['browser_enabled'] = enabled
            write_json(state / 'pi-settings.json', settings)
    return {'servers': mcp_servers(state), 'settings': load_settings(state)}


def mcp_servers(state):
    """Passive, credential-free inventory; connecting is always an explicit action."""
    configured = _read_mcp(state).get('mcpServers', {})
    labels = {'handy': 'Handy', 'browser': 'Браузер · Chrome DevTools'}
    rows = []
    for identifier in dict.fromkeys(['handy', 'browser', *configured]):
        entry = configured.get(identifier)
        raw = entry or {}
        url = raw.get('url')
        if isinstance(url, str):
            try:
                parsed = urlsplit(url)
                detail = f'{parsed.scheme}://{parsed.hostname or ""}'
            except ValueError:
                detail = 'Некорректный URL'
            transport = 'http'
        else:
            detail = Path(str(raw.get('command', ''))).name
            transport = 'stdio'
        rows.append({'id': identifier, 'name': labels.get(identifier, identifier),
                     'enabled': entry is not None and raw.get('enabled', True) is not False,
                     'transport': transport, 'detail': detail,
                     'managed': identifier in labels})
    return rows

def environment(state):
    try:
        settings = json.loads((agent_dir(state) / 'settings.json').read_text())
    except FileNotFoundError:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError('В настройках pi settings.json нужен JSON-объект.')
    require_selection = not (settings.get('defaultProvider') and settings.get('defaultModel'))
    node = shutil.which('node')
    path = os.environ.get('PATH', '/usr/bin:/bin')
    if node:
        path = str(Path(node).parent) + os.pathsep + path
    else:
        for candidate in ('/opt/homebrew/bin', '/usr/local/bin'):
            if Path(candidate, 'node').is_file():
                path = candidate + os.pathsep + path
                break
    return {**os.environ, 'PATH': path, 'PYTHONPATH': str(PROJECT),
            'PI_CODING_AGENT_DIR': str(agent_dir(state)), 'PI_TELEMETRY': '0',
            'HANDY_STATE_DIR': str(state), 'HANDY_PYTHON': sys.executable,
            'HANDY_REQUIRE_MODEL_SELECTION': '1' if require_selection else '0'}


def prepare(state, *, allow_unconfigured=False):
    from .model_registry import ModelRegistry
    from .model_activation import activate_models
    state = Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    registry = ModelRegistry(state)
    data = registry.load()
    selected = data.get('selected_chat')
    def available(profile):
        return profile.get('enabled', True) and registry.profile_ready(profile)
    if not selected or not available(data['profiles'][selected]):
        if allow_unconfigured and not any(available(profile) for profile in data['profiles'].values()
                                          if profile['kind'] == 'chat'):
            # A fresh installation can open /models and offer a download.
            # Never substitute another configured (possibly paid) provider.
            return configure(state)
        raise ValueError('Выбери готовую модель в Handy.app: handy --settings')
    # Service keeps llama.cpp alive and reaps idle models; pi handles the agent loop.
    activate_models(state, registry._resolved(data))
    return configure(state)


def command(state, args=()):
    return [executable(), '--extension', str(PROJECT / 'integration/pi/handy.ts'), *args]


def scheduled_command(state, cwd, session_path, prompt):
    prepare(state)
    return command(state, ['--print', '--mode', 'json', '--session', str(session_path), '--', prompt]), environment(state)
