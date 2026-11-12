"""Remove only verified Handy-owned OpenCode configuration, leaving chats intact."""

import argparse
import copy
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import unquote, urlsplit


PROJECT = Path(__file__).resolve().parents[1]
MODEL = 'funny/memory-engine'
_PACKAGES = ('@opencode/ai/providers/openai-compatible', '@ai-sdk/openai-compatible')


def config_path():
    if directory := os.environ.get('OPENCODE_CONFIG_DIR'):
        return Path(directory).expanduser() / 'opencode.json'
    directory = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    if not directory.is_absolute():
        directory = Path.home() / '.config'
    return directory / 'opencode/opencode.json'


def _owned_provider(provider):
    if not isinstance(provider, dict) or provider.get('name') not in ('Handy', 'Funny'):
        return False
    if provider.get('package', provider.get('npm')) not in _PACKAGES:
        return False
    models = provider.get('models')
    if not isinstance(models, dict) or set(models) != {'memory-engine'}:
        return False
    settings = provider.get('settings', provider.get('options'))
    if not isinstance(settings, dict) or not isinstance(settings.get('baseURL'), str):
        return False
    try:
        url = urlsplit(settings['baseURL'])
        return (url.scheme == 'http' and url.hostname in {'127.0.0.1', 'localhost', '::1'}
                and url.port is not None and url.path.rstrip('/') == '/v1'
                and not any((url.username, url.password, url.query, url.fragment)))
    except ValueError:
        return False


def _owned_plugin(entry):
    if not isinstance(entry, str):
        return False
    if entry.startswith('file:'):
        url = urlsplit(entry)
        if url.netloc not in {'', 'localhost'} or url.query or url.fragment:
            return False
        entry = unquote(url.path)
    if not os.path.isabs(entry):
        return False
    candidate = Path(entry).resolve()
    roots = [PROJECT]
    if PROJECT.name == 'handy':
        roots.append(PROJECT.with_name('funny'))
    plugins = {root / 'integration/opencode-plugin' for root in roots}
    return candidate in plugins or any(candidate == plugin / 'index.js' for plugin in plugins)


def _updated(original):
    if not isinstance(original, dict):
        raise ValueError('OpenCode configuration must be a JSON object.')
    for section in ('provider', 'providers', 'agent', 'agents', 'command', 'commands'):
        if section in original and not isinstance(original[section], dict):
            raise ValueError(f'OpenCode {section} must be an object.')
    for section in ('plugin', 'plugins'):
        if section in original and not isinstance(original[section], list):
            raise ValueError(f'OpenCode {section} must be an array.')
    # Verify every occurrence first; a foreign funny provider must never be removed.
    for section in ('provider', 'providers'):
        if 'funny' in original.get(section, {}) and not _owned_provider(original[section]['funny']):
            raise ValueError(f'{section}.funny is not a verified Handy provider; nothing was changed.')
    updated = copy.deepcopy(original)
    removed = []
    for section in ('provider', 'providers'):
        if 'funny' in updated.get(section, {}):
            del updated[section]['funny']
            removed.append(section + '.funny')
    agents = set()
    for section in ('agent', 'agents'):
        for name in ('funny', 'handy'):
            entry = updated.get(section, {}).get(name)
            if isinstance(entry, dict) and entry.get('model') == MODEL:
                del updated[section][name]
                agents.add(name)
                removed.append(section + '.' + name)
    for section in ('command', 'commands'):
        for name, entry in list(updated.get(section, {}).items()):
            agent = entry.get('agent') if isinstance(entry, dict) else None
            if isinstance(entry, dict) and (entry.get('model') == MODEL or isinstance(agent, str) and agent in agents):
                del updated[section][name]
                removed.append(section + '.' + name)
    for section in ('plugin', 'plugins'):
        if section in updated:
            keep = []
            for index, entry in enumerate(updated[section]):
                if _owned_plugin(entry):
                    removed.append(f'{section}[{index}]')
                else:
                    keep.append(entry)
            updated[section] = keep
    for field in ('model', 'small_model'):
        if updated.get(field) == MODEL:
            del updated[field]
            removed.append(field)
    if isinstance(updated.get('default_agent'), str) and updated['default_agent'] in agents:
        del updated['default_agent']
        removed.append('default_agent')
    return updated, removed


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate configuration keys are ambiguous; nothing was changed.')
        result[key] = value
    return result


def _assert_unchanged(path, contents):
    if path.read_bytes() != contents:
        raise RuntimeError('OpenCode configuration changed during removal. Run the command again.')


def _write(path, contents, updated):
    backups = PROJECT / '.runtime/config-backups'
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backups, 0o700)
    _assert_unchanged(path, contents)
    with tempfile.NamedTemporaryFile('wb', prefix='opencode-before-remove-', suffix='.json',
                                     dir=backups, delete=False) as stream:
        backup = Path(stream.name)
        os.fchmod(stream.fileno(), 0o600)
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', prefix='.opencode-remove-',
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(updated, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        _assert_unchanged(path, contents)
        os.replace(temporary, path)
        return backup
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def remove(path=None, *, dry_run=False):
    path = (Path(path) if path is not None else config_path()).expanduser().resolve()
    try:
        contents = path.read_bytes()
    except FileNotFoundError:
        return {'path': str(path), 'removed': [], 'changed': False, 'backup': None, 'dry_run': dry_run}
    original = json.loads(contents, object_pairs_hook=_unique_pairs)
    updated, removed = _updated(original)
    backup = _write(path, contents, updated) if removed and not dry_run else None
    return {'path': str(path), 'removed': removed, 'changed': bool(removed),
            'backup': str(backup) if backup else None, 'dry_run': dry_run}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='OpenCode JSON configuration (default: global config)')
    parser.add_argument('--dry-run', action='store_true', help='Report owned entries without writing any files')
    args = parser.parse_args()
    try:
        result = remove(args.config, dry_run=args.dry_run)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f'Handy: {error}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
