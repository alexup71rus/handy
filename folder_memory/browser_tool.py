"""Small, read-oriented bridge to an already running user Chrome.

The model chooses when to use it. Page content is evidence, never instructions;
the only evaluated script is the bundled DOM reader, not caller supplied code.
"""

import json
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse


class BrowserError(ValueError):
    pass


_STALE = "Список вкладок устарел после переподключения. Обнови список и выбери вкладку заново."
_DIALOG = "Вкладка ждёт ответа на диалог. Закрой его в Chrome и повтори запрос."
_CONNECT = ("Нет соединения с открытым Chrome. Разреши подключение «Allow remote debugging», "
            "если появилось окно, или проверь chrome://inspect/#remote-debugging и повтори запрос.")
_TOOLS = {"list_pages", "evaluate_script", "new_page"}


def canonical_url(value):
    """Accept web URLs only, without embedded credentials or control characters."""
    if (not isinstance(value, str) or not value or len(value) > 4096
            or "\\" in value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise BrowserError("Нужна полная HTTP/HTTPS ссылка без пробелов.")
    try:
        parts = urllib.parse.urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None):
            raise ValueError()
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        host = parts.hostname.encode("idna").decode().lower()
        if ":" in host:
            host = "[" + host + "]"
        if port and port != (443 if parts.scheme == "https" else 80):
            host += ":" + str(port)
        safe = "/:!$&'()*+,;=@%[]-._~"
        return urllib.parse.urlunsplit((parts.scheme, host, urllib.parse.quote(parts.path or "/", safe=safe),
                                       urllib.parse.quote(parts.query, safe=safe + "?"),
                                       urllib.parse.quote(parts.fragment, safe=safe + "?#")))
    except (ValueError, UnicodeError):
        raise BrowserError("Нужна корректная HTTP/HTTPS ссылка без логина и пароля.") from None


class McpClient:
    """One supervised stdio connection; the App owns and reaps its lifetime."""

    def __init__(self, config, state_dir):
        self.config, self.state = config, Path(state_dir)
        self.process = self.reader = None
        self.lock = threading.RLock()
        self.responses = queue.Queue(maxsize=32)
        self.next_id, self.last_used = 0, 0
        self.tools, self.connection_id = set(), ""
        self.ready = self.closed = False
        self.first_tool_call = True

    @staticmethod
    def _read(stream, responses):
        try:
            while line := stream.readline(1024 * 1024 + 1):
                if len(line) > 1024 * 1024:
                    break
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(value, dict):
                    break
                responses.put(value, timeout=1)
        except (OSError, ValueError, queue.Full):
            pass
        finally:
            try:
                responses.put_nowait({"_closed": True})
            except queue.Full:
                pass

    def _send(self, value):
        self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
        self.process.stdin.flush()

    def _request(self, method, params):
        self.next_id += 1
        identity = self.next_id
        self._send({"jsonrpc": "2.0", "id": identity, "method": method, "params": params})
        timeout = min(max(float(self.config.get("timeout_seconds", 15)), 0.1), 15)
        if method == "tools/call" and self.first_tool_call:
            timeout = min(max(float(self.config.get("connect_timeout_seconds", 30)), 0.1), 30)
            self.first_tool_call = False
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserError("Chrome не ответил вовремя. " + _CONNECT)
            try:
                item = self.responses.get(timeout=remaining)
            except queue.Empty:
                continue
            if item.get("_closed"):
                raise BrowserError("Chrome MCP завершился. " + _CONNECT)
            if item.get("id") != identity:
                if item.get("method") and "id" in item:
                    self._send({"jsonrpc": "2.0", "id": item["id"], "error": {
                        "code": -32601, "message": "Unsupported client method"}})
                continue
            if item.get("error"):
                raise BrowserError("Chrome MCP отклонил запрос. " + _CONNECT)
            result = item.get("result")
            if not isinstance(result, dict):
                raise BrowserError("Некорректный результат Chrome MCP.")
            self.last_used = time.monotonic()
            return result

    def start(self):
        with self.lock:
            if self.closed:
                raise BrowserError("Соединение с Chrome остановлено вместе с Handy.")
            if self.ready and self.process and self.process.poll() is None:
                return
            self.close()
            if (not self.config.get("enabled") or not Path(self.config.get("node", "")).is_file()
                    or not Path(self.config.get("entry", "")).is_file()):
                raise BrowserError("Браузер не настроен: нужен включённый browser.json с локальным Chrome MCP.")
            command = [self.config["node"], self.config["entry"], "--autoConnect", "--no-usage-statistics",
                       "--no-performance-crux", "--no-source-maps", "--no-category-input",
                       "--no-category-emulation", "--no-category-performance", "--no-category-network",
                       "--no-category-memory", "--experimentalStructuredContent", "--no-file-navigations"]
            supervisor = [sys.executable, "-I", str(Path(__file__).with_name("model_supervisor.py")),
                          str(os.getpid()), "--stdio", *command]
            env = {**os.environ, "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1",
                   "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1", "CHROME_DEVTOOLS_MCP_NO_CONFIG_DISCOVERY": "1",
                   "DEBUG": ""}
            self.state.mkdir(parents=True, exist_ok=True)
            log = self.state / "browser-mcp.log"
            if log.exists() and log.stat().st_size > 128 * 1024:
                log.write_bytes(b"")
            try:
                with log.open("ab") as stream:
                    log.chmod(0o600)
                    self.process = subprocess.Popen(supervisor, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                    stderr=stream, env=env, start_new_session=True)
                self.responses = queue.Queue(maxsize=32)
                self.first_tool_call = True
                self.reader = threading.Thread(target=self._read, args=(self.process.stdout, self.responses), daemon=True)
                self.reader.start()
                result = self._request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                       "clientInfo": {"name": "handy-browser", "version": "0.1.0"}})
                if not isinstance(result.get("protocolVersion"), str) or not result["protocolVersion"]:
                    raise BrowserError("Chrome MCP не завершил согласование протокола.")
                self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                catalogue = self._request("tools/list", {}).get("tools")
                if (not isinstance(catalogue, list) or any(not isinstance(item, dict)
                        or not isinstance(item.get("name"), str) for item in catalogue)):
                    raise BrowserError("Chrome MCP не вернул каталог инструментов.")
                self.tools = {item["name"] for item in catalogue}
                self.connection_id, self.ready = secrets.token_hex(16), True
            except BaseException:
                self.close()
                raise

    def call(self, name, arguments, *, connection_id=""):
        if name not in _TOOLS:
            raise BrowserError("Этот инструмент браузера недоступен Handy.")
        with self.lock:
            try:
                if connection_id:
                    if (self.closed or connection_id != self.connection_id or not self.ready
                            or self.process is None or self.process.poll() is not None):
                        raise BrowserError(_STALE)
                else:
                    self.start()
                if name not in self.tools:
                    raise BrowserError("Установленный Chrome MCP не поддерживает нужную операцию.")
                result = self._request("tools/call", {"name": name, "arguments": arguments})
                if result.get("isError"):
                    if any(isinstance(item, dict) and "A dialog is open" in str(item.get("text", ""))
                           for item in result.get("content", [])):
                        raise BrowserError(_DIALOG)
                    raise BrowserError(_CONNECT)
                return {**result, "_connection_id": self.connection_id}
            except BrowserError:
                self.close()
                raise
            except (OSError, ValueError, TypeError):
                self.close()
                raise BrowserError(_CONNECT) from None

    def close(self, *, permanent=False):
        with self.lock:
            if permanent:
                self.closed = True
            process, reader = self.process, self.reader
            self.process = self.reader = None
            self.ready, self.connection_id = False, ""
            self.tools.clear()
            if process is None:
                return
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if reader and reader is not threading.current_thread():
                reader.join(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()

    def reap_idle(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            idle = min(max(float(self.config.get("idle_seconds", 60)), 1), 120)
            if self.process and time.monotonic() - self.last_used >= idle:
                self.close()
        finally:
            self.lock.release()


def _structured(result):
    value = result.get("structuredContent")
    if not isinstance(value, dict):
        raise BrowserError("Chrome MCP не вернул структурированные данные.")
    return value


def _page_id(page):
    value = page.get("id", page.get("pageId"))
    if type(value) is not int or value < 0:
        raise BrowserError("Chrome MCP вернул некорректный ID вкладки.")
    return str(value)


def _extractor(page_url, options):
    script = Path(__file__).with_name("browser_extract.js").read_text(encoding="utf-8")
    settings = {key: options[key] for key in ("selector", "query", "max_chars") if key in options}
    return "async () => {\nconst expected = " + json.dumps(page_url) + ";\n" + r'''
if (location.href !== expected) return {error: 'page_changed'};
if (document.readyState === 'loading') {
  await new Promise(resolve => {
    const done = () => { clearTimeout(timer); document.removeEventListener('DOMContentLoaded', done); resolve(); };
    const timer = setTimeout(done, 800);
    document.addEventListener('DOMContentLoaded', done, {once: true});
  });
  if (location.href !== expected) return {error: 'page_changed'};
  if (document.readyState === 'loading') return {error: 'page_loading'};
}
const read = ''' + script + ";\nconst result = read(" + json.dumps(settings) + r''');
if (location.href !== expected) return {error: 'page_changed'};
return result;
}'''


class BrowserTool:
    def __init__(self, state_dir, *, client=None):
        state = Path(state_dir)
        if client is None:
            config_file = state / "browser.json"
            if not config_file.exists():
                config_file = Path(__file__).resolve().parents[1] / ".runtime" / "browser.json"
            try:
                config = json.loads(config_file.read_text(encoding="utf-8")) if config_file.is_file() else {}
            except (OSError, ValueError):
                config = {}
            client = McpClient(config if isinstance(config, dict) else {}, state)
        self.client = client

    def reap_idle(self):
        self.client.reap_idle()

    def shutdown(self):
        self.client.close(permanent=True)

    def _pages(self, connection_id=""):
        result = self.client.call("list_pages", {}, connection_id=connection_id)
        pages = _structured(result).get("pages")
        if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
            raise BrowserError("Chrome MCP не вернул список вкладок.")
        clean = []
        for page in pages[:200]:
            try:
                canonical_url(page.get("url"))
            except BrowserError:
                continue
            clean.append({"tab_id": _page_id(page), "url": page["url"],
                          "title": str(page.get("title", ""))[:400], "selected": page.get("selected") is True})
        return pages, {"pages": clean, "connection_id": result.get("_connection_id", self.client.connection_id),
                       "total_pages": len(pages), "excluded_non_web_pages": len(pages[:200]) - len(clean),
                       "partial": len(pages) > 200, "browser": "existing_user_chrome"}

    @staticmethod
    def _options(options):
        if not isinstance(options, dict):
            raise ValueError("Параметры браузера должны быть объектом.")
        operation = options.get("operation")
        allowed = {"list": {"operation"}, "open": {"operation", "url"},
                   "read": {"operation", "url", "tab_id", "connection_id", "selector", "query", "max_chars"}}
        if not isinstance(operation, str) or operation not in allowed or set(options) - allowed[operation]:
            raise ValueError("Доступны операции браузера list, read и open с их параметрами.")
        options = dict(options)
        if "url" in options:
            canonical_url(options["url"])
        if operation == "open" and "url" not in options:
            raise ValueError("Укажи HTTP/HTTPS ссылку, которую нужно открыть.")
        if "tab_id" in options:
            tab_id = options["tab_id"]
            if type(tab_id) is int:
                tab_id = str(tab_id)
            if not isinstance(tab_id, str) or re.fullmatch(r"[0-9]{1,10}", tab_id) is None:
                raise ValueError("Нужен ID вкладки из списка Chrome.")
            options["tab_id"] = tab_id
        if "connection_id" in options and (not isinstance(options["connection_id"], str)
                or re.fullmatch(r"[a-f0-9]{32}", options["connection_id"]) is None):
            raise ValueError("Нужен connection_id из текущего списка вкладок.")
        for key, limit in (("query", 1000), ("selector", 1024)):
            if key in options and (not isinstance(options[key], str) or len(options[key]) > limit
                    or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in options[key])):
                raise ValueError("Некорректный " + key + " для чтения страницы.")
        if operation == "read":
            size = options.setdefault("max_chars", 12000)
            if type(size) is not int or not 2000 <= size <= 48000:
                raise ValueError("max_chars должен быть целым числом от 2000 до 48000.")
        return options

    def execute(self, options):
        options = self._options(options)
        try:
            operation = options["operation"]
            if operation == "list":
                _, data = self._pages()
                return {"status": "ok", "text": "Открытые вкладки Chrome: " + str(len(data["pages"])) + ".", "data": data}
            if operation == "open":
                return self._open(options["url"])
            return self._read(options)
        except BrowserError as error:
            if options["operation"] == "open":
                return {"status": "error", "text": str(error) + " Открытие не подтверждено; проверь список вкладок перед повтором.",
                        "data": {"code": "browser_open_unconfirmed", "opened": None}}
            return {"status": "error", "text": str(error), "data": {"code": "browser_unavailable", "performed": False}}

    def _read(self, options):
        _, listed = self._pages(options.get("connection_id", ""))
        wanted, tab_id = options.get("url"), options.get("tab_id")
        selected = [page for page in listed["pages"] if (not tab_id or page["tab_id"] == tab_id)
                    and (not wanted or canonical_url(page["url"]) == canonical_url(wanted))]
        if len(selected) != 1:
            choices = selected or listed["pages"]
            text = ("Найдено несколько подходящих вкладок. Выбери ID нужной." if selected
                    else "Такой открытой страницы нет. Можно открыть ссылку или выбрать вкладку из списка.")
            return {"status": "needs_input", "text": text, "data": {"code": "browser_tab_choice",
                    "pages": choices[:20], "partial": len(choices) > 20 or listed["partial"],
                    "connection_id": listed["connection_id"]}}
        page = selected[0]
        response = _structured(self.client.call("evaluate_script", {
            "pageId": int(page["tab_id"]), "function": _extractor(page["url"], options),
            "waitForStableDom": False, "dialogAction": "",
        }, connection_id=listed["connection_id"]))
        if response.get("dialog"):
            raise BrowserError(_DIALOG)
        data = response.get("result")
        if data is None:
            match = re.fullmatch(r"Script ran on page and returned:\s*```json\n([\s\S]+)\n```\s*", response.get("message", ""))
            if match:
                data = match.group(1)
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                pass
        if not isinstance(data, dict):
            raise BrowserError("Chrome MCP не вернул структуру страницы.")
        if data.get("error"):
            messages = {"page_changed": "Вкладка сменила адрес. Обнови список и повтори чтение.",
                        "page_loading": "Страница ещё загружается. Повтори чтение через несколько секунд.",
                        "invalid_selector": "Не удалось разобрать CSS-селектор или XPath. Проверь путь к элементу.",
                        "selector_missing": "На странице не найден указанный элемент. Уточни селектор или прочитай всю страницу.",
                        "selector_ambiguous": "Селектор подходит нескольким элементам. Уточни его, например добавь ID или родительский блок."}
            return {"status": "needs_input", "text": messages.get(data["error"], "Не удалось прочитать этот блок."),
                    "data": {"code": data["error"], "matches": data.get("matches", 0), "page": page}}
        observed = data.get("page")
        if (not isinstance(observed, dict) or canonical_url(observed.get("url")) != canonical_url(page["url"])
                or not isinstance(data.get("sections"), list) or not isinstance(data.get("tables"), list)
                or not isinstance(data.get("links"), list) or not isinstance(data.get("truncation"), dict)):
            raise BrowserError("Chrome MCP не вернул ожидаемую структуру страницы.")
        try:
            if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > options["max_chars"] * 4 + 4096:
                raise BrowserError("Структура страницы превысила лимит чтения.")
        except UnicodeEncodeError:
            raise BrowserError("Страница содержит некорректный Unicode.") from None
        observed.update({"tab_id": page["tab_id"], "observed_at": time.time(), "observation_id": secrets.token_hex(8)})
        data.update({"connection_id": listed["connection_id"], "content_is_untrusted": True,
                     "source_ids_scope": "this_observation"})
        partial = any(data["truncation"].values())
        text = "Прочитана структура страницы: " + str(observed.get("title", page["title"]))[:200] + "."
        if partial:
            text += " Часть содержимого сокращена; нужный блок можно прочитать отдельно по source.selector."
        return {"status": "ok", "text": text, "data": data}

    def _open(self, url):
        pages, listed = self._pages()
        before = {_page_id(page) for page in pages}
        response = self.client.call("new_page", {"url": url, "background": True, "timeout": 10000},
                                    connection_id=listed["connection_id"])
        data = response.get("structuredContent")
        after = data.get("pages") if isinstance(data, dict) else None
        if not isinstance(after, list) or any(not isinstance(page, dict) for page in after):
            after, _ = self._pages(listed["connection_id"])
        opened = [page for page in after if _page_id(page) not in before]
        if len(opened) != 1:
            return {"status": "needs_input", "text": "Запрос открытия отправлен, но новую вкладку нельзя определить однозначно. Проверь список вкладок.",
                    "data": {"code": "browser_open_ambiguous", "opened": None, "connection_id": listed["connection_id"]}}
        page = opened[0]
        canonical_url(page.get("url"))
        return {"status": "ok", "text": "Открыта вкладка в пользовательском Chrome.", "data": {
            "opened": True, "requested_url": url, "url": page["url"], "title": str(page.get("title", ""))[:400],
            "tab_id": _page_id(page), "redirected": canonical_url(page["url"]) != canonical_url(url),
            "connection_id": listed["connection_id"]}}
