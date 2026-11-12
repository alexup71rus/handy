"""Typed price-watch tools with native approval and durable bound receipts."""

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from .artifacts import ArtifactPage, save_artifact
from .schedule_actions import DIRECTIONS, cadence_label, price_label, start_worker
from .schedules import ScheduleConflict, Schedules, validate_schedule


_SETTINGS = frozenset({'url', 'interval_count', 'interval_unit', 'direction', 'title', 'xpath', 'currency'})
_CONFIG_FIELDS = ('url', 'interval', 'direction', 'title', 'xpath', 'currency')
_OPERATIONS = {
    'create': ('Создать наблюдение', 'Наблюдение создано'),
    'edit': ('Изменить наблюдение', 'Наблюдение изменено'),
    'enable': ('Включить наблюдение', 'Наблюдение включено'),
    'disable': ('Приостановить наблюдение', 'Наблюдение приостановлено'),
    'delete': ('Удалить наблюдение', 'Наблюдение удалено'),
}
_PLAN = re.compile(r'hsp_[0-9a-f]{24}')
_PLAN_SECONDS = 300
_LIST_REPLY_BYTES = 55 * 1024


def _condition(settings):
    """A visible, literal description of what the prepared watcher can do."""
    direction = settings['direction']
    return {'kind': 'public_price_change', 'direction': direction, 'label': DIRECTIONS[direction],
            'numeric_threshold': None, 'notification_channel': 'desktop',
            'authenticated_browser': False}


def _workspace(root):
    return hashlib.sha256(str(root).encode()).hexdigest()[:24]


@contextmanager
def _database(app):
    db = sqlite3.connect(app.db, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS schedule_tool_plans(
            id TEXT PRIMARY KEY, session TEXT NOT NULL, root TEXT NOT NULL, workspace TEXT NOT NULL,
            operation TEXT NOT NULL, job_id TEXT NOT NULL, revision INTEGER NOT NULL,
            settings TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
            started REAL, receipt TEXT
        )''')
        yield db
    finally:
        db.close()


def _time_label(value):
    return datetime.fromtimestamp(value, UTC).strftime('%d.%m.%Y %H:%M') if value is not None else '—'


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def _text_preview(value, limit):
    if value is None or _json_size(value) <= limit:
        return value
    remaining = limit - _json_size('…')
    length = 0
    for char in value:
        remaining -= _json_size(char) - 2
        if remaining < 0:
            break
        length += 1
    return value[:length] + '…'


def _bound_list_reply(reply):
    if _json_size(reply) <= _LIST_REPLY_BYTES:
        return
    data = reply['data']
    full_rows = data['jobs']
    fields = ('title', 'host', 'error')
    data['details_truncated'] = True
    reply['text'] += ' Показаны все задачи. Длинные текстовые поля сокращены.'
    data['jobs'] = [{**row, **dict.fromkeys(fields)} for row in full_rows]
    budget = (_LIST_REPLY_BYTES - _json_size(reply)) // (len(full_rows) * len(fields))
    for row, original in zip(data['jobs'], full_rows):
        row.update({field: _text_preview(original[field], budget) for field in fields})


def list_schedules(app, root, session, *, artifact=False):
    if type(artifact) is not bool:
        raise ValueError('artifact должен быть true или false.')
    schedules = Schedules(app.state_dir)
    jobs = schedules.list(_workspace(root))
    worker_running = schedules.worker_status()['running']
    enabled_state = 'Включена' if worker_running else 'Проверки остановлены. Задача включена в расписании.'
    rows = [{
        'id': job['id'], 'revision': job['revision'], 'title': job['title'],
        'host': urlsplit(job['url']).hostname, 'enabled': job['enabled'],
        'state': enabled_state if job['enabled'] else 'На паузе', 'interval': cadence_label(job['interval']),
        'direction': DIRECTIONS[job['direction']], 'price': price_label(job['last_price'], job['last_currency']),
        'next': _time_label(job['next_run']) if worker_running and job['enabled'] else '—',
        'error': job['last_error'],
    } for job in jobs]
    data = {'jobs': rows, 'worker_running': worker_running, 'partial': False}
    text = 'В этой рабочей папке пока нет запланированных задач.' if not rows else f'В расписании задач — {len(rows)}.'
    if any(row['enabled'] for row in rows) and not worker_running:
        text += ' Фоновый исполнитель сейчас не работает.'
    if artifact:
        data['artifact'] = save_artifact(ArtifactPage('schedules', {'title': 'Запланированные задачи', **data}), root)
        text += ' Снимок расписания сохранён.'
    reply = {'status': 'ok', 'text': text, 'data': data}
    _bound_list_reply(reply)
    return reply


def _arguments(options):
    if not isinstance(options, dict):
        raise ValueError('Параметры расписания должны быть объектом.')
    operation = options.get('operation')
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise ValueError('Выбери create, edit, enable, disable или delete.')
    allowed = {'operation', 'job_id'} | (_SETTINGS if operation in {'create', 'edit'} else set())
    if set(options) - allowed:
        raise ValueError('В запросе есть неподдерживаемые параметры расписания.')
    identifier = options.get('job_id', '')
    if operation == 'create':
        if 'job_id' in options:
            raise ValueError('Для нового наблюдения job_id создаётся автоматически.')
    elif not isinstance(identifier, str) or re.fullmatch(r'[0-9a-f]{12}', identifier) is None:
        raise ValueError('Укажи job_id из списка задач этой рабочей папки.')
    settings = {key: value for key, value in options.items() if key in _SETTINGS}
    interval_keys = {'interval_count', 'interval_unit'} & settings.keys()
    if operation == 'create' and not {'url', 'interval_count', 'interval_unit'} <= settings.keys():
        raise ValueError('Для нового наблюдения нужны url, interval_count и interval_unit.')
    if interval_keys:
        if len(interval_keys) != 2 or type(settings['interval_count']) is not int or not isinstance(settings['interval_unit'], str):
            raise ValueError('Интервал задаётся целым interval_count и строкой interval_unit вместе.')
        settings['interval'] = {'count': settings.pop('interval_count'), 'unit': settings.pop('interval_unit')}
    if operation == 'edit' and not settings:
        raise ValueError('Укажи хотя бы один параметр, который нужно изменить.')
    return operation, identifier, settings


def prepare_change(app, root, session, options):
    operation, job_id, settings = _arguments(options)
    schedules, workspace = Schedules(app.state_dir), _workspace(root)
    if operation == 'create':
        settings = validate_schedule(**{'direction': 'any', **settings})
        job_id, revision = secrets.token_hex(6), 0
        preview_settings = {**settings, 'enabled': True}
    else:
        current = schedules.get(workspace, job_id)
        if current is None:
            raise ScheduleConflict('Этой задачи нет в рабочей папке чата. Посмотри список и проверь ID.')
        revision = current['revision']
        preview_settings = {key: current[key] for key in (*_CONFIG_FIELDS, 'enabled')}
        if operation == 'edit':
            updated = validate_schedule(**{key: settings.get(key, current[key]) for key in _CONFIG_FIELDS})
            settings = {key: updated[key] for key in settings}
        elif operation in {'enable', 'disable'}:
            settings = {'enabled': operation == 'enable'}
        preview_settings.update(settings)
    plan_id, created = 'hsp_' + secrets.token_hex(12), time.time()
    with _database(app) as db:
        db.execute(
            "INSERT INTO schedule_tool_plans(id,session,root,workspace,operation,job_id,revision,settings,status,created,expires) "
            "VALUES(?,?,?,?,?,?,?,?,'pending',?,?)",
            (plan_id, session, str(root), workspace, operation, job_id, revision,
             json.dumps(settings, ensure_ascii=False), created, created + _PLAN_SECONDS),
        )
    preview = {'operation': operation, 'job_id': job_id, 'root': str(root), 'settings': preview_settings,
               'condition': _condition(preview_settings)}
    return {'status': 'approval_required', 'text': _OPERATIONS[operation][0] + '. Нужно подтверждение.',
            'data': {'plan_id': plan_id, 'operation': operation, 'preview': preview, 'expires_at': created + _PLAN_SECONDS}}


def _unknown(plan_id):
    return {'status': 'error', 'text': 'Не удалось подтвердить результат изменения. Проверь список задач перед повтором.',
            'data': {'plan_id': plan_id, 'code': 'outcome_unknown', 'performed': None}}


def _apply(schedules, plan):
    operation, workspace, job_id = plan['operation'], plan['workspace'], plan['job_id']
    settings = json.loads(plan['settings'])
    if operation == 'create':
        job = schedules.create(workspace, job_id=job_id, **settings)
    elif operation == 'delete':
        job = schedules.delete(workspace, job_id, plan['revision'])
    else:
        job = schedules.edit(workspace, job_id, plan['revision'], **settings)
    try:
        worker = start_worker(schedules) if operation != 'delete' and job['enabled'] else schedules.worker_status()
    except (OSError, ValueError, sqlite3.Error):
        worker = {'running': None}
    receipt = {'plan_id': plan['id'], 'operation': operation, 'status': 'succeeded',
               'job': job, 'worker': worker, 'started_at': plan['started'], 'finished_at': time.time()}
    text = f"{_OPERATIONS[operation][1]}. ID — {job_id}."
    if operation != 'delete' and job['enabled']:
        if worker['running'] is False:
            text += ' Задача сохранена. Фоновый исполнитель сейчас не работает.'
        elif worker['running'] is None:
            text += ' Задача сохранена. Состояние фонового исполнителя не подтверждено.'
    return {'status': 'ok', 'text': text, 'data': {'plan_id': plan['id'], 'receipt': receipt, 'job': job, 'worker': worker,
                                                'condition': _condition(job)}}


def finish_change(app, root, session, plan_id, *, approved):
    if not isinstance(plan_id, str) or _PLAN.fullmatch(plan_id) is None or type(approved) is not bool:
        raise ValueError('Нужен корректный plan_id и явное решение approved.')
    with _database(app) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            plan = db.execute('SELECT * FROM schedule_tool_plans WHERE id=? AND session=? AND root=?',
                              (plan_id, session, str(root))).fetchone()
            if plan is None:
                raise PermissionError('Подтверждение относится к другой сессии или рабочей папке.')
            if plan['receipt']:
                db.rollback()
                return json.loads(plan['receipt'])
            if plan['status'] == 'running':
                db.rollback()
                return _unknown(plan_id)
            result = None
            if not approved:
                result = {'status': 'ok',
                          'text': 'Пользователь отклонил изменение расписания. Не повторяй его без новой просьбы.',
                          'data': {'plan_id': plan_id, 'cancelled': True, 'performed': False, 'reason': 'user_declined'}}
            elif plan['expires'] < time.time():
                result = {'status': 'error', 'text': 'Подтверждение устарело. Запроси изменение заново.',
                          'data': {'plan_id': plan_id, 'code': 'expired', 'performed': False}}
            if result is not None:
                db.execute("UPDATE schedule_tool_plans SET status='finished',receipt=? WHERE id=?",
                           (json.dumps(result, ensure_ascii=False), plan_id))
                db.commit()
                return result
            started = time.time()
            db.execute("UPDATE schedule_tool_plans SET status='running',started=? WHERE id=?", (started, plan_id))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        try:
            result = _apply(Schedules(app.state_dir), {**dict(plan), 'started': started})
        except ScheduleConflict as exc:
            result = {'status': 'error', 'text': str(exc),
                      'data': {'plan_id': plan_id, 'code': 'schedule_conflict', 'performed': False}}
        except ValueError as exc:
            result = {'status': 'error', 'text': str(exc),
                      'data': {'plan_id': plan_id, 'code': 'invalid_schedule', 'performed': False}}
        except (OSError, sqlite3.Error):
            result = _unknown(plan_id)
        db.execute("UPDATE schedule_tool_plans SET status='finished',receipt=? WHERE id=?",
                   (json.dumps(result, ensure_ascii=False), plan_id))
        return result
