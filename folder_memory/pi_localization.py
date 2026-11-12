"""Build Handy's Russian UI from pinned pi sources, without modifying npm packages."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from .runtime import process_lock, write_json

PROJECT = Path(__file__).resolve().parents[1]
CATALOGUES = PROJECT / 'integration/pi/localization'


def _copy_package(source, destination):
    destination.mkdir(parents=True)
    shutil.copy2(source / 'package.json', destination / 'package.json')
    shutil.copytree(source / 'dist', destination / 'dist', ignore=shutil.ignore_patterns('bundle', '*.map', '*.d.ts'))
    for name in ('native', 'docs', 'examples', 'README.md', 'CHANGELOG.md'):
        if (source / name).exists():
            (destination / name).symlink_to(source / name, target_is_directory=(source / name).is_dir())


def _apply_catalogues(roots, entries):
    changed = set()
    for entry in entries:
        package = entry.get('package', 'coding-agent')
        if package not in roots:
            raise ValueError('Неизвестный пакет в переводе pi: ' + package)
        relative = Path(entry['file'])
        if relative.is_absolute() or '..' in relative.parts or relative.suffix != '.js':
            raise ValueError('Некорректный путь в переводе pi.')
        path = roots[package] / 'dist' / relative
        source = path.read_text(encoding='utf-8')
        expected = entry.get('count', 1)
        if source.count(entry['old']) != expected:
            raise ValueError(f'Перевод pi не соответствует версии пакета: {relative}: {entry["old"][:90]}')
        path.write_text(source.replace(entry['old'], entry['new']), encoding='utf-8')
        changed.add(path)
    return changed


def localized_cli(pi_root, version):
    """Generate one cached, private runtime. Reinstalling npm never loses the translation."""
    pi_root = Path(pi_root).resolve()
    source = pi_root / 'node_modules/@earendil-works/pi-coding-agent'
    tui_source = pi_root / 'node_modules/@earendil-works/pi-tui'
    try:
        package_metadata = [(package / 'package.json').read_bytes() for package in (source, tui_source)]
    except FileNotFoundError:
        raise ValueError('Сначала установи pi: python3 scripts/install_cli.py') from None
    for raw in package_metadata:
        actual = json.loads(raw)['version']
        if actual != version:
            raise ValueError(f'Для перевода Handy нужен pi {version}; установлен {actual}. Повтори установку Handy.')
    catalogues = sorted(CATALOGUES.glob('*.ru.json'))
    entries = []
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for raw in package_metadata:
        digest.update(raw)
    for path in catalogues:
        raw = path.read_bytes()
        digest.update(raw)
        entries.extend(json.loads(raw))
    key = digest.hexdigest()
    destination = pi_root / 'handy-ru'
    marker = destination / '.handy-localization.json'
    with process_lock(pi_root / 'localization.lock', wait=True):
        try:
            cached = json.loads(marker.read_text())
            if (isinstance(cached, dict) and cached.get('digest') == key
                    and (destination / 'dist/cli.js').is_file()):
                return destination / 'dist/cli.js'
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        with tempfile.TemporaryDirectory(prefix='.handy-ru-', dir=pi_root) as temporary:
            staging = Path(temporary) / 'handy-ru'
            _copy_package(source, staging)
            tui = staging / 'node_modules/@earendil-works/pi-tui'
            _copy_package(tui_source, tui)
            changed = _apply_catalogues({'coding-agent': staging, 'tui': tui}, entries)
            node = shutil.which('node') or next((str(Path(p) / 'node') for p in ('/opt/homebrew/bin', '/usr/local/bin') if (Path(p) / 'node').is_file()), None)
            if not node:
                raise ValueError('Для pi нужен Node.js 22.19 или новее.')
            for path in sorted(changed):
                check = subprocess.run([node, '--check', str(path)], capture_output=True, text=True)
                if check.returncode:
                    raise ValueError('Ошибка в переводе pi: ' + check.stderr[:600])
            (staging / 'dist/cli.js').chmod(0o755)
            write_json(staging / '.handy-localization.json', {'digest': key, 'version': version,
                                                           'patches': len(entries), 'files': len(changed)})
            previous = Path(temporary) / 'previous'
            if destination.exists():
                destination.rename(previous)
            try:
                staging.rename(destination)
            except BaseException:
                if previous.exists():
                    previous.rename(destination)
                raise
    return destination / 'dist/cli.js'
