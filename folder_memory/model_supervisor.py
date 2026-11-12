"""Tie local workers to their owning core process."""

import os
import selectors
import signal
import subprocess
import sys


def _stop_worker(worker):
    if worker.poll() is None:
        try:
            worker.terminate()
        except ProcessLookupError:
            pass
        try:
            worker.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            try:
                worker.kill()
            except ProcessLookupError:
                pass
            worker.wait(timeout=1)


def _supervise_stdio(args, active):
    # MCP uses the inherited stdin for RPC. Monitoring the owner instead of
    # reading the pipe leaves all request bytes for the worker.
    worker = subprocess.Popen(args, start_new_session=True)
    try:
        while active():
            try:
                code = worker.wait(timeout=0.1)
                return code if code >= 0 else 128 - code
            except subprocess.TimeoutExpired:
                continue
        return 0
    finally:
        # The worker can exit before its helpers. Signal the owned process
        # group even when the worker's own exit status has already arrived.
        try:
            os.killpg(worker.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            worker.wait(timeout=0.75)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        worker.wait(timeout=0.75)


def main():
    # This is a private argv-only process entry point, never a shell adapter.
    owner = int(sys.argv[1])
    args = sys.argv[2:]
    stdio = args[:1] == ["--stdio"]
    if stdio:
        args = args[1:]
    if owner <= 0 or not args or os.getppid() != owner:
        return 125
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if stdio:
        def active():
            return not stopping and os.getppid() == owner

        return _supervise_stdio(args, active) if active() else 125
    with selectors.DefaultSelector() as selector:
        selector.register(sys.stdin.fileno(), selectors.EVENT_READ)

        def disconnected(timeout):
            return bool(selector.select(timeout)) and not os.read(sys.stdin.fileno(), 4096)

        if stopping or disconnected(0) or os.getppid() != owner:
            return 125
        worker = subprocess.Popen(args, stdin=subprocess.DEVNULL)
        try:
            while not stopping and os.getppid() == owner:
                code = worker.poll()
                if code is not None:
                    return code if code >= 0 else 128 - code
                if disconnected(0.05):
                    break
            return 0
        finally:
            _stop_worker(worker)


if __name__ == "__main__":
    raise SystemExit(main())
