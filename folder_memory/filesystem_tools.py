"""Optional local read primitives, independent of shell quoting and core routing."""

import json
import os
from pathlib import Path
import stat
import unicodedata

from .file_access import (
    MAX_WINDOW_CHARS, MAX_WINDOW_LINES, is_private, open_resolved_file,
    read_text_window, require_user_path, system_path,
)


_INTERNAL_DIRECTORIES = frozenset({'.git', '.handy', '.funny', 'node_modules', '.venv', 'venv', '__pycache__'})
_MAX_TREE_CHARS = 16000
_MAX_SCANNED_ENTRIES = 4000


def _target(root, path):
    root = require_user_path(Path(root).resolve(strict=True))
    if not root.is_dir() or is_private(root):
        raise ValueError('Выбери рабочую папку с данными.')
    if not isinstance(path, str) or not path.strip() or len(path) > 4096 or '\0' in path:
        raise ValueError('Нужен непустой путь к файлу или папке.')
    supplied = Path(path).expanduser()
    target = require_user_path(supplied if supplied.is_absolute() else root / supplied)
    if is_private(target):
        raise ValueError('Содержимое файлов и папок с секретами через этот инструмент не читается.')
    return root, target


def read_file(root, *, path='.', offset=0, revision=None,
              max_lines=MAX_WINDOW_LINES, max_chars=MAX_WINDOW_CHARS):
    """Reuse the shared safe reader and its byte/character continuation contract."""
    root, target = _target(root, path)
    if target.is_dir():
        raise ValueError('Это папка. Для её содержимого используй list_directory.')
    if revision is not None and not isinstance(revision, str):
        raise ValueError('revision должна быть строкой из предыдущего результата.')
    window = read_text_window(target.parent, target.name, offset=offset, revision=revision,
                              max_chars=max_chars, max_lines=max_lines)
    return {'status': 'ok', 'text': 'Прочитан фрагмент файла.', 'data': {
        'root': str(root), 'path': str(window.path), 'text': window.text,
        'offset': window.offset, 'next_offset': window.next_offset, 'revision': window.revision,
        'total': window.total, 'unit': window.unit, 'partial': window.partial,
        'truncated': window.next_offset is not None, 'eof': window.next_offset is None,
        'max_lines': max_lines, 'max_chars': max_chars}}


def _caption(value):
    result = []
    for character in value:
        number = ord(character)
        if unicodedata.category(character) in {'Cc', 'Cf', 'Zl', 'Zp'} or 0x2500 <= number <= 0x257f:
            result.append(f'\\u{number:04x}' if number <= 0xffff else f'\\U{number:08x}')
        else:
            result.append('\\\\' if character == '\\' else character)
    return ''.join(result)


def _tree(directory, entries):
    lines = [_caption(str(directory)) + '/']
    reasons = {'service_directory': 'служебная папка', 'private': 'приватная папка',
               'system_directory': 'системная папка', 'depth_limit': 'предел глубины',
               'entry_limit': 'предел вывода', 'unavailable': 'недоступно'}

    def append(items, prefix):
        for index, row in enumerate(items):
            last = index == len(items) - 1
            name = _caption(row['name'])
            if row['type'] == 'directory':
                name += '/'
            elif row['type'] == 'symlink':
                name += '@ [ссылка, без обхода]'
            if row.get('skip_reason'):
                name += ' [' + reasons.get(row['skip_reason'], row['skip_reason']) + ']'
            lines.append(prefix + ('└── ' if last else '├── ') + name)
            if row.get('children'):
                append(row['children'], prefix + ('    ' if last else '│   '))

    append(entries, '')
    return '\n'.join(lines)


def list_directory(root, *, path='.', depth=2, max_entries=100, include_hidden=False):
    """Return a bounded tree; explicit directory targets can include service dirs."""
    if type(depth) is not int or not 1 <= depth <= 4:
        raise ValueError('depth должна быть целым числом от 1 до 4.')
    if type(max_entries) is not int or not 1 <= max_entries <= 200:
        raise ValueError('max_entries должно быть целым числом от 1 до 200.')
    if type(include_hidden) is not bool:
        raise ValueError('include_hidden должен быть boolean.')
    root, target = _target(root, path)
    home = Path.home().resolve()
    data = {'root': str(root), 'path': str(target), 'depth': depth, 'include_hidden': include_hidden,
            'entries': [], 'returned_entries': 0, 'partial': False, 'truncated': False,
            'symlinks_followed': False, 'hidden_filtered': False, 'tree': _caption(str(target)) + '/'}
    budget = {'chars': _MAX_TREE_CHARS - len(json.dumps(data, ensure_ascii=False)) - 512, 'scanned': 0}
    reasons = set()

    def omitted(reason, *, truncated=False, partial=True):
        data['partial'] = data['partial'] or partial
        data['truncated'] = data['truncated'] or truncated
        reasons.add(reason)

    def walk(descriptor, directory, levels):
        candidates, result, children = [], [], []
        remaining = max_entries - data['returned_entries']
        with os.scandir(descriptor) as entries:
            for entry in entries:
                budget['scanned'] += 1
                if budget['scanned'] > _MAX_SCANNED_ENTRIES:
                    omitted('scan_limit', truncated=True)
                    break
                if not include_hidden and entry.name.startswith('.'):
                    data['hidden_filtered'] = True
                    omitted('hidden', partial=False)
                    continue
                if len(candidates) >= remaining:
                    omitted('entry_limit', truncated=True)
                    break
                try:
                    candidates.append((entry.name, entry.stat(follow_symlinks=False), None))
                except OSError as error:
                    candidates.append((entry.name, None, type(error).__name__))
        for name, info, error in sorted(candidates, key=lambda item: (item[0].casefold(), item[0])):
            child = directory / name
            row = {'name': name, 'path': os.path.relpath(child, root)}
            if info is not None:
                kind = ('symlink' if stat.S_ISLNK(info.st_mode) else 'directory' if stat.S_ISDIR(info.st_mode)
                        else 'file' if stat.S_ISREG(info.st_mode) else 'other')
                row['type'] = kind
                if kind == 'file':
                    row['size_bytes'] = info.st_size
                if is_private(child):
                    row['private'] = True
                if kind == 'directory':
                    row.update(children=[], expanded=False)
                    skip = ('private' if row.get('private') else 'system_directory' if system_path(child, home)
                            else 'service_directory'
                            if name.casefold() in _INTERNAL_DIRECTORIES else 'depth_limit' if levels <= 1 else None)
                    if skip:
                        row['skip_reason'] = skip
                        omitted(skip, partial=skip == 'depth_limit')
                elif kind == 'symlink':
                    row['followed'] = False
                    omitted('symlink', partial=False)
            else:
                row.update(type='unavailable', error=error)
                omitted('unavailable')
            # Include the factual tree caption, error fields, and transport escaping.
            cost = (len(json.dumps(row, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e'))
                    + len(_caption(name)) + 128)
            if cost > budget['chars']:
                omitted('output_limit', truncated=True)
                break
            budget['chars'] -= cost
            data['returned_entries'] += 1
            result.append(row)
            if row['type'] == 'directory' and 'skip_reason' not in row:
                children.append((name, child, info, row))
        for name, child, observed, row in children:
            if data['returned_entries'] >= max_entries or budget['chars'] <= 0:
                row['skip_reason'] = 'entry_limit'
                omitted('entry_limit', truncated=True)
                continue
            try:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
                child_fd = os.open(name, flags, dir_fd=descriptor)
                try:
                    current = os.fstat(child_fd)
                    if (current.st_dev, current.st_ino) != (observed.st_dev, observed.st_ino):
                        raise ValueError('Папка изменилась во время чтения.')
                    row['children'] = walk(child_fd, child, levels - 1)
                    row['expanded'] = True
                finally:
                    os.close(child_fd)
            except (OSError, ValueError) as error:
                row.update(skip_reason='unavailable', error=type(error).__name__)
                omitted('unavailable')
        return result

    descriptor = open_resolved_file(target)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError('Это не папка. Для текста файла используй read_file.')
        data['entries'] = walk(descriptor, target, depth)
    finally:
        os.close(descriptor)
    data['omitted_reasons'] = sorted(reasons)
    data['tree'] = _tree(target, data['entries'])
    return {'status': 'ok', 'text': 'Получено содержимое папки.', 'data': data}
