"""Supervise the local llama.cpp chat and embedding workers."""

import http.client
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import weakref
from contextlib import closing, contextmanager
from pathlib import Path
from .memory_budget import tree_memory, descendant_pids


_DEFAULT_CONTEXT_SIZE = 4096
_MAX_CHAT_CONTEXT_SIZE = 65536
_IDLE_DEFAULTS = {"idle_sleep_seconds": 60, "idle_terminate_seconds": 120}
_CHAT_COMPLETIONS = "/v1/chat/completions"
CHAT_MODEL = "memory-engine"


class NeuralError(ValueError):
    pass


class ResponseLengthError(NeuralError):
    pass


class ContextBudgetError(NeuralError):
    def __init__(self, input_tokens, max_tokens, context_size):
        self.input_tokens = input_tokens
        self.max_tokens = max_tokens
        self.context_size = context_size
        super().__init__(f"Запрос занимает {input_tokens} токенов, для ответа нужно ещё {max_tokens}. "
                         f"Окно модели вмещает {context_size} токенов. Сократи запрос или выбери фрагмент.")


def check_schema(value, schema):
    """Independent validation of the small JSON-Schema subset we send."""
    if "oneOf" in schema:
        matches = 0
        for candidate in schema["oneOf"]:
            try:
                check_schema(value, candidate)
                matches += 1
            except NeuralError:
                pass
        if matches != 1:
            raise NeuralError("Результат не соответствует ни одному смысловому фрейму.")
        return
    if "const" in schema and value != schema["const"]:
        raise NeuralError("Недопустимое значение смыслового фрейма.")
    kind = schema.get("type")
    checks = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list), "string": lambda v: isinstance(v, str), "integer": lambda v: type(v) is int}
    if kind in checks and not checks[kind](value):
        raise NeuralError("Результат не соответствует типу схемы.")
    if "enum" in schema and value not in schema["enum"]:
        raise NeuralError("Результат вне каталога допустимых значений.")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value) or schema.get("additionalProperties") is False and set(value) - set(properties):
            raise NeuralError("Результат не соответствует полям схемы.")
        for key, item in value.items():
            if key in properties:
                check_schema(item, properties[key])
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            raise NeuralError("Число элементов не соответствует схеме.")
        for item in value:
            check_schema(item, schema.get("items", {}))
    if type(value) is int and (value < schema.get("minimum", value) or value > schema.get("maximum", value)):
        raise NeuralError("Результат вне диапазона схемы.")
    if isinstance(value, str) and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
        raise NeuralError("Длина текста не соответствует схеме.")


class LocalModel:
    instances = weakref.WeakSet()
    instances_lock = threading.Lock()

    def __init__(self, state_dir, *, profile=None, config=None):
        self.state = Path(state_dir).resolve()
        self.profile = profile
        path = self.state / "neural.json"
        if config is None:
            config = json.loads(path.read_text()) if path.exists() else {}
        self.config = config
        if profile:
            idle = {key: config[key] for key in _IDLE_DEFAULTS if key in config}
            self.config = {**idle, **config.get(profile, {})}
        self.process = None
        self.shutdown_event = threading.Event()
        self.last_used = 0
        self.peak_tree_rss = 0
        self.peak_charged = 0
        with self.instances_lock:
            peer = next((instance for instance in self.instances if instance.state == self.state), None)
            self.lock = peer.lock if peer else threading.RLock()
            self.instances.add(self)

    @property
    def enabled(self):
        return bool(self.config.get("enabled")
                    and all(Path(self.config.get(key, "")).is_file() for key in ("binary", "model")))

    @property
    def context_size(self):
        try:
            context = int(self.config.get("context", _DEFAULT_CONTEXT_SIZE))
        except (TypeError, ValueError, OverflowError) as exc:
            raise NeuralError("Размер контекста должен быть положительным числом токенов.") from exc
        if context <= 0:
            raise NeuralError("Размер контекста должен быть положительным числом токенов.")
        main_chat = self.profile is None and self.config.get("embedding") is not True
        limit = _MAX_CHAT_CONTEXT_SIZE if main_chat else _DEFAULT_CONTEXT_SIZE
        return min(context, limit)

    @property
    def log_path(self):
        if self.config.get("embedding") is True:
            return self.state / "embedding-llama.log"
        return self.state / "llama.log"

    def _idle_seconds(self):
        sleep, terminate = (self.config.get(key, default) for key, default in _IDLE_DEFAULTS.items())
        if type(sleep) is not int or not (sleep == -1 or 1 <= sleep < 2**31):
            raise NeuralError("idle_sleep_seconds должен быть положительным целым числом или -1.")
        if type(terminate) not in (int, float) or not 0 <= terminate < math.inf:
            raise NeuralError("idle_terminate_seconds должен быть неотрицательным числом.")
        return sleep, terminate

    def _cache_args(self):
        if self.profile is not None or self.config.get("embedding") is True:
            return []
        args = []
        for key, flag, choices in (
            ("cache_type_k", "-ctk", ("f16", "q8_0")),
            ("cache_type_v", "-ctv", ("f16", "q8_0")),
            ("flash_attention", "-fa", ("on", "off", "auto")),
        ):
            if key not in self.config:
                continue
            value = self.config[key]
            if not isinstance(value, str) or value not in choices:
                raise NeuralError(f"{key}: допустимые значения — {', '.join(choices)}.")
            args += [flag, value]
        return args

    @contextmanager
    def _response(self, path, body=None, timeout=30, *, cancelled=None):
        if self.shutdown_event.is_set():
            raise NeuralError("Языковой адаптер остановлен.")
        if cancelled is not None and cancelled.is_set():
            raise NeuralError("Запрос модели отменён.")
        token = (self.state / "token").read_text().strip()
        port = int(self.config.get("port", 18747))
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        # A model request may carry local data. Connect directly, with no
        # environment proxy, redirect, or caller-controlled worker headers.
        with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)) as connection:
            finished, watcher = threading.Event(), None
            try:
                if cancelled is not None:
                    connection.connect()
                    upstream_socket = connection.sock

                    def abort_cancelled_request():
                        while not finished.wait(.05):
                            if cancelled.is_set():
                                # Pin this request's socket: HTTPConnection.sock can
                                # become None when response headers say Connection: close.
                                try:
                                    upstream_socket.shutdown(socket.SHUT_RDWR)
                                except OSError:
                                    pass
                                return

                    watcher = threading.Thread(target=abort_cancelled_request,
                                               name="handy-upstream-cancel", daemon=True)
                    watcher.start()
                    if cancelled.is_set():
                        raise NeuralError("Запрос модели отменён.")
                connection.request("POST" if body is not None else "GET", path, body=data, headers=headers)
                with connection.getresponse() as response:
                    if cancelled is not None and cancelled.is_set():
                        raise NeuralError("Запрос модели отменён.")
                    yield response
            finally:
                finished.set()
                if watcher is not None:
                    watcher.join(timeout=.3)

    def _request(self, path, body=None, timeout=30):
        try:
            with self._response(path, body, timeout) as response:
                status = response.status
                if 200 <= status < 300:
                    return json.loads(response.read(1024 * 1024))
                detail = response.read(16384).decode("utf-8", errors="replace").lower() if status >= 400 else ""
        except NeuralError:
            raise
        except TimeoutError as exc:
            if path != "/health":
                self.close()
            raise NeuralError("Истекло время ответа языкового адаптера.") from exc
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise NeuralError("Языковой адаптер недоступен: " + type(exc).__name__) from exc
        context = any(marker in detail for marker in ["context", "n_ctx", "too many tokens"])
        limit = any(marker in detail for marker in ["exceed", "too long", "too large", "maximum", "n_ctx", "too many tokens"])
        if context and limit:
            reason = "превышен размер контекста"
        elif any(marker in detail for marker in ["batch", "n_tokens <= n_"]) and any(marker in detail for marker in ["too small", "exceed", "larger than", "n_tokens <= n_", "must be at least"]):
            reason = "недостаточный размер batch"
        else:
            reason = "модель отклонила запрос"
        raise NeuralError(f"Языковой адаптер: HTTP {status} — {reason}.")

    def _start(self):
        with self.lock:
            if self.shutdown_event.is_set():
                raise NeuralError("Языковой адаптер остановлен.")
            try:
                self._start_locked()
            except BaseException:
                self.close()
                raise

    def _start_locked(self):
        if not self.enabled:
            raise NeuralError("Языковой адаптер не установлен или отключён.")
        with self.instances_lock:
            peers = [instance for instance in self.instances if instance is not self and instance.state == self.state]
        for peer in peers:
            if self.config.get("exclusive") or peer.config.get("exclusive"):
                peer.close()
        process = self.process
        if process:
            if process.poll() is None and process.stdin and not process.stdin.closed:
                return
            self.close()
        context = self.context_size
        long_context = context > _DEFAULT_CONTEXT_SIZE
        sleep, _ = self._idle_seconds()
        cache_args = self._cache_args()
        multimodal_args = []
        if 'mmproj' in self.config:
            projector = self.config['mmproj']
            if (self.config.get('embedding') is True or not isinstance(projector, str)
                    or not Path(projector).is_file()):
                raise NeuralError('mmproj должен указывать на существующий vision-проектор основной модели.')
            multimodal_args = ['--mmproj', projector]
            if 'image_max_tokens' in self.config:
                limit = self.config['image_max_tokens']
                if type(limit) is not int or not 1 <= limit <= 8192:
                    raise NeuralError('image_max_tokens должен быть целым числом от 1 до 8192.')
                multimodal_args += ['--image-max-tokens', str(limit)]
        args = [self.config["binary"], "-m", self.config["model"], "--host", "127.0.0.1", "--port", str(int(self.config.get("port", 18747))), "--api-key-file", str(self.state / "token"), "-c", str(context), "-np", "1", "-ngl", "99", "--fit", "off", "--sleep-idle-seconds", str(sleep), "--no-webui"]
        args += cache_args + multimodal_args
        if self.config.get("embedding") is True:
            batch = int(self.config.get("batch_tokens", 512))
            micro_batch = int(self.config.get("micro_batch_tokens", 512))
            pooling=self.config.get('pooling','mean')
            if pooling not in {'mean','last','cls'}:raise NeuralError('Недопустимый pooling embedding-профиля.')
            args += ["--embedding", "--pooling", pooling, "--embd-normalize", "2", "-b", str(batch), "-ub", str(micro_batch), "-t", "4"]
        else:
            default_batch, default_micro_batch = (512, 256) if long_context else (128, 64)
            batch = int(self.config.get("batch_tokens", default_batch))
            micro_batch = int(self.config.get("micro_batch_tokens", default_micro_batch))
            args += ["-b", str(batch), "-ub", str(micro_batch), "-t", "6", "--jinja", "--alias", CHAT_MODEL]
        if not 1 <= micro_batch <= batch <= 4096:
            raise NeuralError("Недопустимый размер batch языкового адаптера.")
        if long_context:
            args += ["--cache-ram", "0", "--ctx-checkpoints", "0", "--no-context-shift"]
        if self.config.get("template"):
            args += ["--chat-template-file", self.config["template"]]
        with self.log_path.open("ab") as output:
            os.chmod(self.log_path, 0o600)
            if self.shutdown_event.is_set():
                raise NeuralError("Языковой адаптер остановлен при запуске.")
            supervisor = [sys.executable, "-I", str(Path(__file__).with_name("model_supervisor.py")), str(os.getpid()), *args]
            self.process = subprocess.Popen(supervisor, stdin=subprocess.PIPE, stdout=output, stderr=output, start_new_session=True)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self.shutdown_event.is_set():
                raise NeuralError("Языковой адаптер остановлен при запуске.")
            if self.process.poll() is not None:
                raise NeuralError(f"llama.cpp завершился при запуске. См. .runtime/{self.log_path.name}.")
            try:
                health = self._request("/health", timeout=0.5)
            except NeuralError:
                time.sleep(0.1)
                continue
            if not isinstance(health, dict):
                raise NeuralError("Языковой адаптер вернул некорректный health-ответ.")
            if health.get("status") == "ok":
                if self.shutdown_event.is_set() or self.process.poll() is not None:
                    raise NeuralError("Языковой адаптер завершился при запуске.")
                return
            time.sleep(0.1)
        self.close()
        raise NeuralError("Истекло время запуска языкового адаптера.")

    def stats(self):
        process = self.process
        pid = process.pid if process and process.poll() is None else None
        model_pids = descendant_pids(pid) if pid else []
        with self.instances_lock:
            processes = [instance.process for instance in self.instances]
        pids = [process.pid for process in processes if process and process.poll() is None]
        accounting = tree_memory(list(dict.fromkeys([*descendant_pids(os.getpid()), *pids, *model_pids])))
        model_rss = sum(item["rss_bytes"] or 0 for item in accounting["processes"] if item["pid"] in model_pids)
        total = accounting["rss_bytes"]
        self.peak_tree_rss = max(self.peak_tree_rss, total)
        self.peak_charged = max(self.peak_charged, accounting.get("charged_bytes") or 0)
        # This is a conservative allocation report, not a Metal footprint meter.
        buffers = 0
        if self.log_path.exists():
            with self.log_path.open("rb") as stream:
                stream.seek(max(0, self.log_path.stat().st_size - 24000))
                text = stream.read().decode(errors="replace")
            values = re.findall(r"(?:model|KV|compute) buffer size\s*=\s*([\d.]+)\s*MiB", text)
            buffers = int(sum(float(n) for n in values) * 1024 ** 2)
        return {"enabled": self.enabled, "name": self.config.get("name"), "pid": pid, "supervisor_pid": pid, "worker_pids": [worker for worker in model_pids if worker != pid], "model_rss_bytes": model_rss, "tree_rss_bytes": total, "peak_tree_rss_bytes": self.peak_tree_rss, "reported_buffers_bytes": buffers, "peak_charged_bytes": self.peak_charged, "accounting": accounting, "context": self.context_size, "parallel": 1}

    def _generate(self, path, body):
        with self.lock:
            self._start()
            self.last_used = time.monotonic()
            timeout = 30
            if path == _CHAT_COMPLETIONS:
                # The pinned server counts the same templated prompt it will generate from.
                count = self._request(path + "/input_tokens", body)
                input_tokens = count.get("input_tokens") if isinstance(count, dict) else None
                if type(input_tokens) is not int or input_tokens <= 0:
                    raise NeuralError("Языковой адаптер не вернул размер запроса в токенах.")
                max_tokens = body["max_tokens"]
                if input_tokens + max_tokens > self.context_size:
                    raise ContextBudgetError(input_tokens, max_tokens, self.context_size)
                if input_tokens > 8192:
                    timeout = min(240, 30 + input_tokens // 400 + max_tokens // 20)
            try:
                result = self._request(path, body, timeout=timeout)
            except NeuralError:
                if timeout > 30:
                    self.close()
                raise
            self.last_used = time.monotonic()
            return result

    @contextmanager
    def _completion_lock(self, cancelled):
        if cancelled is None:
            with self.lock:
                yield
            return
        while True:
            if cancelled.is_set():
                raise NeuralError("Запрос модели отменён.")
            if self.lock.acquire(timeout=.05):
                break
        try:
            if cancelled.is_set():
                raise NeuralError("Запрос модели отменён.")
            yield
        finally:
            self.lock.release()

    @contextmanager
    def chat_completion(self, body, *, cancelled=None):
        """Keep the supervised worker alive for the entire native response."""
        with self._completion_lock(cancelled):
            self._start()
            if cancelled is not None and cancelled.is_set():
                raise NeuralError("Запрос модели отменён.")
            self.last_used = time.monotonic()
            try:
                with self._response(_CHAT_COMPLETIONS, body, timeout=240, cancelled=cancelled) as response:
                    yield response
            finally:
                self.last_used = time.monotonic()

    def input_tokens(self, body):
        """Count the exact chat template used by the supervised llama server."""
        with self.lock:
            self._start()
            self.last_used = time.monotonic()
            result = self._request(_CHAT_COMPLETIONS + '/input_tokens', body)
            count = result.get('input_tokens') if isinstance(result, dict) else None
            if type(count) is not int or count < 1:
                raise NeuralError('Не удалось определить размер контекста.')
            return {'input_tokens': count, 'context_size': self.context_size}

    def _chat_message(self, messages, max_tokens, *, response_format=None, tools=None):
        if type(max_tokens) is not int or max_tokens <= 0:
            raise NeuralError("Лимит ответа должен быть положительным числом токенов.")
        body = {
            "model": self.config.get("name", "local"),
            "messages": messages,
            "temperature": 0,
            "seed": 42,
            "max_tokens": min(max_tokens, 1536),
            "cache_prompt": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if response_format is not None:
            body["response_format"] = response_format
        if tools:
            body.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        result = self._generate(_CHAT_COMPLETIONS, body)
        try:
            choice = result["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise NeuralError("Модель не вернула текст ответа.") from exc
        if choice.get("finish_reason") == "length":
            raise ResponseLengthError("Ответ модели превысил лимит длины.")
        if not isinstance(message, dict):
            raise NeuralError("Модель не вернула сообщение.")
        return message

    def complete_text(self, messages, max_tokens, *, response_format=None):
        content = self._chat_message(messages, max_tokens, response_format=response_format).get('content')
        if not isinstance(content, str):
            raise NeuralError("Модель не вернула текст ответа.")
        return content

    def complete_tool(self, messages, tools, *, max_tokens=320):
        """Return one actual function call, leaving its arguments untrusted."""
        message = self._chat_message(messages, max_tokens, tools=tools)
        calls = message.get('tool_calls')
        if not calls:
            return None
        try:
            [call] = calls
            function = call['function']
            name = function['name']
            arguments = json.loads(function['arguments'])
        except (KeyError, ValueError, TypeError) as exc:
            raise NeuralError('Модель вернула некорректный вызов инструмента.') from exc
        if (call.get('type') != 'function' or not isinstance(name, str)
                or name not in {tool['function']['name'] for tool in tools} or not isinstance(arguments, dict)):
            raise NeuralError('Модель предложила неизвестный инструмент или аргументы.')
        return name, arguments

    def complete(self, messages, schema, max_tokens=400):
        response_format = {"type": "json_schema", "json_schema": {
            "name": "funny_frame", "strict": True, "schema": schema,
        }}
        content = self.complete_text(messages, min(max_tokens, 600), response_format=response_format)
        try:
            value = json.loads(content)
        except ValueError as exc:
            raise NeuralError("Модель не вернула полный структурированный результат.") from exc
        check_schema(value, schema)
        return value

    def reap_idle(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.process and time.monotonic() - self.last_used >= self._idle_seconds()[1]:
                self.close()
        finally:
            self.lock.release()

    @staticmethod
    def _stop_process(process):
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)

    def close(self):
        with self.lock:
            process = self.process
            if process:
                try:
                    self._stop_process(process)
                finally:
                    if process.poll() is not None:
                        self.process = None

    def shutdown(self):
        self.shutdown_event.set()
        process = self.process
        if process and process.stdin and not process.stdin.closed:
            process.stdin.close()
        if process and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        if self.lock.acquire(timeout=2):
            try:
                self.close()
            finally:
                self.lock.release()
        elif process:
            self._stop_process(process)
            if self.process is process and process.poll() is not None:
                self.process = None
