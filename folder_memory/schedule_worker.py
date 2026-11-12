"""Run Handy's price watches independently of the chat and model processes."""

import argparse
import fcntl
from html import escape
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

from .schedules import Schedules
from .runtime import process_lock, watches_paused


_NOTIFY_SCRIPT = """
on run argv
    display notification (item 1 of argv) with title "Handy"
end run
"""


def notify_desktop(event):
    change = "снизилась" if event["direction"] == "down" else "выросла"
    message = f'{event["title"]}: цена {change} с {event["previous_price"]} до {event["price"]} {event["currency"]}'
    if sys.platform == "darwin":
        command = ["/usr/bin/osascript", "-e", _NOTIFY_SCRIPT, "--", message]
    else:
        program = shutil.which("notify-send")
        if not program:
            return False
        command = [program, "--", "Handy", escape(message)]
    try:
        process = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=3, check=False)
        return process.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run(schedules, stop, *, until_idle=False, notify=False):
    try:
        with process_lock(schedules.worker_lock_path, retry=True) as fd:
            pid = os.getpid()
            released = False

            def release():
                nonlocal released
                fcntl.flock(fd, fcntl.LOCK_UN)
                released = True

            try:
                next_heartbeat = 0
                while not stop.is_set() and not watches_paused(schedules.state_dir):
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        schedules.heartbeat(pid)
                        next_heartbeat = now + 15
                    pause = schedules.worker_pause(pid, release, until_idle=until_idle)
                    if pause is None:
                        return
                    if pause > 0:
                        stop.wait(min(1, pause))
                    else:
                        for event in schedules.tick(limit=1):
                            if notify:
                                notify_desktop(event)
            finally:
                if not released:
                    schedules.worker_stopped(pid)
    except BlockingIOError:
        return


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent.parent / ".runtime")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run due checks and print matching events as JSON.")
    mode.add_argument("--until-idle", action="store_true", help="Exit when there are no enabled watches.")
    parser.add_argument("--notify-desktop", action="store_true", help="Also attempt a local desktop notification.")
    args = parser.parse_args(argv)
    schedules = Schedules(args.state_dir)
    if args.once:
        events = schedules.tick()
        if args.notify_desktop:
            for event in events:
                notify_desktop(event)
        print(json.dumps(events, ensure_ascii=False))
        return
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    run(schedules, stop, until_idle=args.until_idle, notify=args.notify_desktop)


if __name__ == "__main__":
    main()
