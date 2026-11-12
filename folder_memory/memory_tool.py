"""Confirmed personal and project memory, independent of a chat client."""

from contextlib import closing, contextmanager
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
import unicodedata

from .evidence_text import redact_text
from .file_access import is_private, require_user_path
from .personal_memory import MemoryConflictError, PersonalMemory, _clean, _key, evidence_values
from .storage import Store, digest


_PLAN = re.compile(r'hmp_[0-9a-f]{24}')
_PLAN_SECONDS = 300
_CURRENT_USER = frozenset({'пользователь', 'я', 'мой профиль', 'user', 'current user', 'me', 'myself'})
_PROFILE_FIELDS = frozenset({
    'имя', 'name', 'как обращаться', 'обращение', 'preferred name',
    'язык', 'язык ответов', 'предпочитаемый язык', 'language', 'response language',
    'часовой пояс', 'timezone', 'time zone',
    'стиль ответов', 'стиль общения', 'response style', 'answer style', 'предпочтения ответов',
})


def project_memory_root(root):
    directory = require_user_path(root)
    if not directory.is_dir() or is_private(directory):
        raise ValueError('Выбери рабочую папку проекта вне папок с секретами.')
    for candidate in (directory, *directory.parents):
        marker = candidate / '.git'
        if marker.is_dir() or marker.is_file():
            directory = candidate
            break
    if directory == Path.home().resolve() or is_private(directory):
        raise ValueError('Для проектной памяти выбери папку или рабочую копию проекта.')
    return directory


def _project_key(title):
    return ' '.join(unicodedata.normalize('NFKC', title).casefold().replace('ё', 'е').split()).rstrip(' .!?')


@contextmanager
def _database(app):
    store = Store(app.db)
    try:
        db = store.db
        PersonalMemory(db)
        db.execute('''CREATE TABLE IF NOT EXISTS memory_tool_plans(
            id TEXT PRIMARY KEY, session TEXT NOT NULL, root TEXT NOT NULL,
            proposal TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, receipt TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS memory_chat_usage(
            fact_id INTEGER PRIMARY KEY REFERENCES personal_facts(id),
            enabled INTEGER NOT NULL, version INTEGER NOT NULL
        )''')
        db.commit()
        yield db
    finally:
        store.close()


def _usage(db, fact_id):
    if fact_id is None:
        return {'enabled': False, 'version': 0}
    row = db.execute('SELECT enabled,version FROM memory_chat_usage WHERE fact_id=?', (fact_id,)).fetchone()
    return {'enabled': bool(row['enabled']), 'version': row['version']} if row else {'enabled': False, 'version': 0}


def _profile_allowed(subject, field, value):
    return (_key(subject) in _CURRENT_USER and _key(field) in _PROFILE_FIELDS
            and len(value) <= 400 and redact_text(value) == value)


def _project_provenance(value, evidence):
    """Separate assistant notes from literal facts without changing storage."""
    try:
        context = json.loads(evidence)
    except (ValueError, TypeError, RecursionError):
        context = None
    if isinstance(context, dict) and context.get('kind') == 'assistant-summary':
        sources = context.get('sources')
        if set(context) != {'kind', 'sources'} or not isinstance(sources, list) or not 1 <= len(sources) <= 3:
            raise ValueError('Для заметки проекта нужны реальные исходные фрагменты.')
        sources = [_clean(span, 'source', 2000, multiline=True) for span in sources]
        return 'assistant-summary', sources
    if not any(' '.join(value.split()) in ' '.join(span.split()) for span in evidence_values(evidence)):
        raise ValueError('Сохраняемый текст должен присутствовать в сообщении пользователя.')
    return 'literal-source', []


def _prepared(db, root, options):
    scope = options.get('scope')
    if scope == 'personal':
        if set(options) - {'scope', 'subject', 'field', 'value', 'evidence', 'use_in_chat'}:
            raise ValueError('Для личной памяти используй subject, field, value, evidence и при необходимости use_in_chat.')
        proposal = PersonalMemory(db).prepare(options.get('subject'), options.get('field'),
                                               options.get('value'), options.get('evidence'))
        usage = _usage(db, proposal['expected_id'])
        enabled = options.get('use_in_chat', usage['enabled'])
        if type(enabled) is not bool:
            raise ValueError('use_in_chat должен быть true или false.')
        if enabled and not _profile_allowed(proposal['subject'], proposal['field'], proposal['value']):
            raise ValueError('В обычный контекст можно включать только имя, язык, часовой пояс или стиль ответов текущего пользователя.')
        proposal.update(use_in_chat=enabled, expected_usage=usage)
        prior = proposal['prior']
        preview = {'scope': scope, 'subject': proposal['subject'], 'field': proposal['field'],
                   'value': proposal['value'], 'previous_value': prior['value'] if prior else None,
                   'use_in_chat': enabled, 'previous_use_in_chat': usage['enabled']}
    elif scope == 'project':
        if set(options) - {'scope', 'title', 'body', 'evidence'}:
            raise ValueError('Для проектной памяти используй title, body и evidence.')
        title = _clean(options.get('title'), 'title', 96)
        value = _clean(options.get('body'), 'body', 2000, multiline=True)
        evidence = _clean(options.get('evidence'), 'evidence', 16000, multiline=True)
        source, excerpts = _project_provenance(value, evidence)
        project = project_memory_root(root)
        workspace, key = digest(str(project))[:24], _project_key(title)
        row = db.execute('SELECT id,version,title,body FROM project_knowledge WHERE workspace=? AND key=?',
                         (workspace, key)).fetchone()
        proposal = {'scope': scope, 'root': str(project), 'workspace': workspace, 'key': key,
                    'title': title, 'body': value, 'evidence': evidence, 'source': source,
                    'expected_id': row['id'] if row else None,
                    'expected_version': row['version'] if row else None}
        preview = {'scope': scope, 'root': str(project), 'title': title, 'body': value,
                   'previous_body': row['body'] if row else None}
        if source == 'assistant-summary':
            preview.update(source=source, note='Заметка составлена ассистентом; её выводы не проверены автоматически.',
                           source_excerpts=excerpts)
    else:
        raise ValueError('Уточни область памяти — personal для личного факта или project для текущего проекта.')
    return proposal, preview


def prepare_memory(app, root, session, options):
    if not isinstance(options, dict):
        raise ValueError('Параметры памяти должны быть объектом.')
    with _database(app) as db:
        proposal, preview = _prepared(db, root, options)
        plan_id, now = 'hmp_' + secrets.token_hex(12), time.time()
        db.execute('INSERT INTO memory_tool_plans(id,session,root,proposal,created,expires) VALUES(?,?,?,?,?,?)',
                   (plan_id, session, str(root), json.dumps(proposal, ensure_ascii=False), now, now + _PLAN_SECONDS))
        db.commit()
    return {'status': 'approval_required', 'text': 'Сохранить эту запись в памяти? Нужно подтверждение.',
            'data': {'plan_id': plan_id, 'preview': preview, 'expires_at': now + _PLAN_SECONDS}}


def _save_project(db, proposal):
    current = db.execute('SELECT id,version,title,body FROM project_knowledge WHERE workspace=? AND key=?',
                         (proposal['workspace'], proposal['key'])).fetchone()
    revision = (current['id'], current['version']) if current else (None, None)
    if revision != (proposal['expected_id'], proposal['expected_version']):
        raise MemoryConflictError('Запись проекта изменилась. Нужно новое подтверждение.')
    db.execute('INSERT OR IGNORE INTO workspaces(id,root) VALUES(?,?)', (proposal['workspace'], proposal['root']))
    if current is None:
        cursor = db.execute('INSERT INTO project_knowledge(workspace,key,title,body,version) VALUES(?,?,?,?,1)',
                            (proposal['workspace'], proposal['key'], proposal['title'], proposal['body']))
        record_id, version = cursor.lastrowid, 1
    elif (current['title'], current['body']) == (proposal['title'], proposal['body']):
        record_id, version = current['id'], current['version']
    else:
        record_id, version = current['id'], current['version'] + 1
        db.execute('UPDATE project_knowledge SET title=?,body=?,version=?,encoder=NULL,vector=NULL WHERE id=?',
                   (proposal['title'], proposal['body'], version, record_id))
    return {'scope': 'project', 'id': record_id, 'version': version, 'root': proposal['root'],
            'title': proposal['title'], 'body': proposal['body'], 'source': proposal['source']}


def _save_personal(db, proposal, plan_id):
    usage = _usage(db, proposal['expected_id'])
    if usage != proposal['expected_usage']:
        raise MemoryConflictError('Настройка профиля изменилась. Нужно новое подтверждение.')
    record = PersonalMemory(db).save(proposal, plan_id)
    if proposal['use_in_chat'] != usage['enabled']:
        db.execute('INSERT INTO memory_chat_usage(fact_id,enabled,version) VALUES(?,?,?) '
                   'ON CONFLICT(fact_id) DO UPDATE SET enabled=excluded.enabled,version=excluded.version',
                   (record['id'], int(proposal['use_in_chat']), usage['version'] + 1))
    return {key: record[key] for key in ('scope', 'id', 'version', 'subject', 'field', 'value')} | {
        'use_in_chat': proposal['use_in_chat']}


def finish_memory(app, root, session, plan_id, *, approved):
    if not isinstance(plan_id, str) or _PLAN.fullmatch(plan_id) is None or type(approved) is not bool:
        raise ValueError('Нужен корректный plan_id и решение approved.')
    with _database(app) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            plan = db.execute('SELECT * FROM memory_tool_plans WHERE id=? AND session=? AND root=?',
                              (plan_id, session, str(root))).fetchone()
            if plan is None:
                raise PermissionError('Подтверждение относится к другой сессии или рабочей папке.')
            if plan['receipt']:
                db.rollback()
                return json.loads(plan['receipt'])
            if not approved:
                result = {'status': 'ok',
                          'text': 'Пользователь отклонил сохранение. Запись не сохранена. Не повторяй её без новой просьбы.',
                          'data': {'plan_id': plan_id, 'cancelled': True, 'performed': False, 'reason': 'user_declined'}}
            elif plan['expires'] < time.time():
                result = {'status': 'error', 'text': 'Подтверждение устарело. Запроси сохранение заново.',
                          'data': {'plan_id': plan_id, 'code': 'expired', 'performed': False}}
            else:
                proposal = json.loads(plan['proposal'])
                try:
                    record = (_save_personal(db, proposal, plan_id) if proposal['scope'] == 'personal'
                              else _save_project(db, proposal))
                    result = {'status': 'ok', 'text': 'Запись сохранена в личной памяти.' if proposal['scope'] == 'personal'
                              else 'Заметка сохранена в памяти проекта.' if proposal['source'] == 'assistant-summary'
                              else 'Запись сохранена в памяти проекта.',
                              'data': {'plan_id': plan_id, 'record': record, 'performed': True}}
                except MemoryConflictError as error:
                    result = {'status': 'error', 'text': str(error),
                              'data': {'plan_id': plan_id, 'code': 'memory_conflict', 'performed': False}}
            db.execute('UPDATE memory_tool_plans SET receipt=? WHERE id=?', (json.dumps(result, ensure_ascii=False), plan_id))
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise


def _readonly(app):
    connection = sqlite3.connect(Path(app.db).resolve().as_uri() + '?mode=ro', uri=True, timeout=.25)
    connection.row_factory = sqlite3.Row
    connection.create_function('memory_casefold', 1, str.casefold, deterministic=True)
    return connection


def _search_words(text):
    return set(re.findall(r'[^\W_]+', _key(unicodedata.normalize('NFKC', text)), re.UNICODE))


def _personal_candidates(db, query, field, limit):
    query = _clean(query, 'query', 240)
    terms = _search_words(query)
    if not terms:
        raise ValueError('В query нужны слова или числа для поиска в памяти.')
    if not isinstance(field, str) or len(field) > 160:
        raise ValueError('field должен быть строкой длиной до 160 символов.')
    field = _clean(field, 'field', 160) if field.strip() else ''
    db.create_function('memory_terms_match', 3,
                       lambda subject, field, value: int(terms <= _search_words(' '.join((subject, field, value)))),
                       deterministic=True)
    sql = ("SELECT id,version,subject,field,value FROM personal_facts WHERE source='user-confirmed' "
           'AND memory_terms_match(subject,field,value)=1')
    args = []
    if field:
        sql += ' AND field_key=?'
        args.append(_key(field))
    try:
        rows = db.execute(sql + ' ORDER BY updated DESC,id DESC LIMIT ?', (*args, limit + 1)).fetchall()
    except sqlite3.OperationalError as error:
        if 'no such table' not in str(error):
            raise
        rows = []
    truncated = len(rows) > limit
    candidates = [{'scope': 'personal', 'source': 'user-confirmed', **dict(row),
                   'matched_fields': {name: sorted(terms & _search_words(row[name]))
                                      for name in ('subject', 'field', 'value')
                                      if terms & _search_words(row[name])}}
                  for row in rows[:limit]]
    ambiguous = len({_key(row['subject']) for row in rows}) > 1
    text = ('Найдено несколько субъектов. Выбери нужное сохранённое имя и поле перед точным чтением.' if ambiguous
            else 'Найдено больше записей, чем показано. Уточни запрос или поле.' if truncated
            else 'Найдены кандидаты по словам запроса. Это совпадение текста, а не подтверждение тождества субъекта.'
            if candidates else 'По этим словам подтверждённых записей памяти пока нет.')
    return {'status': 'needs_input' if ambiguous or truncated else 'ok', 'text': text,
            'data': {'scope': 'personal', 'records': [], 'candidates': candidates,
                     'match': {'mode': 'all_word_terms', 'query': query, 'terms': sorted(terms),
                               'field_filter': field or None},
                     'has_more': truncated, 'ambiguous': ambiguous}}


def project_note_source(db, record_id, version):
    """Read the saved authorship for one exact project-note revision."""
    # Committed receipts identify authorship for this exact record revision.
    try:
        metadata = db.execute("""SELECT proposal FROM memory_tool_plans
            WHERE json_extract(receipt,'$.data.record.scope')='project'
              AND json_extract(receipt,'$.data.record.id')=?
              AND json_extract(receipt,'$.data.record.version')=?
            ORDER BY rowid DESC LIMIT 1""", (record_id, version)).fetchone()
    except sqlite3.OperationalError:
        metadata = None
    if metadata:
        source = json.loads(metadata['proposal']).get('source')
        if source in {'literal-source', 'assistant-summary'}:
            return source
    return None


def _project_record(db, root, row):
    record = {'scope': 'project', 'root': str(root), **dict(row)}
    if source := project_note_source(db, row['id'], row['version']):
        record['source'] = source
    return record


def recall_memory(app, root, session, options):
    if not isinstance(options, dict) or options.get('scope') not in {'personal', 'project'}:
        raise ValueError('Для чтения памяти укажи scope personal или project.')
    scope = options['scope']
    allowed = {'scope', 'subject', 'query', 'field', 'limit'} if scope == 'personal' else {'scope', 'title', 'query', 'limit'}
    if set(options) - allowed:
        raise ValueError('Укажи параметры, относящиеся к выбранной области памяти.')
    limit = options.get('limit', 5)
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('limit должен быть целым числом от 1 до 10.')
    with closing(_readonly(app)) as db:
        try:
            if scope == 'personal':
                if 'query' in options:
                    if 'subject' in options:
                        raise ValueError('Укажи subject для точного чтения или query для поиска кандидатов.')
                    return _personal_candidates(db, options['query'], options.get('field', ''), limit)
                memory = PersonalMemory(db, initialize=False)
                rows = memory.recall(options.get('subject'), options.get('field', ''), limit=limit)
                records = [{key: row[key] for key in ('scope', 'id', 'version', 'subject', 'field', 'value')}
                           for row in rows if row['source'] == 'user-confirmed']
            else:
                directory = project_memory_root(root)
                title = options.get('title')
                query = options.get('query')
                if title is not None and query is not None:
                    raise ValueError('Укажи title или query для проектной памяти.')
                sql = 'SELECT id,version,title,body FROM project_knowledge WHERE workspace=?'
                args = [digest(str(directory))[:24]]
                if title is not None:
                    sql += ' AND key=?'
                    args.append(_project_key(_clean(title, 'title', 96)))
                elif query is not None:
                    query = _clean(query, 'query', 240)
                    sql += ' AND (instr(memory_casefold(title),?)>0 OR instr(memory_casefold(body),?)>0)'
                    args.extend((query.casefold(), query.casefold()))
                rows = db.execute(sql + ' ORDER BY id DESC LIMIT ?', (*args, limit)).fetchall()
                records = [_project_record(db, directory, row) for row in rows]
        except sqlite3.OperationalError as error:
            if 'no such table' not in str(error):
                raise
            records = []
    return {'status': 'ok', 'text': 'Найдены подтверждённые записи памяти.' if records else 'Подходящих записей памяти пока нет.',
            'data': {'scope': scope, 'records': records}}


def profile_memory(app):
    """Return only confirmed user profile values explicitly approved for ordinary chat."""
    try:
        with closing(_readonly(app)) as db:
            rows = db.execute('''SELECT p.subject,p.field,p.value FROM personal_facts p
                JOIN memory_chat_usage u ON u.fact_id=p.id
                WHERE p.source='user-confirmed' AND u.enabled=1 ORDER BY p.updated DESC,p.id DESC LIMIT 12''').fetchall()
        return [{'source': 'Подтверждённый профиль пользователя', **dict(row)} for row in rows
                if _profile_allowed(row['subject'], row['field'], row['value'])][:4]
    except (OSError, sqlite3.Error):
        return []
