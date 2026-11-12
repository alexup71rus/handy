"""Relay native model chat and serve Handy's explicit local tools."""

import argparse
import hashlib
import hmac
import http.client
import json
import os
import re
import resource
import select
import signal
import socket
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .browser_tool import BrowserTool
from .embeddings import Embedder
from .neural import CHAT_MODEL as MODEL, NeuralError
from .model_backend import create_model, model_config_digest, read_model_config
from .query_api import query as api_query
from .runtime import ENGINE_BUSY, OWNERS, engine_settings, get_token, process_lock, runtime_dir, service_proof, model_config_proof, set_watches_paused
from .schedule_actions import resume_watches
from .semantic import SemanticStore
from .storage import Store
from .tool_api import execute_tool


class App:
    def __init__(self, state_dir, *, owner="foreground"):
        self.state_dir = Path(state_dir).resolve()
        self.owner = owner
        self.token = get_token(self.state_dir)
        self.db = self.state_dir / "memory.sqlite3"
        Store(self.db).close()
        self.session_locks = tuple(threading.RLock() for _ in range(64))
        self.started = time.time()
        self.stopped = threading.Event()
        configuration = read_model_config(self.state_dir)
        self.model_config = model_config_digest(configuration)
        self.neural_model = create_model(self.state_dir, config=configuration)
        self.embedding_model = create_model(self.state_dir, profile="embedding", config=configuration)
        self.browser = BrowserTool(self.state_dir)
        self.semantic = (
            SemanticStore(self.embedding_model.config.get('index_path') or
                          Path(__file__).resolve().parent.parent / "data/semantic.sqlite3",
                          Embedder(self.embedding_model))
            if self.embedding_model.enabled else None
        )

    def _session_lock(self, sid):
        stripe = int.from_bytes(hashlib.blake2s(sid.encode(), digest_size=2).digest(), "big")
        return self.session_locks[stripe % len(self.session_locks)]

    def healthz(self):
        return {"name": "funny", "version": __version__, "pid": os.getpid(), "stopped": self.stopped.is_set(),
                "model_config": self.model_config}

    def _model_workers(self):
        return {"neural": self.neural_model, "embedding": self.embedding_model}

    def health(self):
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            rss *= 1024
        models = {name: worker.stats() for name, worker in self._model_workers().items()}
        return {
            "name": "funny", "pid": os.getpid(), "version": __version__, "model": MODEL,
            "neural_models": [item["name"] for item in models.values() if item["pid"]],
            **models, "semantic": self.semantic.stats() if self.semantic else {"ready": False},
            "uptime_seconds": round(time.time() - self.started), "peak_rss_bytes": rss,
        }

    @contextmanager
    def model_completion(self, body, headers, *, cancelled=None):
        from .chat_evidence import augment_chat

        self._ensure_running()
        if cancelled is not None and cancelled.is_set():
            raise NeuralError("Запрос модели отменён.")
        if body.get("model") != MODEL:
            raise ValueError("Неизвестная модель. Выбери Handy / memory-engine.")
        messages = body.get("messages")
        if not isinstance(messages, list) or not all(
                isinstance(message, dict) and isinstance(message.get("role"), str) for message in messages):
            raise ValueError("messages должен быть массивом сообщений с текстовым полем role.")
        # Pi owns context, tools, compaction and the agent loop. Its requests
        # pass through unchanged to the app-owned llama.cpp worker.
        if headers.get('X-Handy-Client') == 'pi':
            expected = headers.get('X-Handy-Model-Config', '')
            if not isinstance(expected, str) or not expected.isascii() or not hmac.compare_digest(expected, self.model_config):
                raise ValueError('Настройки локальной модели изменились. Выбери модель заново через /models или перезапусти Handy.')
            augmented = body
        else:
            augmented = augment_chat(self, body, headers)
        if cancelled is not None and cancelled.is_set():
            raise NeuralError("Запрос модели отменён.")
        if augmented is not body:
            with self.neural_model.chat_completion(augmented, cancelled=cancelled) as response:
                if response.status not in {400, 413, 422}:
                    yield response
                    return
        with self.neural_model.chat_completion(body, cancelled=cancelled) as response:
            yield response

    def _ensure_running(self):
        if self.stopped.is_set():
            raise OSError("Handy is shutting down")

    def context(self, body, headers):
        from .chat_evidence import augment_chat
        self._ensure_running()
        if body.get('model') != MODEL or not isinstance(body.get('messages'), list):
            raise ValueError('Нужны model и messages для подсчёта контекста.')
        return self.neural_model.input_tokens(augment_chat(self, body, headers))

    @staticmethod
    def _cleanup_error(component, error):
        sys.stderr.write("Handy " + component + " cleanup failed: " + type(error).__name__ + "\n")

    def shutdown(self):
        self.stopped.set()
        try:
            self.browser.shutdown()
        except Exception as error:
            self._cleanup_error("browser", error)
        for model in self._model_workers().values():
            try:
                model.shutdown()
            except Exception as error:
                self._cleanup_error("model", error)

    def watch(self):
        while not self.stopped.wait(10):
            try:
                self.browser.reap_idle()
            except Exception as error:
                self._cleanup_error("idle browser", error)
            for model in self._model_workers().values():
                try:
                    model.reap_idle()
                except Exception as error:
                    self._cleanup_error("idle model", error)


class Handler(BaseHTTPRequestHandler):
    server_version = "Handy/" + __version__
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Never log request bodies or authorization headers.
        sys.stderr.write(time.strftime("%H:%M:%S ") + fmt % args + "\n")

    @property
    def app(self):
        return self.server.app

    def send_json(self, status, data, *, headers=()):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def error(self, status, text):
        self.send_json(status, {"error": {"message": text, "type": "invalid_request_error", "code": str(status)}})

    def authenticated(self):
        # OpenCode backend requests have no browser Origin. A bearer token also
        # separates this local endpoint from arbitrary local web pages.
        auth = self.headers.get_all("Authorization", [])
        return ("Origin" not in self.headers and len(auth) == 1 and auth[0].isascii()
                and hmac.compare_digest(auth[0], "Bearer " + self.app.token))

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json(200, self.app.healthz())
        elif self.path == "/health":
            self.send_json(200, self.app.health())
        elif match := re.fullmatch(r"/api/status\?nonce=([0-9a-f]{32})", self.path):
            status = {**self.app.healthz(), "owner": self.app.owner}
            status["proof"] = service_proof(self.app.token, match[1], self.app.state_dir, status)
            status['model_proof'] = model_config_proof(self.app.token, match[1], self.app.state_dir, status)
            self.send_json(200, status)
        elif self.path == "/v1/models" and self.authenticated():
            self.send_json(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "created": 0, "owned_by": "funny"}]})
        else:
            self.error(404, "Unknown endpoint")

    def do_POST(self):
        if self.path not in {"/v1/chat/completions", "/api/query", "/api/tools", "/api/context", "/api/shutdown"}:
            self.error(404, "Unknown endpoint")
            return
        if not self.authenticated():
            self.close_connection = True
            self.error(401, "Handy: local authorization required")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0].strip()) or "Transfer-Encoding" in self.headers:
            self.close_connection = True
            self.error(400, "Expected a single Content-Length and no Transfer-Encoding")
            return
        length = int(lengths[0])
        if not 0 < length <= 2 * 1024 * 1024:
            self.close_connection = True
            self.error(413, "Maximum request size: 2 MiB")
            return
        try:
            try:
                body = json.loads(self.rfile.read(length))
            except RecursionError:
                raise ValueError("JSON nesting exceeds the supported depth") from None
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            if self.path == "/api/shutdown":
                if body != {"state_dir": str(self.app.state_dir)}:
                    raise ValueError("Handy: shutdown request refers to a different runtime directory")
                self.app.stopped.set()
                try:
                    self.send_json(200, {"stopping": True})
                finally:
                    self.server.shutdown()
                return
            if self.path == "/api/tools":
                self.send_json(200, execute_tool(self.app, body))
                return
            if self.path == "/api/context":
                self.send_json(200, self.app.context(body, self.headers))
                return
            if self.path == "/api/query":
                self.app._ensure_running()
                self.send_json(200, api_query(self.app, body))
                return
            self.relay_completion(body)
        except (ValueError, OSError) as exc:
            self.error(400, str(exc))
            return
        except Exception:
            # Report a real failure; never return success for an exception.
            import traceback
            traceback.print_exc()
            self.error(500, "Handy: внутренняя ошибка. Подробности в журнале локального сервиса.")
            return

    def relay_completion(self, body):
        started = False
        cancelled, finished = threading.Event(), threading.Event()
        peer = self.connection
        self.close_connection = True

        def watch_peer():
            # do_POST has consumed the complete request body; never read or
            # consume any possible pipelined bytes from this connection.
            while not finished.wait(.05):
                if self._peer_closed(peer):
                    cancelled.set()
                    return

        watcher = threading.Thread(target=watch_peer, name="handy-peer-cancel", daemon=True)
        watcher.start()
        try:
            with self.app.model_completion(body, self.headers, cancelled=cancelled) as upstream:
                # http.client decodes chunk framing. Close-delimit streaming
                # responses rather than relaying the upstream transfer header.
                self.close_connection = True
                self.send_response(upstream.status)
                self.send_header("Connection", "close")
                self.send_header("Cache-Control", "no-store")
                names = ("Content-Type", "Content-Encoding")
                if not upstream.chunked:
                    names += ("Content-Length",)
                for name in names:
                    value = upstream.getheader(name)
                    if value and "\r" not in value and "\n" not in value:
                        self.send_header(name, value)
                started = True
                self.end_headers()
                while chunk := upstream.read1(64 * 1024):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (NeuralError, OSError, http.client.HTTPException) as exc:
            if not started and not cancelled.is_set() and not self._peer_closed(peer):
                try:
                    if isinstance(exc, TimeoutError):
                        self.error(504, "Истекло время ответа модели.")
                    elif isinstance(exc, NeuralError):
                        self.error(503, str(exc))
                    else:
                        self.error(503, "Выбранная модель недоступна.")
                except OSError:
                    cancelled.set()  # The peer can disappear while writing the error.
        finally:
            finished.set()
            watcher.join(timeout=.3)

    @staticmethod
    def _peer_closed(peer):
        try:
            readable, _, _ = select.select([peer], [], [], 0)
            return bool(readable) and peer.recv(1, socket.MSG_PEEK | getattr(socket, "MSG_DONTWAIT", 0)) == b""
        except (BlockingIOError, InterruptedError):
            return False
        except (OSError, ValueError):
            return True


class Server(ThreadingHTTPServer):
    daemon_threads = True


def serve(state, *, owner, port=None):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with process_lock(state / "engine.lock", retry=True):
            settings = engine_settings(state)
            if owner != settings["launcher"] and not (owner == "foreground" and settings["launcher"] == "opencode"):
                raise ValueError(f"Handy uses launcher {settings['launcher']}.")
            if port is None:
                port = settings["port"]
            with Server(("127.0.0.1", port), Handler) as server:
                _serve(server, state, owner)
    except BlockingIOError:
        raise BlockingIOError("Handy is already running for this runtime directory.") from None


def _serve(server, state, owner):
    server.app = app = App(state, owner=owner)
    managed = owner in {"managed", "systemd"}

    def stop(*_):
        if not app.stopped.is_set():
            app.stopped.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        if managed:
            set_watches_paused(state, False)
        threading.Thread(target=app.watch, name="handy-model-idle", daemon=True).start()
        print(f"Handy {__version__}: http://127.0.0.1:{server.server_port} (local model: {app.neural_model.enabled})", flush=True)
        try:
            resume_watches(state)
        except (OSError, ValueError, sqlite3.Error) as error:
            app._cleanup_error("schedule worker", error)
        server.serve_forever(poll_interval=0.1)
    finally:
        try:
            if managed:
                set_watches_paused(state, True)
        finally:
            app.shutdown()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Handy local model and tools service")
    parser.add_argument("--port", type=int)
    parser.add_argument("--state-dir", type=Path, default=runtime_dir())
    parser.add_argument("--owner", choices=OWNERS, default="foreground")
    args = parser.parse_args(argv)
    try:
        serve(args.state_dir.resolve(), owner=args.owner, port=args.port)
    except BlockingIOError as error:
        parser.exit(ENGINE_BUSY, f"Handy: {error}\n")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Handy: {error}\n")


if __name__ == "__main__":
    main()
