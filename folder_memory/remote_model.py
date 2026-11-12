"""OpenAI-compatible models whose service lifecycle belongs to the provider."""

import errno
import http.client
import ipaddress
import json
import os
import re
import select
import socket
import ssl
import stat
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .neural import ContextBudgetError, LocalModel, NeuralError


_CHAT = "/v1/chat/completions"
_EMBEDDINGS = "/v1/embeddings"
_MAX_JSON_BYTES = 4 * 1024 * 1024
# getaddrinfo cannot be interrupted portably. Bound outstanding DNS calls across
# adapters; the request itself never waits for an abandoned resolver to finish.
_DNS_SLOTS = threading.BoundedSemaphore(4)
_LLAMA_OPTIONS = frozenset({
    "cache_prompt", "chat_template_kwargs", "enable_thinking", "top_k", "min_p",
    "typical_p", "repeat_penalty", "repeat_last_n", "presence_penalty_last_n",
    "frequency_penalty_last_n", "dry_multiplier", "dry_base", "dry_allowed_length",
    "dry_penalty_last_n", "mirostat", "mirostat_tau", "mirostat_eta", "tfs_z",
    "penalize_nl", "ignore_eos", "grammar", "json_schema", "slot_id", "id_slot",
    "n_keep", "n_predict", "return_tokens", "return_progress", "samplers",
    "post_sampling_probs", "timings_per_token", "prompt_cache", "prompt_cache_all",
})
_HOME_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "::1/128", "fc00::/7",
))


def _tls_context():
    context = ssl.create_default_context()
    if not context.get_ca_certs():
        # python.org builds on macOS may have no installed default CA bundle.
        # Use the same verified public roots shipped with our HTTP dependencies.
        try:
            import certifi
            context.load_verify_locations(cafile=certifi.where())
        except ImportError:
            pass
    return context


def _home_address(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address.version == network.version and address in network for network in _HOME_NETWORKS)


def validate_base_url(base):
    """Validate a provider API base URL without resolving or contacting it."""
    if (not isinstance(base, str) or not base or len(base) > 8192
            or any(character.isspace() or ord(character) < 32 for character in base)
            or "\\" in base):
        raise NeuralError("Нужен корректный base_url внешнего провайдера.")
    try:
        parsed = urlsplit(base)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise NeuralError("Нужен корректный base_url внешнего провайдера.") from None
    if (parsed.scheme not in {"https", "http"} or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or "?" in base or "#" in base
            or not host.isascii() or "%" in host
            or (port is not None and not 1 <= port <= 65535)):
        raise NeuralError("base_url должен использовать HTTP(S), без ключей, логина, query и fragment.")
    if parsed.path and (not parsed.path.isascii() or "//" in parsed.path
                        or any(part in {".", ".."} for part in parsed.path.split("/"))):
        raise NeuralError("Нужен корректный путь base_url внешнего провайдера.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if parsed.scheme == "http" and address is not None and not _home_address(host):
        raise NeuralError("Обычный HTTP разрешён только для localhost и домашней сети; используй HTTPS.")
    return base.rstrip("/")


class _EstimatedContextBudgetError(ContextBudgetError):
    def __init__(self, input_tokens, max_tokens, context_size):
        super().__init__(input_tokens, max_tokens, context_size)
        self.estimated = True
        self.args = (f"Консервативная оценка запроса: {input_tokens} токенов; для ответа нужно ещё {max_tokens}. "
                     f"Окно модели вмещает {context_size} токенов. Сократи запрос или настрой точный подсчёт у провайдера.",)


class OpenAIModel(LocalModel):
    """Use the local model's completion parsing without owning any processes.

    ``token_count_path`` is optional and must accept the prepared chat request
    and return ``{"input_tokens": <positive integer>}``. Without that endpoint,
    context checks use a deliberately conservative UTF-8 byte estimate and say
    so in their metadata. No tokenizer or health endpoint is assumed.
    """

    def __init__(self, state_dir, *, profile=None, config=None):
        self.state = Path(state_dir).resolve()
        self.profile = profile
        if config is None:
            path = self.state / "neural.json"
            configured = json.loads(path.read_text()) if path.exists() else {}
            config = configured.get(profile, {}) if profile else configured
        if not isinstance(config, dict):
            raise NeuralError("Настройки внешнего провайдера должны быть объектом JSON.")
        self.config = dict(config)
        self.process = None
        self.lock = threading.RLock()
        self.shutdown_event = threading.Event()
        self.last_used = 0
        self._sockets = set()
        self._sockets_lock = threading.Lock()

    def _settings(self):
        config = self.config
        if config.get("backend", "openai") != "openai":
            raise NeuralError("Для внешнего адаптера нужен backend=openai.")
        parsed = urlsplit(validate_base_url(config.get("base_url")))
        model = config.get("model_id", config.get("model"))
        if (not isinstance(model, str) or not model.strip() or len(model) > 1024
                or any(ord(character) < 32 or ord(character) == 127 for character in model)):
            raise NeuralError("Укажи model_id внешнего провайдера.")
        if "api_key" in config:
            raise NeuralError("Сохрани ключ в api_key_env или приватном api_key_file.")
        environment, key_file = config.get("api_key_env"), config.get("api_key_file")
        if environment and key_file:
            raise NeuralError("Укажи только один источник ключа: api_key_env или api_key_file.")
        if environment is not None and (not isinstance(environment, str)
                                       or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", environment)):
            raise NeuralError("api_key_env должен содержать имя переменной окружения.")
        if key_file is not None and (not isinstance(key_file, str) or not key_file
                                     or "\x00" in key_file):
            raise NeuralError("api_key_file должен указывать на приватный файл ключа.")
        if "dimensions" in config and (type(config["dimensions"]) is not int
                                        or not 1 <= config["dimensions"] <= 65536):
            raise NeuralError("dimensions должен быть положительным целым числом не больше 65536.")
        if "token_count_path" in config:
            self._validate_path(config["token_count_path"])
            if config["token_count_path"] in {_CHAT, _EMBEDDINGS}:
                raise NeuralError("token_count_path должен указывать на отдельный endpoint подсчёта токенов.")
        return parsed, model

    @staticmethod
    def _validate_path(path):
        if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
                or len(path) > 8192 or not path.isascii()
                or any(character.isspace() or ord(character) < 32 for character in path)
                or any(character in path for character in "?#\\")
                or any(part in {".", ".."} for part in path.split("/"))):
            raise NeuralError("Endpoint провайдера должен быть относительным путём без query и fragment.")

    @property
    def context_size(self):
        value = self.config.get("context", 4096)
        if type(value) is not int or not 1 <= value < 2 ** 31:
            raise NeuralError("Размер контекста должен быть положительным числом токенов.")
        return value

    def _key(self):
        environment, key_file = self.config.get("api_key_env"), self.config.get("api_key_file")
        value = None
        if environment:
            value = os.environ.get(environment)
        elif key_file:
            path = Path(key_file).expanduser()
            if not path.is_absolute():
                path = self.state / path
            try:
                local_token = self.state / "token"
                if local_token.exists() and path.samefile(local_token):
                    raise NeuralError("Локальный токен Handy нельзя использовать как ключ провайдера.")
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    details = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077
                            or details.st_uid != os.getuid() or details.st_size > 16384):
                        raise NeuralError("Файл ключа должен принадлежать текущему пользователю и иметь права 600.")
                    value = stream.read(16385).decode("ascii")
            except NeuralError:
                raise
            except (OSError, ValueError, UnicodeError):
                raise NeuralError("Не удалось прочитать приватный файл ключа провайдера.") from None
        if environment or key_file:
            if (not isinstance(value, str) or not value.strip() or len(value) > 16384
                    or any(not 33 <= ord(character) <= 126 for character in value.strip())):
                raise NeuralError("Ключ провайдера недоступен или имеет неверный формат.")
            return value.strip()
        return None

    @property
    def enabled(self):
        if not self.config.get("enabled") or self.shutdown_event.is_set():
            return False
        try:
            self._settings()
            self.context_size
            self._key()
        except NeuralError:
            return False
        return True

    def _start(self):
        if self.shutdown_event.is_set():
            raise NeuralError("Языковой адаптер остановлен.")
        self._settings()
        self.context_size
        self._key()
        if not self.config.get("enabled"):
            raise NeuralError("Внешний провайдер отключён.")

    def _start_locked(self):
        self._start()

    def _body(self, path, body):
        if body is None:
            return None
        if not isinstance(body, dict):
            raise NeuralError("Запрос провайдера должен быть объектом JSON.")
        _, model = self._settings()
        result = dict(body)
        if path in {_CHAT, _EMBEDDINGS, self.config.get("token_count_path")} or "model" in result:
            result["model"] = model
        if path == _CHAT or path == self.config.get("token_count_path"):
            for name in _LLAMA_OPTIONS:
                result.pop(name, None)
            if "max_tokens" not in result and "max_completion_tokens" not in result:
                result["max_tokens"] = self.config.get("max_output_tokens", 1536)
        if path == _EMBEDDINGS:
            result.pop("truncate", None)
            result.pop("pooling", None)
            if self.config.get('send_dimensions') is True and "dimensions" in self.config:
                result["dimensions"] = self.config["dimensions"]
        return result

    @staticmethod
    def _abort(upstream):
        try:
            upstream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            upstream.close()
        except OSError:
            pass

    def _cancelled(self, cancelled):
        if self.shutdown_event.is_set():
            raise NeuralError("Языковой адаптер остановлен.")
        if cancelled is not None and cancelled.is_set():
            raise NeuralError("Запрос модели отменён.")

    def _connect_interval(self, deadline, cancelled):
        self._cancelled(cancelled)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return min(.05, remaining)

    def _addresses(self, host, port, deadline, cancelled):
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            target = (host, port, 0, 0) if address.version == 6 else (host, port)
            return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", target)]
        slots = _DNS_SLOTS
        while not slots.acquire(timeout=self._connect_interval(deadline, cancelled)):
            pass
        done, result = threading.Event(), {}

        def resolve():
            # This helper owns no socket, model, request body or provider key.
            # A late DNS result can therefore never open an abandoned request.
            try:
                result["addresses"] = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            except Exception as error:
                result["error"] = error
            finally:
                slots.release()
                done.set()

        resolver = threading.Thread(target=resolve, name="handy-remote-dns", daemon=True)
        try:
            self._connect_interval(deadline, cancelled)
            resolver.start()
        except BaseException:
            slots.release()
            raise
        while not done.wait(self._connect_interval(deadline, cancelled)):
            pass
        self._cancelled(cancelled)
        if "error" in result:
            raise result["error"]
        return result["addresses"]

    def _wait_connect(self, upstream, deadline, cancelled, *, read=False):
        while True:
            interval = self._connect_interval(deadline, cancelled)
            ready = select.select([upstream] if read else [], [] if read else [upstream],
                                  [upstream], interval)
            if any(ready):
                return

    def _connect(self, connection, *, context=None, cancelled=None):
        deadline = time.monotonic() + connection.timeout
        addresses = self._addresses(connection.host, connection.port, deadline, cancelled)
        if context is None and (not addresses or any(not _home_address(item[4][0]) for item in addresses)):
            raise NeuralError("Обычный HTTP разрешён только для localhost и домашней сети; используй HTTPS.")
        last_error = None
        for family, kind, protocol, _, address in addresses:
            self._connect_interval(deadline, cancelled)
            upstream = socket.socket(family, kind, protocol)
            with self._sockets_lock:
                self._sockets.add(upstream)
            try:
                self._cancelled(cancelled)
                upstream.setblocking(False)
                status = upstream.connect_ex(address)
                if status not in {0, errno.EISCONN}:
                    if status not in {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR}:
                        raise OSError(status, os.strerror(status))
                    self._wait_connect(upstream, deadline, cancelled)
                    status = upstream.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if status:
                        raise OSError(status, os.strerror(status))
                if context is not None:
                    # Transfer ownership under the same lock used by close(),
                    # before the first TLS handshake I/O can block.
                    with self._sockets_lock:
                        wrapped = context.wrap_socket(upstream, server_hostname=connection.host,
                                                      do_handshake_on_connect=False)
                        self._sockets.discard(upstream)
                        upstream = wrapped
                        self._sockets.add(upstream)
                    while True:
                        self._connect_interval(deadline, cancelled)
                        try:
                            upstream.do_handshake()
                            break
                        except ssl.SSLWantReadError:
                            self._wait_connect(upstream, deadline, cancelled, read=True)
                        except ssl.SSLWantWriteError:
                            self._wait_connect(upstream, deadline, cancelled)
                self._cancelled(cancelled)
                upstream.settimeout(connection.timeout)
                connection.sock = upstream
                return upstream
            except OSError as error:
                last_error = error
            finally:
                if connection.sock is not upstream:
                    self._abort(upstream)
                    with self._sockets_lock:
                        self._sockets.discard(upstream)
        if last_error is not None:
            raise last_error
        raise OSError("No provider addresses")

    @contextmanager
    def _response(self, path, body=None, timeout=30, *, cancelled=None):
        self._cancelled(cancelled)
        self._validate_path(path)
        parsed, _ = self._settings()
        key = self._key()
        body = self._body(path, body)
        try:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None
        except (ValueError, TypeError, RecursionError):
            raise NeuralError("Запрос провайдера содержит некорректный JSON.") from None
        base_path = parsed.path.rstrip("/")
        # base_url names the API root, as in OpenAI clients. Providers may use
        # /api/v1 or /v1beta/openai instead of a terminal /v1 segment.
        endpoint = base_path + (path[3:] if base_path and path.startswith("/v1/") else path)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        tls = {'context': _tls_context()} if parsed.scheme == 'https' else {}
        finished, watcher, upstream = threading.Event(), None, None
        try:
            # http.client uses neither environment proxies nor automatic redirects.
            with closing(connection_type(parsed.hostname, parsed.port, timeout=timeout, **tls)) as connection:
                upstream = self._connect(connection, context=tls.get('context'), cancelled=cancelled)
                self._cancelled(cancelled)
                if cancelled is not None:
                    def abort_cancelled_request():
                        while not finished.wait(.05):
                            if cancelled.is_set() or self.shutdown_event.is_set():
                                self._abort(upstream)
                                return

                    watcher = threading.Thread(target=abort_cancelled_request,
                                               name="handy-remote-cancel", daemon=True)
                    watcher.start()
                connection.request("POST" if body is not None else "GET", endpoint, body=data, headers=headers)
                with connection.getresponse() as response:
                    self._cancelled(cancelled)
                    yield response
        except NeuralError:
            raise
        except TimeoutError:
            self._cancelled(cancelled)
            raise NeuralError("Истекло время ответа внешнего провайдера.") from None
        except (OSError, ValueError, http.client.HTTPException):
            self._cancelled(cancelled)
            raise NeuralError("Внешний провайдер недоступен.") from None
        finally:
            finished.set()
            if watcher is not None:
                watcher.join(timeout=.3)
            if upstream is not None:
                with self._sockets_lock:
                    self._sockets.discard(upstream)

    @staticmethod
    def _status_error(response):
        status = response.status
        # Inspect only a bounded body to classify context failures. Never echo
        # provider errors, URLs, prompts or credentials into Handy's responses.
        detail = response.read(16384).decode("utf-8", errors="replace").lower()
        if 300 <= status < 400:
            reason = "перенаправление отклонено"
        elif status in {401, 403}:
            reason = "проверь ключ и доступ к модели"
        elif status == 429:
            reason = "превышен лимит запросов или квота"
        elif (any(word in detail for word in ("context", "too many tokens", "token limit"))
              and any(word in detail for word in ("exceed", "too long", "maximum", "limit", "too many"))):
            reason = "превышен размер контекста"
        else:
            reason = "провайдер отклонил запрос"
        raise NeuralError(f"Внешний провайдер: HTTP {status} — {reason}.")

    def _request(self, path, body=None, timeout=30, *, cancelled=None):
        with self._response(path, body, timeout, cancelled=cancelled) as response:
            if not 200 <= response.status < 300:
                self._status_error(response)
            raw = response.read(_MAX_JSON_BYTES + 1)
            if len(raw) > _MAX_JSON_BYTES:
                raise NeuralError("Ответ внешнего провайдера превышает допустимый размер JSON.")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise NeuralError("Внешний провайдер вернул некорректный JSON.") from None

    def _count(self, body, *, cancelled=None):
        path = self.config.get("token_count_path")
        if path:
            result = self._request(path, body, timeout=30, cancelled=cancelled)
            count = result.get("input_tokens") if isinstance(result, dict) else None
            if type(count) is not int or count < 1:
                raise NeuralError("Провайдер не вернул корректный размер запроса в токенах.")
            return {"input_tokens": count, "context_size": self.context_size,
                    "estimated": False, "token_count_method": "provider_endpoint"}
        # UTF-8 bytes plus a template allowance conservatively estimate text tokens.
        # Inline images can be overestimated; provider token counters are preferred.
        try:
            size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
        except (ValueError, TypeError, RecursionError):
            raise NeuralError("Запрос провайдера содержит некорректный JSON.") from None
        messages = body.get("messages", [])
        tools = body.get("tools", [])
        count = size + 1024 + 64 * (len(messages) if isinstance(messages, list) else 1)
        count += 64 * (len(tools) if isinstance(tools, list) else 1)
        return {"input_tokens": count, "context_size": self.context_size,
                "estimated": True, "token_count_method": "utf8_upper_bound"}

    def input_tokens(self, body):
        with self.lock:
            self._start()
            self.last_used = time.monotonic()
            return self._count(self._body(_CHAT, body))

    def _guard_context(self, body, *, cancelled=None):
        budget = body.get("max_completion_tokens", body.get("max_tokens"))
        if type(budget) is not int or budget <= 0:
            raise NeuralError("Лимит ответа должен быть положительным числом токенов.")
        count = self._count(body, cancelled=cancelled)
        if count["input_tokens"] + budget > self.context_size:
            error = _EstimatedContextBudgetError if count["estimated"] else ContextBudgetError
            raise error(count["input_tokens"], budget, self.context_size)

    def _generate(self, path, body):
        with self.lock:
            self._start()
            prepared = self._body(path, body)
            if path == _CHAT:
                self._guard_context(prepared)
            self.last_used = time.monotonic()
            try:
                return self._request(path, prepared, timeout=240 if path == _CHAT else 60)
            finally:
                self.last_used = time.monotonic()

    @contextmanager
    def chat_completion(self, body, *, cancelled=None):
        with self._completion_lock(cancelled):
            self._start()
            self._cancelled(cancelled)
            prepared = self._body(_CHAT, body)
            self._guard_context(prepared, cancelled=cancelled)
            self.last_used = time.monotonic()
            try:
                with self._response(_CHAT, prepared, timeout=240, cancelled=cancelled) as response:
                    if not 200 <= response.status < 300:
                        self._status_error(response)
                    yield response
            finally:
                self.last_used = time.monotonic()

    def stats(self):
        return {"enabled": self.enabled, "name": self.config.get("name", self.config.get("model_id")),
                "backend": "openai", "unmanaged": True, "pid": None, "supervisor_pid": None,
                "worker_pids": [], "model_rss_bytes": 0, "tree_rss_bytes": 0,
                "peak_tree_rss_bytes": 0, "reported_buffers_bytes": 0,
                "peak_charged_bytes": 0, "context": self.context_size, "parallel": 1,
                "accounting": {"processes": [], "rss_bytes": 0, "charged_bytes": 0}}

    def reap_idle(self):
        # Idle management cannot stop a service owned by the user/provider.
        pass

    def close(self):
        with self._sockets_lock:
            # Keep close() atomic with raw-to-TLS ownership transfer.
            for upstream in self._sockets:
                self._abort(upstream)

    def shutdown(self):
        self.shutdown_event.set()
        self.close()
