"""Create validated template artifacts from explicit tool arguments."""

from pathlib import Path

from .artifact_data import chart_page, clock_page, list_page, read_display_table, table_page
from .artifacts import ArtifactPage, project_root, save_artifact
from .clock_zones import resolve_clock_zone
from .constraint_execution import filter_table, validate_constraints
from .file_access import MAX_FILE_BYTES, is_private, read_text, read_workbook, safe_path
from .tool_contract import argument_feedback, selected_contract


MAX_SECONDS = 24 * 60 * 60

_TITLES = {
    'timer': 'Таймер', 'pomodoro': 'Pomodoro', 'clock': 'Часы',
    'dice': 'Бросок кубиков', 'password': 'Генератор паролей',
    'chart': 'График', 'table': 'Таблица', 'list': 'Список',
}
_DATA_OPTIONS = {'path', 'data', 'format'}
_UTILITY_KINDS = ('timer', 'pomodoro', 'clock', 'dice', 'password')
_OPTIONS = {
    **{kind: set(selected_contract('handy_artifact', {'kind': kind})['properties']['options']['properties']) - {'title'}
       for kind in _UTILITY_KINDS},
    'chart': _DATA_OPTIONS | {'x', 'y', 'chart_type', 'sheet', 'where'},
    'table': _DATA_OPTIONS | {'sheet', 'density', 'where'},
    'list': _DATA_OPTIONS,
}
_PASSWORD_GROUPS = ('lowercase', 'uppercase', 'digits', 'symbols')


def _integer(options, name, default, minimum, maximum):
    value = options.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'Для {name} укажи целое число от {minimum} до {maximum}.')
    return value


def _text(options, name, default=None, *, limit=160):
    value = options.get(name, default)
    if value is None and name not in options:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError(f'Для {name} нужна непустая строка длиной до {limit} символов.')
    return value.strip()


def _choice(options, name, default, choices):
    value = options.get(name, default)
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f'Для {name} выбери одно значение из {", ".join(choices)}.')
    return value


def _source(root, kind, options):
    if ('path' in options) == ('data' in options):
        raise ValueError('Укажи ровно один источник — path или data.')
    workbook = None
    source = None
    if 'path' in options:
        relative = _text(options, 'path', limit=4096)
        if relative is None or is_private(relative):
            raise ValueError('Выбери файл проекта вне папок с секретами.')
        source = safe_path(root, relative)
        if is_private(source):
            raise ValueError('Выбери файл проекта вне папок с секретами.')
        suffix = source.suffix.lower().removeprefix('.')
        suffix = 'text' if suffix == 'txt' else suffix
        if 'format' in options and options['format'] != suffix:
            raise ValueError('format должен совпадать с форматом файла.')
        allowed = {'json', 'text'} if kind == 'list' else {'csv', 'tsv', 'json', 'xlsx'}
        if suffix not in allowed:
            raise ValueError('Выбери поддерживаемый формат источника.')
        if suffix == 'xlsx':
            workbook = read_workbook(root, relative, sheet=_text(options, 'sheet'))
            raw = workbook.text
        else:
            raw = read_text(root, relative)
    else:
        raw = options['data']
        if not isinstance(raw, str):
            raise ValueError('В data передай текст CSV, TSV, JSON или пунктов списка.')
        allowed = ('json', 'text') if kind == 'list' else ('csv', 'tsv', 'json')
        suffix = _choice(options, 'format', None, allowed)
    if 'sheet' in options and suffix != 'xlsx':
        raise ValueError('Выбор листа доступен только для файла XLSX.')
    if len(raw.encode('utf-8')) > MAX_FILE_BYTES:
        raise ValueError('Источник больше 256 КиБ. Выбери меньший набор данных.')
    description = f'Источник — {source.name}' if source else 'Данные из сообщения'
    if workbook:
        description += f' · лист {workbook.sheet}'
        if workbook.formula_cells:
            if kind == 'chart' or options.get('where'):
                raise ValueError('На листе есть формулы. Для графика или фильтра сохрани значения либо CSV.')
            description += f' · формул без вычисления — {workbook.formula_cells}'
    return raw, suffix, description, source


def _where(options):
    clauses = options.get('where', [])
    if not isinstance(clauses, list) or len(clauses) > 4:
        raise ValueError('В where передай список не более четырёх условий.')
    result = []
    types = {str: 'string', int: 'number', float: 'number', bool: 'boolean', type(None): 'null'}
    for clause in clauses:
        if not isinstance(clause, dict) or set(clause) - {'column', 'operator', 'value', 'value_type'}:
            raise ValueError('Условие where содержит неподдерживаемые параметры.')
        result.append({**clause, 'value_type': clause.get('value_type', types.get(type(clause.get('value'))))})
    validate_constraints({'where': result})
    return result


def _dataset(root, kind, options):
    raw, suffix, description, source = _source(root, kind, options)
    title = _text(options, 'title', source.stem if source else _TITLES[kind])
    if kind == 'list':
        data = {**list_page(raw, suffix, title), 'description': description}
        return data, {'items': len(data['items']), 'sources': [str(source)] if source else []}
    table = read_display_table(raw, suffix, json_envelope=True)
    where = _where(options)
    if where:
        table = filter_table(table, {'where': where})
        description += ' · с учётом условий для строк'
    if kind == 'chart':
        chart_type = _choice(options, 'chart_type', 'line', ('line', 'bar', 'donut'))
        data = chart_page(table, title, _text(options, 'x'), _text(options, 'y'), description,
                          chart_type=chart_type)
        summary = {'points': len(data['points']), 'chart_type': chart_type}
    else:
        data = table_page(table, title, description)
        data['density'] = _choice(options, 'density', 'comfortable', ('comfortable', 'compact'))
        summary = {'rows': len(data['rows']), 'columns': len(data['columns'])}
    return data, {**summary, 'where': where, 'sources': [str(source)] if source else []}


def _page(root, kind, options):
    title = _text(options, 'title', _TITLES[kind])
    if kind == 'timer':
        data = {'title': title, 'mode': 'countdown',
                'durationSeconds': _integer(options, 'seconds', None, 1, MAX_SECONDS)}
    elif kind == 'pomodoro':
        data = {'title': title, 'mode': 'pomodoro',
                'focusMinutes': _integer(options, 'focus_minutes', 25, 1, 120),
                'shortBreakMinutes': _integer(options, 'short_break_minutes', 5, 1, 60),
                'longBreakMinutes': _integer(options, 'long_break_minutes', 15, 1, 60),
                'rounds': _integer(options, 'rounds', 4, 1, 12)}
    elif kind == 'clock':
        value = _text(options, 'time_zone', limit=180)
        zone = resolve_clock_zone(value) if value is not None else None
        if zone is not None and zone.error:
            raise ValueError(zone.error)
        data = clock_page(title, zone.time_zone if zone else None)
    elif kind == 'dice':
        data = {'title': title, 'count': _integer(options, 'count', 1, 1, 20),
                'sides': _integer(options, 'sides', 6, 2, 1000)}
    elif kind == 'password':
        groups = options.get('groups', list(_PASSWORD_GROUPS))
        if (not isinstance(groups, list) or not groups or any(not isinstance(group, str) for group in groups)
                or len(set(groups)) != len(groups) or not set(groups) <= set(_PASSWORD_GROUPS)):
            raise ValueError('Для groups выбери неповторяющиеся наборы lowercase, uppercase, digits, symbols.')
        data = {'title': title, 'length': _integer(options, 'length', 24, 4, 128), 'groups': groups}
    else:
        return _dataset(root, kind, options)
    return data, {'settings': {key: value for key, value in data.items() if key != 'title'}}


def create_artifact(app, root: Path, session: str, kind: str, options: dict) -> dict:
    """Return a receipt only after both files were atomically published."""
    if isinstance(kind, str) and kind in _UTILITY_KINDS:
        feedback = argument_feedback('handy_artifact', {'kind': kind, 'options': options})
        if feedback:
            feedback['data']['code'] = 'artifact_input'
            return feedback
    try:
        if not isinstance(kind, str) or kind not in _OPTIONS:
            raise ValueError('Выбери timer, pomodoro, clock, dice, password, chart, table или list.')
        if not isinstance(options, dict) or set(options) - _OPTIONS[kind] - {'title'}:
            raise ValueError('Параметры не соответствуют выбранному виду артефакта.')
        directory = Path(root).resolve(strict=True)
        root = project_root(directory)
        if 'path' in options:
            path = _text(options, 'path', limit=4096)
            options = {**options, 'path': str(directory / path)}
        data, summary = _page(root, kind, options)
    except (ValueError, OSError, UnicodeError) as error:
        return {'status': 'needs_input', 'text': str(error), 'data': {'code': 'artifact_input'}}
    try:
        artifact = save_artifact(ArtifactPage(kind, data), root)
    except (ValueError, OSError, UnicodeError) as error:
        return {'status': 'error', 'text': 'Не удалось сохранить артефакт. ' + str(error),
                'data': {'code': 'artifact_write_failed'}}
    return {'status': 'ok', 'text': 'Артефакт создан.', 'data': {'artifact': artifact, **summary}}
