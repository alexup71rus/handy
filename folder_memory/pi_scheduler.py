"""Durable prompt schedules: each execution is a separate native pi session."""

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .runtime import lock_held, process_lock, runtime_dir, write_json


RUN_TIMEOUT = 900
MAX_OUTPUT = 8 * 1024 * 1024


@contextmanager
def _database(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / 'pi-scheduler.sqlite3'
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    try:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL,
                cwd TEXT NOT NULL, run_at REAL NOT NULL, interval_seconds REAL,
                next_run REAL, enabled INTEGER NOT NULL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, title TEXT NOT NULL,
                cwd TEXT NOT NULL, session_path TEXT NOT NULL, status TEXT NOT NULL,
                started_at REAL NOT NULL, finished_at REAL, answer TEXT NOT NULL DEFAULT '',
                error TEXT, notified INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS jobs_due ON jobs(enabled, next_run);
            CREATE INDEX IF NOT EXISTS runs_recent ON runs(started_at);
        ''')
        yield db
    finally:
        db.close()


def _record(row):
    record = dict(row)
    for field in ('enabled', 'notified'):
        if field in record:
            record[field] = bool(record[field])
    if 'finished_at' in record:
        record['completed_at'] = record['finished_at']
    return record


def _text(value, field, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError(f'{field}: нужна непустая строка длиной до {limit} символов.')
    return value.strip()


def _number(value, field, *, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError(f'{field}: укажи число не меньше {minimum}.')
    return float(value)


def _settings(*, title, prompt, cwd, run_at=None, interval_seconds=None):
    title = _text(title, 'title', 200)
    prompt = _text(prompt, 'prompt', 131072)
    if not isinstance(cwd, (str, Path)) or not Path(cwd).is_absolute():
        raise ValueError('cwd: укажи абсолютный путь к рабочей папке.')
    directory = Path(cwd).resolve(strict=True)
    if not directory.is_dir():
        raise ValueError('cwd должен быть папкой.')
    if interval_seconds is not None:
        interval_seconds = _number(interval_seconds, 'interval_seconds', minimum=1)
        if interval_seconds > 366 * 86400:
            raise ValueError('Интервал не должен превышать 366 дней.')
    if run_at is None:
        if interval_seconds is None:
            raise ValueError('Для разовой задачи нужен run_at (Unix timestamp).')
        run_at = time.time() + interval_seconds
    run_at = _number(run_at, 'run_at')
    return dict(title=title, prompt=prompt, cwd=str(directory), run_at=run_at,
                interval_seconds=interval_seconds)


def list_jobs(state):
    with _database(state) as db:
        return [_record(row) for row in db.execute('SELECT * FROM jobs ORDER BY created_at,id')]


def create_job(state, *, title, prompt, cwd, run_at=None, interval_seconds=None):
    values = _settings(title=title, prompt=prompt, cwd=cwd, run_at=run_at,
                       interval_seconds=interval_seconds)
    identifier, now = uuid.uuid4().hex, time.time()
    with _database(state) as db:
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (identifier, values['title'], values['prompt'], values['cwd'], values['run_at'],
                    values['interval_seconds'], values['run_at'], 1, now, now))
        return _record(db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone())


def update_job(state, job_id, *, expected_cwd=None, **changes):
    if not changes or set(changes) - {'title', 'prompt', 'cwd', 'run_at', 'interval_seconds', 'enabled'}:
        raise ValueError('Укажи title, prompt, cwd, run_at, interval_seconds или enabled.')
    if 'enabled' in changes and type(changes['enabled']) is not bool:
        raise ValueError('enabled должен быть true или false.')
    with _database(state) as db:
        db.execute('BEGIN IMMEDIATE')
        scope = str(Path(expected_cwd).resolve()) if expected_cwd is not None else None
        current = db.execute('SELECT * FROM jobs WHERE id=? AND (? IS NULL OR cwd=?)',
                             (job_id, scope, scope)).fetchone()
        if current is None:
            raise ValueError('Задача не найдена.')
        arguments = {key: changes.get(key, current[key]) for key in
                     ('title', 'prompt', 'cwd', 'run_at', 'interval_seconds')}
        values = _settings(**arguments)
        enabled = changes.get('enabled', bool(current['enabled']))
        next_run = current['next_run']
        if {'run_at', 'interval_seconds'} & changes.keys():
            next_run = values['run_at'] if 'run_at' in changes else time.time() + (values['interval_seconds'] or 0)
        elif enabled and next_run is None:
            next_run = time.time()
        db.execute('''UPDATE jobs SET title=?,prompt=?,cwd=?,run_at=?,interval_seconds=?,
                      next_run=?,enabled=?,updated_at=? WHERE id=?''',
                   (values['title'], values['prompt'], values['cwd'], values['run_at'],
                    values['interval_seconds'], next_run, int(enabled), time.time(), job_id))
        db.execute('COMMIT')
        return _record(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())


def delete_job(state, job_id, *, expected_cwd=None):
    with _database(state) as db:
        scope = str(Path(expected_cwd).resolve()) if expected_cwd is not None else None
        if not db.execute('DELETE FROM jobs WHERE id=? AND (? IS NULL OR cwd=?)',
                          (job_id, scope, scope)).rowcount:
            raise ValueError('Задача не найдена.')
    return {'id': job_id, 'deleted': True}


def list_runs(state, *, limit=100, cwd=None):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('limit должен быть целым числом от 1 до 1000.')
    with _database(state) as db:
        scope = ' WHERE cwd=?' if cwd is not None else ''
        parameters = (str(Path(cwd).resolve()), limit) if cwd is not None else (limit,)
        return [_record(row) for row in db.execute(
            'SELECT * FROM runs' + scope + ' ORDER BY started_at DESC,id DESC LIMIT ?', parameters)]


def delete_run(state, run_id):
    """Remove a finished result from app history; retain its native pi chat."""
    with _database(state) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('Результат не найден.')
        if row['status'] == 'running':
            raise ValueError('Нельзя удалить результат выполняющейся задачи. Сначала останови планировщик.')
        db.execute('DELETE FROM runs WHERE id=?', (run_id,))
        db.execute('COMMIT')
    return {'id': run_id, 'deleted': True}


def mark_notified(state, run_id):
    with _database(state) as db:
        if not db.execute("UPDATE runs SET notified=1 WHERE id=? AND status!='running'", (run_id,)).rowcount:
            raise ValueError('Завершённый запуск не найден.')
    return {'id': run_id, 'notified': True}


def _claim(db, state):
    now = time.time()
    db.execute('BEGIN IMMEDIATE')
    row = db.execute('SELECT * FROM jobs WHERE enabled=1 AND next_run<=? ORDER BY next_run,id LIMIT 1',
                     (now,)).fetchone()
    if row is None:
        db.execute('COMMIT')
        return None
    job = _record(row)
    identifier = uuid.uuid4().hex
    session = Path(state).resolve() / 'pi' / 'sessions' / (identifier + '.jsonl')
    session.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    interval = job['interval_seconds']
    db.execute('UPDATE jobs SET next_run=?,enabled=?,updated_at=? WHERE id=?',
               (now + interval if interval else None, int(bool(interval)), now, job['id']))
    db.execute('''INSERT INTO runs(id,job_id,title,cwd,session_path,status,started_at)
                  VALUES(?,?,?,?,?,'running',?)''',
               (identifier, job['id'], job['title'], job['cwd'], str(session), now))
    db.execute('COMMIT')
    return job, identifier, session


def _answer(raw):
    """Use final assistant text; a zero exit code alone is not model success."""
    answer, error = '', None
    for line in raw.split(b'\n'):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            continue
        message = event.get('message')
        if event.get('type') != 'message_end' or not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        answer, error = '', None
        if message.get('stopReason') in {'error', 'aborted'}:
            error = str(message.get('errorMessage') or 'Модель прервала ответ.')
        content = message.get('content', [])
        if isinstance(content, list) and message.get('stopReason') != 'toolUse':
            text = '\n'.join(part['text'] for part in content if isinstance(part, dict)
                             and part.get('type') == 'text' and isinstance(part.get('text'), str)).strip()
            if text:
                answer = text
    if error:
        raise ValueError(error)
    if not answer:
        raise ValueError('Pi завершился без текстового ответа модели.')
    return answer


def _terminate(child):
    # Reap an exited pi parent, but still terminate its MCP/browser helpers.
    child.poll()
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        # Darwin can report EPERM if it exits between poll() and killpg().
        if child.poll() is None:
            raise
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=3)


def _scheduler_enabled(state):
    try:
        settings = json.loads((Path(state) / 'pi-settings.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return True
    if not isinstance(settings, dict) or type(settings.get('scheduler_enabled', True)) is not bool:
        raise ValueError('Некорректная настройка scheduler_enabled.')
    return settings.get('scheduler_enabled', True)


def _execute(state, job, session, stop):
    from .pi_runtime import scheduled_command

    cwd = Path(job['cwd'])
    if not cwd.is_dir():
        raise ValueError('Рабочая папка задачи больше не существует.')
    command, environment = scheduled_command(Path(state), cwd, session, job['prompt'])
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        child = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=errors, start_new_session=True)
        deadline = time.monotonic() + RUN_TIMEOUT
        try:
            while child.poll() is None:
                if stop.wait(0.25):
                    raise ValueError('Запуск остановлен вместе с планировщиком.')
                if output.tell() + errors.tell() > MAX_OUTPUT:
                    raise ValueError('Вывод задачи превысил допустимый размер (8 МиБ).')
                if not _scheduler_enabled(state):
                    raise ValueError('Планировщик выключен в настройках Handy.')
                if time.monotonic() >= deadline:
                    raise ValueError('Превышено время выполнения задачи (15 минут).')
            if output.tell() + errors.tell() > MAX_OUTPUT:
                raise ValueError('Вывод задачи превысил допустимый размер (8 МиБ).')
            output.seek(max(0, output.tell() - MAX_OUTPUT))
            raw = output.read(MAX_OUTPUT)
            if child.returncode != 0:
                errors.seek(max(0, errors.tell() - 4096))
                detail = errors.read(4096).decode('utf-8', errors='replace').strip()
                raise ValueError(f'Pi завершился с кодом {child.returncode}. {detail}')
            answer = _answer(raw)
            if not session.is_file():
                raise ValueError('Pi не сохранил сессию; чат недоступен.')
            return answer
        finally:
            _terminate(child)


def tick(state, *, stop=None, limit=10):
    """Claim and execute due tasks serially. Only one tick owns executions."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit должен быть целым числом от 1 до 100.')
    state = Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _scheduler_enabled(state):
        return []
    stop = stop or threading.Event()
    results = []
    try:
        with process_lock(state / 'pi-scheduler.tick.lock'):
            with _database(state) as db:
                # Holding the execution lock proves no prior run is still owned.
                db.execute("UPDATE runs SET status='failed',finished_at=?,error=? WHERE status='running'",
                           (time.time(), 'Планировщик был прерван до завершения ответа.'))
                for _ in range(limit):
                    if stop.is_set() or not _scheduler_enabled(state):
                        break
                    claimed = _claim(db, state)
                    if claimed is None:
                        break
                    job, identifier, session = claimed
                    try:
                        answer = _execute(state, job, session, stop)
                        status, error = 'succeeded', None
                    except Exception as failure:
                        answer, status, error = '', 'failed', str(failure)[:4096]
                    db.execute('UPDATE runs SET status=?,finished_at=?,answer=?,error=? WHERE id=?',
                               (status, time.time(), answer, error, identifier))
                    results.append(_record(db.execute('SELECT * FROM runs WHERE id=?', (identifier,)).fetchone()))
    except BlockingIOError:
        return []
    return results


def worker_status(state):
    state = Path(state)
    if not lock_held(state / 'pi-scheduler.worker.lock'):
        return {'running': False, 'pid': None}
    try:
        record = json.loads((state / 'pi-scheduler.worker.json').read_text(encoding='utf-8'))
        pid = record.get('pid')
        if type(pid) is not int or pid <= 1:
            raise ValueError('Некорректный PID планировщика.')
        os.kill(pid, 0)
    except (OSError, ValueError):
        return {'running': True, 'pid': None}
    return {'running': True, 'pid': pid}


def start_worker(state):
    state = Path(state).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _scheduler_enabled(state):
        raise ValueError('Планировщик выключен в настройках Handy.')
    status = worker_status(state)
    if status['running'] and status['pid']:
        return status
    child = None
    if not status['running']:
        with (state / 'pi-scheduler.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, '-m', 'folder_memory.pi_scheduler', '--state-dir', str(state)],
                                     cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=True)
    for _ in range(100):
        status = worker_status(state)
        if status['running'] and status['pid']:
            # Reap the detached daemon when it eventually exits without keeping
            # the caller alive or abandoning a live Popen object.
            if child is not None:
                threading.Thread(target=child.wait, daemon=True).start()
            return status
        if child is not None and child.poll() is not None:
            raise ValueError('Не удалось запустить планировщик. Открой pi-scheduler.log.')
        time.sleep(0.05)
    if child is not None:
        child.terminate()
        child.wait(timeout=5)
    raise ValueError('Планировщик не подтвердил запуск.')


def stop_worker(state):
    status = worker_status(state)
    for _ in range(100):
        if not status['running'] or status['pid']:
            break
        time.sleep(0.05)
        status = worker_status(state)
    if status['running'] and not status['pid']:
        raise ValueError('Планировщик не подтвердил PID; проверь pi-scheduler.log.')
    if status['running'] and status['pid']:
        try:
            os.kill(status['pid'], signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(120):
            if not worker_status(state)['running']:
                return worker_status(state)
            time.sleep(0.05)
        raise ValueError('Планировщик ещё останавливается.')
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=runtime_dir())
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    state = args.state_dir.expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.once:
        print(json.dumps(tick(state), ensure_ascii=False))
        return
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        with process_lock(state / 'pi-scheduler.worker.lock'):
            write_json(state / 'pi-scheduler.worker.json', {'pid': os.getpid()})
            try:
                while not stop.is_set() and _scheduler_enabled(state):
                    tick(state, stop=stop)
                    stop.wait(1)
            finally:
                (state / 'pi-scheduler.worker.json').unlink(missing_ok=True)
    except BlockingIOError:
        return


if __name__ == '__main__':
    main()
