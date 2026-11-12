"""Persistent, typed price watches and an inbox of matching price changes."""

import calendar
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from urllib.parse import quote, urlunsplit

from .http_client import validate_public_url
from .runtime import lock_held, watches_paused
from .scheduled_price import PriceUnavailable, check_price, currency_code, selector_steps


class ScheduleConflict(ValueError):
    pass


_UNITS = {"minute": (60, 525_600), "hour": (3600, 8760), "day": (86400, 365), "week": (604800, 52), "month": (None, 120)}
_DIRECTIONS = {"down", "up", "any"}
_LEASE_SECONDS = 20
_EVENT_LIMIT = 100
_WORKER_STALE_SECONDS = 30
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, config TEXT NOT NULL,
    enabled INTEGER NOT NULL, revision INTEGER NOT NULL, anchor REAL NOT NULL,
    next_run REAL, last_checked REAL, last_price TEXT, last_currency TEXT,
    last_source TEXT, last_error TEXT, lease TEXT, lease_until REAL
);
CREATE INDEX IF NOT EXISTS due_jobs ON jobs(enabled, next_run);
CREATE INDEX IF NOT EXISTS workspace_jobs ON jobs(workspace);
CREATE TABLE IF NOT EXISTS price_events(
    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    title TEXT NOT NULL, checked REAL NOT NULL, direction TEXT NOT NULL,
    previous_price TEXT NOT NULL, price TEXT NOT NULL, currency TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_events ON price_events(job_id, id);
CREATE TABLE IF NOT EXISTS worker(slot INTEGER PRIMARY KEY, pid INTEGER NOT NULL, heartbeat REAL NOT NULL);
"""


def _now(value):
    return time.time() if value is None else value


def _text(value, limit):
    return isinstance(value, str) and len(value) <= limit and all(ord(char) >= 32 and ord(char) != 127 and not 0xD800 <= ord(char) <= 0xDFFF for char in value)


def validate_schedule(url, interval, direction, title="", xpath="", currency=""):
    """Validate settings without doing any network work before approval."""
    if not _text(url, 4096) or any(char.isspace() for char in url):
        raise ValueError("Нужна полная HTTP/HTTPS ссылка без пробелов.")
    try:
        parsed = validate_public_url(url)
        host = parsed.hostname.encode("idna").decode().lower()
    except (ValueError, UnicodeError):
        raise ValueError("Нужна корректная ссылка на публичный HTTP/HTTPS сайт без логина и пароля.") from None
    netloc = f"[{host}]" if ":" in host else host
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    url = urlunsplit((parsed.scheme, netloc, quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                      quote(parsed.query, safe="/?%:@!$&'()*+,;=-._~"), ""))
    if len(url) > 4096:
        raise ValueError("Ссылка на сайт слишком длинная.")
    validate_public_url(url)
    if not isinstance(interval, dict) or set(interval) != {"count", "unit"}:
        raise ValueError("Нужен интервал проверки в минутах, часах, днях, неделях или месяцах.")
    count, unit = interval["count"], interval["unit"]
    if not isinstance(unit, str) or unit not in _UNITS or type(count) is not int or not 1 <= count <= _UNITS[unit][1]:
        raise ValueError("Нужен положительный интервал проверки допустимого размера.")
    if not isinstance(direction, str) or direction not in _DIRECTIONS:
        raise ValueError("Укажи, какие изменения отслеживать: снижение, повышение или оба направления.")
    if not _text(title, 120):
        raise ValueError("Название задачи должно быть одной строкой до 120 символов.")
    if not _text(xpath, 512):
        raise ValueError("Некорректный XPath.")
    if xpath:
        selector_steps(xpath)
    if currency != "" and not currency_code(currency):
        raise ValueError("Валюта задаётся кодом из трёх букв, например RUB или USD.")
    return {"url": url, "interval": {"count": count, "unit": unit}, "direction": direction,
            "title": title.strip() or host, "xpath": xpath, "currency": currency}


def _next_run(anchor, interval, now):
    count, unit = interval["count"], interval["unit"]
    seconds = _UNITS[unit][0]
    if seconds is not None:
        period = seconds * count
        return anchor + max(1, int((now - anchor) // period) + 1) * period
    first = datetime.fromtimestamp(anchor, UTC)
    current = datetime.fromtimestamp(now, UTC)
    origin = first.year * 12 + first.month - 1
    elapsed = current.year * 12 + current.month - 1 - origin
    months = max(count, elapsed // count * count)
    for offset in (months, months + count):
        year, month_index = divmod(origin + offset, 12)
        month = month_index + 1
        candidate = first.replace(year=year, month=month, day=min(first.day, calendar.monthrange(year, month)[1]))
        if candidate.timestamp() > now:
            return candidate.timestamp()
    raise ValueError("Не удалось рассчитать следующее наблюдение.")


def _view(row):
    if row is None:
        return None
    return {**json.loads(row["config"]), **{key: row[key] for key in
            ("id", "workspace", "revision", "next_run", "last_checked", "last_price", "last_currency", "last_error")},
            "enabled": bool(row["enabled"])}


def _job(db, workspace, job_id):
    return db.execute("SELECT * FROM jobs WHERE workspace=? AND id=?", (workspace, job_id)).fetchone()


def _due(db):
    return db.execute("SELECT MIN(MAX(next_run, COALESCE(lease_until, 0))) FROM jobs WHERE enabled=1").fetchone()[0]


class Schedules:
    def __init__(self, state_dir, *, check=check_price):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.state_dir / "schedules.sqlite3"
        self.worker_lock_path = self.state_dir / "schedules-worker.lock"
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Хранилище наблюдений должно быть обычным файлом.")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.check = check
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def create(self, workspace, *, url, interval, direction="any", title="", xpath="", currency="", job_id=None, now=None):
        config = validate_schedule(url, interval, direction, title, xpath, currency)
        job_id = secrets.token_hex(6) if job_id is None else job_id
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{12}", job_id):
            raise ValueError("Некорректный идентификатор задачи.")
        now = _now(now)
        with self._db(write=True) as db:
            existing = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if existing:
                if existing["workspace"] == workspace and existing["revision"] == 1 and json.loads(existing["config"]) == config:
                    return _view(existing)
                raise ScheduleConflict("Идентификатор наблюдения уже занят.")
            if db.execute("SELECT COUNT(*) FROM jobs WHERE workspace=?", (workspace,)).fetchone()[0] >= 64:
                raise ValueError("В этом проекте уже 64 наблюдения. Можно удалить одно из существующих.")
            db.execute("INSERT INTO jobs(id,workspace,config,enabled,revision,anchor,next_run) VALUES(?,?,?,1,1,?,?)",
                       (job_id, workspace, json.dumps(config, ensure_ascii=False), now, now))
            return _view(_job(db, workspace, job_id))

    def get(self, workspace, job_id):
        with self._db() as db:
            return _view(_job(db, workspace, job_id))

    def list(self, workspace):
        with self._db() as db:
            return [_view(row) for row in db.execute("SELECT * FROM jobs WHERE workspace=? ORDER BY enabled DESC, anchor, id", (workspace,))]

    def has_enabled(self):
        with self._db() as db:
            return db.execute("SELECT 1 FROM jobs WHERE enabled=1 LIMIT 1").fetchone() is not None

    def _checked(self, db, workspace, job_id, revision):
        row = _job(db, workspace, job_id)
        if row is None:
            raise ScheduleConflict("Такой задачи уже нет в этом проекте.")
        if type(revision) is not int or revision != row["revision"]:
            raise ScheduleConflict("Задача изменилась после запроса. Открой список и повтори изменение.")
        return row

    def edit(self, workspace, job_id, revision, *, url=None, interval=None, direction=None, title=None, xpath=None, currency=None, enabled=None, now=None):
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("Задача может быть включена или выключена.")
        now = _now(now)
        with self._db(write=True) as db:
            row = self._checked(db, workspace, job_id, revision)
            previous = json.loads(row["config"])
            changes = {"url": url, "interval": interval, "direction": direction, "title": title, "xpath": xpath, "currency": currency}
            config = validate_schedule(**{key: previous[key] if value is None else value for key, value in changes.items()})
            enabled = bool(row["enabled"]) if enabled is None else enabled
            if previous == config and bool(row["enabled"]) == enabled:
                return _view(row)
            source_changed = any(previous[key] != config[key] for key in ("url", "xpath", "currency"))
            interval_changed = previous["interval"] != config["interval"]
            restart = source_changed or interval_changed or not row["enabled"] or row["lease"] is not None
            next_run = (now if restart else row["next_run"]) if enabled else None
            anchor = now if interval_changed else row["anchor"]
            previous_check = (None,) * 5 if source_changed else tuple(row[key] for key in ("last_checked", "last_price", "last_currency", "last_source", "last_error"))
            db.execute(
                "UPDATE jobs SET config=?,enabled=?,revision=revision+1,anchor=?,next_run=?,last_checked=?,last_price=?,last_currency=?,last_source=?,last_error=?,lease=NULL,lease_until=NULL WHERE id=?",
                (json.dumps(config, ensure_ascii=False), enabled, anchor, next_run, *previous_check, job_id))
            return _view(_job(db, workspace, job_id))

    def delete(self, workspace, job_id, revision):
        with self._db(write=True) as db:
            row = self._checked(db, workspace, job_id, revision)
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            return {**_view(row), "deleted": True}

    def events(self, workspace, *, limit=20):
        with self._db() as db:
            rows = db.execute(
                "SELECT e.* FROM price_events e JOIN jobs j ON j.id=e.job_id WHERE j.workspace=? ORDER BY e.id DESC LIMIT ?",
                (workspace, min(max(limit, 0), _EVENT_LIMIT)))
            return [dict(row) for row in rows]

    def _claim(self, now):
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE enabled=1 AND next_run<=? AND (lease_until IS NULL OR lease_until<=?) ORDER BY next_run, id LIMIT 1", (now, now)).fetchone()
            if row is None:
                return None
            lease = secrets.token_hex(16)
            db.execute("UPDATE jobs SET lease=?,lease_until=? WHERE id=?", (lease, now + _LEASE_SECONDS, row["id"]))
            return dict(row) | {"lease": lease}

    def _record(self, claim, price, error, now):
        with self._db(write=True) as db:
            current = db.execute("SELECT revision,lease FROM jobs WHERE id=? AND enabled=1", (claim["id"],)).fetchone()
            if current is None or current["revision"] != claim["revision"] or current["lease"] != claim["lease"]:
                return None
            config = json.loads(claim["config"])
            previous = claim["last_price"]
            amount, currency, source = previous, claim["last_currency"], claim["last_source"]
            event = None
            if price is not None:
                if previous is not None and (currency != price.currency or source != price.source_key):
                    error = "Источник цены или валюта изменились. Сравнение пропущено."
                else:
                    amount, currency, source = format(price.amount, "f"), price.currency, price.source_key
                    if previous is not None and Decimal(previous) != price.amount:
                        direction = "down" if price.amount < Decimal(previous) else "up"
                        if config["direction"] in {direction, "any"}:
                            inserted = db.execute(
                                "INSERT INTO price_events(job_id,title,checked,direction,previous_price,price,currency) VALUES(?,?,?,?,?,?,?)",
                                (claim["id"], config["title"], now, direction, previous, amount, currency))
                            row = db.execute("SELECT * FROM price_events WHERE id=?", (inserted.lastrowid,)).fetchone()
                            event = dict(row)
                            db.execute("DELETE FROM price_events WHERE job_id=? AND id<=COALESCE((SELECT id FROM price_events WHERE job_id=? ORDER BY id DESC LIMIT 1 OFFSET ?),0)",
                                       (claim["id"], claim["id"], _EVENT_LIMIT))
            db.execute("UPDATE jobs SET last_checked=?,last_price=?,last_currency=?,last_source=?,last_error=?,next_run=?,lease=NULL,lease_until=NULL WHERE id=?",
                       (now, amount, currency, source, error, _next_run(claim["anchor"], config["interval"], now), claim["id"]))
            return event

    def tick(self, now=None, limit=8):
        """Poll due jobs once. Only committed, selected-direction changes are returned."""
        events = []
        for _ in range(max(0, min(limit, 64))):
            if watches_paused(self.state_dir):
                break
            claim = self._claim(_now(now))
            if claim is None:
                break
            price, error = None, None
            try:
                price = self.check(_view(claim))
            except PriceUnavailable as exc:
                error = str(exc)[:240]
            event = self._record(claim, price, error, _now(now))
            if event is not None:
                events.append(event)
        return events

    def worker_status(self, now=None):
        with self._db() as db:
            row = db.execute("SELECT * FROM worker WHERE slot=1").fetchone()
            running = bool(row and 0 <= _now(now) - row["heartbeat"] < _WORKER_STALE_SECONDS)
            if running:
                try:
                    os.kill(row["pid"], 0)
                except ProcessLookupError:
                    running = False
                except PermissionError:
                    pass
            # A PID can still exist after the worker has lost its lock.
            try:
                running = running and lock_held(self.worker_lock_path)
            except (OSError, ValueError):
                running = False
            return {"running": running, "pid": row["pid"] if running else None, "heartbeat": row["heartbeat"] if row else None}

    def heartbeat(self, pid):
        with self._db(write=True) as db:
            db.execute("INSERT OR REPLACE INTO worker VALUES(1,?,?)", (pid, time.time()))

    def worker_pause(self, pid, release, *, until_idle):
        """Release the worker lock atomically with observing no active watches."""
        with self._db() as db:
            next_run = _due(db)
        if until_idle and next_run is None:
            with self._db(write=True) as db:
                next_run = _due(db)
                if next_run is None:
                    db.execute("DELETE FROM worker WHERE slot=1 AND pid=?", (pid,))
                    release()
                    return None
        return 5 if next_run is None else max(0, min(5, next_run - time.time()))

    def worker_stopped(self, pid):
        with self._db(write=True) as db:
            db.execute("DELETE FROM worker WHERE slot=1 AND pid=?", (pid,))
