"""Shared rules for reading files and keeping observations out of system trees."""

import codecs
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import stat

from .document_text import extract_docx


MAX_FILE_BYTES = 256 * 1024
MAX_TEXT_COUNT_BYTES = 64 * 1024 * 1024
MAX_WINDOW_CHARS = 5000
MAX_WINDOW_LINES = 80
_LINE_BREAKS = '\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029'
ARTIFACT_NAMESPACE = '.handy'
ARTIFACT_NAMESPACES = frozenset({ARTIFACT_NAMESPACE, '.funny'})
_DOCUMENT_SUFFIXES = {'.docx', '.pdf', '.xlsx'}
SYSTEM_DIRECTORIES = frozenset({
    'system', 'library', 'applications', 'bin', 'sbin', 'usr', 'etc',
    'dev', 'proc', 'sys', 'run', 'boot', 'root',
})


class SystemPathError(ValueError):
    pass


class TextSourceChanged(ValueError):
    def __init__(self):
        super().__init__('Файл изменился после чтения. Открой его заново, чтобы начать с первого фрагмента.')


@dataclass(frozen=True)
class TextWindow:
    path: Path
    text: str
    offset: int
    next_offset: int | None
    revision: str
    total: int
    unit: str

    @property
    def partial(self):
        return self.offset > 0 or self.next_offset is not None


def system_path(path, home):
    parts = tuple(part.casefold() for part in path.parts[1:])
    home_parts = tuple(part.casefold() for part in home.parts[1:])
    if (parts and parts[0] == 'private' and parts[:2] != ('private', 'tmp')
            and parts[:3] not in {('private', 'var', 'folders'), ('private', 'var', 'tmp')}):
        return True
    return (not parts or parts[0] in SYSTEM_DIRECTORIES
            or parts == ('var',)
            or len(parts) >= 2 and parts[0] == 'var' and parts[1] in {'db', 'lib', 'log', 'run', 'lock', 'spool', 'cache'}
            or parts[:len(home_parts)+1] == (*home_parts, 'library'))


def require_user_path(path):
    """Keep file observations and navigation out of system trees."""
    candidate = Path(path).expanduser().resolve()
    if system_path(candidate, Path.home().resolve()):
        raise SystemPathError('Это системный путь. Выбери файл или папку с рабочими данными.')
    return candidate


def safe_path(root, relative="."):
    root = Path(root).expanduser().resolve(strict=True)
    candidate = require_user_path(root / relative)
    if not candidate.is_relative_to(root):
        raise ValueError("Путь выходит за выбранную папку. Сначала выбери её командой «папка: /путь».")
    return candidate


PRIVATE_DIRECTORIES = frozenset({".ssh", ".gnupg", ".aws", ".runtime"})
PRIVATE_SUFFIXES = frozenset({".pem", ".key", ".p12"})
_MODEL_METADATA_BYTES = 2 * 1024 * 1024


@lru_cache(maxsize=8)
def _configured_key_paths(path, revision):
    """Cache key references by registry revision without loading or creating profiles."""
    try:
        with os.fdopen(open_resolved_file(path), 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MODEL_METADATA_BYTES:
                return frozenset()
            raw = stream.read(_MODEL_METADATA_BYTES + 1)
        if len(raw) > _MODEL_METADATA_BYTES:
            return frozenset()
        data = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return frozenset()
    paths = set()
    for collection in ('profiles', 'providers'):
        records = data.get(collection, {}) if isinstance(data, dict) else {}
        for record in records.values() if isinstance(records, dict) else ():
            value = record.get('api_key_file') if isinstance(record, dict) else None
            if isinstance(value, str) and value:
                try:
                    candidate = Path(value).expanduser()
                    if candidate.is_absolute():
                        paths.add(candidate)
                except (OSError, ValueError, RuntimeError):
                    continue
    return frozenset(paths)


def _is_model_secret(path):
    from .model_registry import config_root
    try:
        root = config_root().resolve()
        candidate = path.expanduser().resolve()
        # Cover legacy extensionless keys as well as keys created by current setup.
        if candidate.is_relative_to((root / 'credentials').resolve()):
            return True
        metadata = root / 'models.json'
        info = metadata.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return False
        revision = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        for reference in _configured_key_paths(metadata, revision):
            try:
                if candidate == reference.resolve():
                    return True
            except (OSError, ValueError, RuntimeError):
                continue
        return False
    except (OSError, ValueError, RuntimeError):
        return False


def is_private(path):
    p = Path(path)
    name = p.name.casefold()
    return (any(part.casefold() in PRIVATE_DIRECTORIES for part in p.parts)
            or name == ".env" or name.startswith(".env.") and not name.endswith(("example", "sample"))
            or p.suffix.casefold() in PRIVATE_SUFFIXES
            or _is_model_secret(p))


def decode_text(raw, source):
    # This decoder also ships in the standalone SSH worker.
    if source.suffix.lower() == ".docx":
        return extract_docx(raw, source=source).text
    return _decode_utf8(raw)


def _decode_utf8(raw, *, first=True, final=True):
    if b"\0" in raw:
        raise ValueError("Это двоичный файл. Для его содержимого нужен соответствующий адаптер.")
    if first and raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8):]
    try:
        return raw.decode('utf-8') if final else codecs.getincrementaldecoder('utf-8')().decode(raw)
    except UnicodeDecodeError:
        raise ValueError("Файл не в UTF-8. Адаптер других кодировок пока не подключён.") from None


def open_resolved_file(path):
    """Open a validated absolute path without following links added after resolution."""
    access = getattr(os, "O_SEARCH", getattr(os, "O_PATH", os.O_RDONLY))
    directories = access | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(path.anchor, directories)
    try:
        for name in path.parts[1:-1]:
            child = os.open(name, directories, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(path.name or ".", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)


@contextmanager
def _open_file(root, relative, *, revision=None):
    p = safe_path(root, relative)
    if not p.is_file():
        if revision is not None:
            raise TextSourceChanged()
        raise ValueError("Это не обычный файл.")
    if is_private(p):
        raise ValueError("Содержимое файлов с секретами не индексируется в MVP; видны только их имена.")
    try:
        fd = open_resolved_file(p)
    except OSError:
        if revision is not None:
            raise TextSourceChanged() from None
        raise
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if revision is not None and revision != _revision(info):
            raise TextSourceChanged()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Это не обычный файл.')
        yield p, stream, info


def _read_bounded(p, stream, info):
    pdf = p.suffix.lower() == '.pdf'
    limit = MAX_FILE_BYTES
    if pdf:
        from .pdf_text import MAX_PDF_BYTES
        limit = MAX_PDF_BYTES
    raw = stream.read(limit + 1) if info.st_size <= limit else None
    if raw is None or len(raw) > limit:
        raise ValueError('Выбери PDF размером до 8 МиБ.' if pdf else 'Файл больше 256 КиБ. Выбери меньший файл.')
    return raw


def _read_file(root, relative):
    with _open_file(root, relative) as (p, stream, info):
        return p, _read_bounded(p, stream, info)


def _decode_file(raw, p):
    if p.suffix.lower() == '.xlsx':
        from .spreadsheet_text import extract_xlsx
        return extract_xlsx(raw, source=p).text
    if p.suffix.lower() == '.pdf':
        from .pdf_text import extract_pdf
        return extract_pdf(raw)
    return decode_text(raw, p)


def read_text(root, relative):
    p, raw = _read_file(root, relative)
    return _decode_file(raw, p)


def _revision(info):
    return ':'.join(str(value) for value in (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))


def _require_revision(p, stream, expected):
    try:
        current = _revision(p.stat(follow_symlinks=False))
    except OSError:
        raise TextSourceChanged() from None
    if current != expected or _revision(os.fstat(stream.fileno())) != expected:
        raise TextSourceChanged()


def _excerpt(text, max_chars, max_lines):
    excerpt = ''.join(text[:max_chars].splitlines(keepends=True)[:max_lines])
    if excerpt.endswith('\r') and text[len(excerpt):len(excerpt) + 1] == '\n':
        return excerpt[:-1]
    return excerpt


def _plain_window(stream, info, offset, max_chars, max_lines):
    if offset > info.st_size:
        raise ValueError('Смещение выходит за пределы файла.')
    stream.seek(offset)
    raw = stream.read(4 * max_chars + len(codecs.BOM_UTF8))
    text = _decode_utf8(raw, first=not offset, final=offset + len(raw) >= info.st_size)
    excerpt = _excerpt(text, max_chars, max_lines)
    bom = len(codecs.BOM_UTF8) if not offset and raw.startswith(codecs.BOM_UTF8) else 0
    return excerpt, offset + bom + len(excerpt.encode('utf-8'))


def read_text_window(root, relative, *, offset=0, revision=None,
                     max_chars=MAX_WINDOW_CHARS, max_lines=MAX_WINDOW_LINES):
    """Read one display-sized window from a fixed version of a local text source."""
    if type(offset) is not int or offset < 0 or (offset and revision is None):
        raise ValueError('Для продолжения нужны смещение и версия файла.')
    if (type(max_chars) is not int or not 2 <= max_chars <= MAX_WINDOW_CHARS
            or type(max_lines) is not int or not 1 <= max_lines <= MAX_WINDOW_LINES):
        raise ValueError('Недопустимый размер фрагмента текста.')
    with _open_file(root, relative, revision=revision) as (p, stream, info):
        observed = _revision(info)
        if p.suffix.lower() in _DOCUMENT_SUFFIXES:
            body = _decode_file(_read_bounded(p, stream, info), p)
            total, unit = len(body), 'characters'
            if offset > total:
                raise ValueError('Смещение выходит за пределы текста документа.')
            text = _excerpt(body[offset:], max_chars, max_lines)
            stop = offset + len(text)
        else:
            total, unit = info.st_size, 'bytes'
            text, stop = _plain_window(stream, info, offset, max_chars, max_lines)
        _require_revision(p, stream, observed)
    return TextWindow(p, text, offset, stop if stop < total else None, observed, total, unit)


def read_summary_source(root, relative):
    """Return extractable document text or a bounded plain UTF-8 prefix."""
    with _open_file(root, relative) as (p, stream, info):
        if p.suffix.lower() in _DOCUMENT_SUFFIXES:
            body, partial = _decode_file(_read_bounded(p, stream, info), p), False
        else:
            raw = stream.read(MAX_FILE_BYTES)
            partial = len(raw) < info.st_size
            body = _decode_utf8(raw, final=not partial)
        _require_revision(p, stream, _revision(info))
    return body, partial


def _countable_text(root, relative):
    with _open_file(root, relative) as (p, stream, info):
        if p.suffix.lower() in _DOCUMENT_SUFFIXES:
            if text := _decode_file(_read_bounded(p, stream, info), p):
                yield text
        else:
            if info.st_size > MAX_TEXT_COUNT_BYTES:
                raise ValueError('Для полного подсчёта выбери текстовый файл до 64 МиБ.')
            decoder = codecs.getincrementaldecoder('utf-8-sig')()
            remaining = info.st_size
            try:
                while remaining:
                    raw = stream.read(min(remaining, 64 * 1024))
                    if not raw:
                        raise TextSourceChanged()
                    remaining -= len(raw)
                    if b'\0' in raw:
                        raise ValueError('Это двоичный файл. Подсчёт работает с текстом.')
                    if text := decoder.decode(raw, final=not remaining):
                        yield text
            except UnicodeDecodeError:
                raise ValueError('Файл не в UTF-8. Адаптер других кодировок пока не подключён.') from None
        _require_revision(p, stream, _revision(info))


def read_word_count(root, relative):
    """Count whitespace-separated words from one revision without loading a large file."""
    count, in_word = 0, False
    for text in _countable_text(root, relative):
        count += len(text.split()) - (in_word and not text[0].isspace())
        in_word = not text[-1].isspace()
    return count


def read_line_count(root, relative):
    """Count text lines as displayed by the file reader, including a final unterminated line."""
    count, last = 0, ''
    for text in _countable_text(root, relative):
        count += len(text.splitlines())
        if (last and last not in _LINE_BREAKS) or (last == '\r' and text[0] == '\n'):
            count -= 1  # The previous chunk already counted the shared line or CRLF.
        last = text[-1]
    return count


def read_workbook(root, relative, *, sheet=None):
    from .spreadsheet_text import extract_xlsx

    p, raw = _read_file(root, relative)
    return extract_xlsx(raw, source=p, sheet=sheet)
