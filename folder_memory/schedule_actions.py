"""Format background price watches and manage their worker."""

from pathlib import Path
import subprocess
import sys
import time

from .runtime import watches_paused
from .schedules import Schedules

_UNITS = {'minute': 'мин', 'hour': 'ч', 'day': 'дн', 'week': 'нед', 'month': 'мес'}
_ONE = {'minute': 'минуту', 'hour': 'час', 'day': 'день', 'week': 'неделю', 'month': 'месяц'}
DIRECTIONS = {'down': 'снижение', 'up': 'рост', 'any': 'любое изменение'}


def cadence_label(interval):
    if interval['count'] == 1:
        return 'Раз в ' + _ONE[interval['unit']]
    return f'{interval["count"]} {_UNITS[interval["unit"]]}'


def price_label(price, currency):
    return f'{price} {currency}'.strip() if price is not None else '—'


def start_worker(store):
    status = store.worker_status()
    if status['running'] or watches_paused(store.state_dir):
        return status
    try:
        child = subprocess.Popen(
            [sys.executable, '-m', 'folder_memory.schedule_worker', '--state-dir', str(store.path.parent),
             '--until-idle', '--notify-desktop'],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        return status
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = store.worker_status()
        if status['running'] or child.poll() is not None:
            break
        time.sleep(0.05)
    return store.worker_status()


def resume_watches(state_dir):
    if not (state_dir / 'schedules.sqlite3').is_file():
        return
    store = Schedules(state_dir)
    if store.has_enabled():
        start_worker(store)
