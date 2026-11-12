"""Enforce whole-request HTTP deadlines with stateless, disposable workers."""

import atexit
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import http.client
import ipaddress
import json
import os
import platform
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class WorkerScope:
    def __init__(self):
        self.slots = threading.BoundedSemaphore(4)
        self.lock = threading.Lock()
        self.closed = False
        self.processes = set()

    @contextmanager
    def activate(self):
        token = _CURRENT_SCOPE.set(self)
        try:
            yield
        finally:
            _CURRENT_SCOPE.reset(token)

    def spawn(self, args):
        with self.lock:
            if self.closed:
                raise OSError("HTTP transport has shut down")
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.processes.add(process)
            return process

    def discard(self, process):
        with self.lock:
            self.processes.discard(process)

    def shutdown(self):
        with self.lock:
            self.closed = True
            processes = list(self.processes)
        for process in processes:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        for process in processes:
            process.wait(timeout=2)


_DEFAULT_SCOPE = WorkerScope()
_CURRENT_SCOPE = ContextVar("funny_http_scope", default=_DEFAULT_SCOPE)
_HTTP_SLOTS = _DEFAULT_SCOPE.slots
atexit.register(_DEFAULT_SCOPE.shutdown)


MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024


class IncompleteResponse(OSError):
    pass


def validate_url(value):
    if any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError("HTTP URL cannot contain whitespace or control characters")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only explicit http:// or https:// URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials in URLs are not supported")
    if parsed.fragment:
        raise ValueError("URL fragments are not network request arguments")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid HTTP port") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid HTTP port")
    return parsed


def _public_ip(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.is_site_local or ip.sixtofour or ip.teredo):
        return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def validate_public_url(value):
    parsed = validate_url(value)
    host = parsed.hostname.rstrip(".")
    if not host or ("." not in host and ":" not in host) or host.endswith((".localhost", ".local")) or "%" in host:
        raise ValueError("Для наблюдения нужен публичный сайт.")
    try:
        is_public = _public_ip(host)
    except ValueError:
        if ":" in host:
            raise ValueError("Некорректный адрес сайта.") from None
        try:
            socket.inet_aton(host)
        except OSError:
            pass
        else:
            raise ValueError("IPv4-адрес сайта нужен в стандартной записи.")
    else:
        if not is_public:
            raise ValueError("Для наблюдения нужен публичный сайт.")
    return parsed


def _connect_public(address, timeout, source_address=None):
    # Use the resolved socket addresses directly. Resolving again after the
    # public-address check would allow a DNS change to point to a local host.
    addresses = socket.getaddrinfo(*address, type=socket.SOCK_STREAM)
    if not addresses or any(not _public_ip(info[4][0]) for info in addresses):
        raise PermissionError("Host resolved to a non-public address")
    last_error = None
    for family, kind, protocol, _, target in addresses:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(target)
            return sock
        except OSError as exc:
            sock.close()
            last_error = exc
    raise last_error


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(_PublicHTTPConnection, request)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_PublicHTTPSConnection, request, context=self._context)


def _origin(value):
    parsed = validate_url(value)
    return parsed.scheme.lower(), parsed.hostname.lower(), parsed.port or (443 if parsed.scheme.lower() == "https" else 80)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def bounded_request(args, limits):
    """Execute once; timeout kills the worker, never retries the request."""
    timeout = max(0.1, min(limits["timeout_seconds"], 5))
    deadline = time.monotonic() + timeout
    scope = _CURRENT_SCOPE.get()
    with scope.lock:
        if scope.closed:
            raise OSError("HTTP transport has shut down")
    if not scope.slots.acquire(timeout=timeout):
        raise TimeoutError("HTTP worker capacity deadline exceeded")
    try:
        return _run_once(args, limits, deadline, scope)
    finally:
        scope.slots.release()


def _run_once(args, limits, deadline, scope):
    payload = json.dumps({"args": args, "limits": limits, "deadline": deadline, "owner_pid": os.getpid()}, ensure_ascii=False).encode()
    process = scope.spawn([sys.executable, "-I", str(Path(__file__).resolve())])
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP deadline exceeded")
        try:
            output, _ = process.communicate(payload, timeout=remaining)
        except subprocess.TimeoutExpired:
            raise TimeoutError("HTTP deadline exceeded") from None
        if process.returncode == 124:
            raise TimeoutError("HTTP deadline exceeded")
        if limits.get("binary_download") is True:
            output_limit = 4 * ((MAX_DOWNLOAD_BYTES + 2) // 3) + 16384
        else:
            output_limit = 4 * 1024 * 1024 if limits.get("public_web") is True else 1024 * 1024
        if process.returncode or len(output) > output_limit:
            raise OSError("HTTP worker failed")
        response = json.loads(output)
        if "error" in response:
            if response["error"] in {"IncompleteResponse", "IncompleteRead"}:
                raise IncompleteResponse("Incomplete HTTP response")
            exception = {"TimeoutError": TimeoutError, "PermissionError": PermissionError, "ValueError": ValueError}.get(response["error"], OSError)
            raise exception("HTTP request failed")
        return response["result"]
    finally:
        if process.poll() is None:
            process.kill()
        # communicate closes all pipes and waits, including cancellation by
        # KeyboardInterrupt. No blocked request thread survives a timeout.
        try:
            process.communicate()
        finally:
            scope.discard(process)


def _response_length(response):
    encodings = response.headers.get_all("Transfer-Encoding", [])
    lengths = response.headers.get_all("Content-Length", [])
    if encodings and (lengths or len(encodings) != 1 or not response.chunked):
        raise IncompleteResponse("Ambiguous transfer encoding")
    if not lengths:
        return None
    value = lengths[0].strip()
    if len(lengths) != 1 or len(value) > 20 or not value.isascii() or not value.isdecimal():
        raise IncompleteResponse("Invalid content length")
    return int(value)


def _text_body(response, raw):
    if raw:
        content_encodings = response.headers.get_all("Content-Encoding", [])
        if any(encoding.strip().lower() not in {"", "identity"} for encoding in content_encodings):
            return {"body": "", "body_omitted": "content_encoding"}
        media = response.headers.get_content_type()
        if (media in {"application/octet-stream", "application/pdf", "application/zip", "application/gzip"}
                or media.split("/", 1)[0] in {"audio", "font", "image", "video"} and not media.endswith("+xml")):
            return {"body": "", "body_omitted": "binary"}
    charset = response.headers.get_content_charset() or "utf-8"
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        charset, text = "utf-8", raw.decode("utf-8", errors="replace")
    if "\0" in text:
        return {"body": "", "body_omitted": "binary"}
    return {"body": text, "encoding": charset}


def _request(args, limits, deadline):
    public = limits.get("public_web") is True
    binary = limits.get("binary_download") is True
    if binary and (args["method"] != "GET" or args["body"]):
        raise ValueError("File downloads are GET-only")
    cap = MAX_DOWNLOAD_BYTES if binary else 512 * 1024 if public else 65536
    maximum = max(0, min(limits["response_bytes"], cap))
    max_redirects = max(0, min(limits["same_origin_redirects"], 2))
    origin = _origin(args["url"])
    current = args["url"]
    ca = "/etc/ssl/cert.pem" if platform.system() == "Darwin" and Path("/etc/ssl/cert.pem").exists() else None
    context = ssl.create_default_context(cafile=ca)
    handlers = [_PublicHTTPHandler(), _PublicHTTPSHandler(context=context)] if public else [urllib.request.HTTPSHandler(context=context)]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), *handlers)
    for redirects in range(max_redirects + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP deadline exceeded")
        if public:
            validate_public_url(current)
            if args["method"] != "GET" or args["body"]:
                raise ValueError("Public watch requests are GET-only")
        body = args["body"].encode() if args["body"] else None
        accept = "*/*" if binary else "text/html, application/xhtml+xml;q=0.9" if public else "application/json, text/plain;q=0.9, */*;q=0.1"
        headers = {"Accept": accept, "User-Agent": "Handy/0.1"}
        if body is not None:
            headers["Content-Type"] = args["content_type"] + "; charset=utf-8"
        request = urllib.request.Request(current, data=body, headers=headers, method=args["method"])
        try:
            response = opener.open(request, timeout=remaining)
        except urllib.error.HTTPError as exc:
            response = exc
        except urllib.error.URLError as exc:
            if public and isinstance(exc.reason, PermissionError):
                raise exc.reason from None
            raise
        with response:
            status = response.code
            location = response.headers.get("Location")
            if status in {301, 302, 303, 307, 308} and location and args["method"] in {"GET", "HEAD"}:
                next_url = urllib.parse.urljoin(current, location)
                if _origin(next_url) != origin:
                    raise PermissionError("Cross-origin redirects are not authorized")
                if redirects >= max_redirects:
                    raise ValueError("HTTP redirect budget exceeded")
                current = next_url
                continue
            metadata = {"method": args["method"], "url": current, "status": status, "http_ok": 200 <= status < 300,
                        "content_type": response.headers.get("Content-Type", "")[:128], "redirects": redirects}
            if binary and (not metadata["http_ok"] or status == 206 or response.headers.get("Content-Range") is not None):
                return {**metadata, "http_ok": False, "truncated": False}
            length = _response_length(response)
            if binary and length is not None and length > maximum:
                return {**metadata, "truncated": True}
            chunks, total, incomplete = [], 0, False
            while total <= maximum and args["method"] != "HEAD":
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("HTTP deadline exceeded")
                sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(remaining)
                reader = getattr(response, "read1", response.read)
                try:
                    chunk = reader(min(4096, maximum + 1 - total))
                except http.client.IncompleteRead as exc:
                    if public or binary:
                        raise
                    chunks.append(exc.partial)
                    incomplete = True
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            raw = b"".join(chunks)
            truncated = len(raw) > maximum
            incomplete = incomplete or (not truncated and getattr(response, "length", None) not in {None, 0})
            if public and incomplete:
                raise IncompleteResponse("Incomplete HTTP response")
            if binary:
                if not truncated and length is not None and len(raw) != length:
                    raise IncompleteResponse("Incomplete HTTP response")
                if truncated:
                    return {**metadata, "truncated": True}
                return {**metadata, "truncated": False, "bytes_read": len(raw),
                        "body_base64": base64.b64encode(raw).decode("ascii")}
            raw = raw[:maximum]
            if incomplete:
                metadata["incomplete"] = True
            partial = truncated or incomplete or status == 206 or response.headers.get("Content-Range") is not None
            return {**metadata, **_text_body(response, raw), "bytes_read": len(raw), "truncated": partial}
    raise ValueError("HTTP redirect budget exceeded")


def _main():
    finished = threading.Event()
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("HTTP worker input exceeds budget")
        payload = json.loads(raw)
        owner = payload["owner_pid"]
        if type(owner) is not int or owner <= 0:
            raise ValueError("Invalid HTTP owner")
        def watch_owner():
            while not finished.wait(0.05):
                if os.getppid() != owner:
                    os._exit(125)
                if time.monotonic() >= payload["deadline"]:
                    os._exit(124)
        threading.Thread(target=watch_owner, daemon=True).start()
        result = {"result": _request(payload["args"], payload["limits"], payload["deadline"])}
    except Exception as exc:
        result = {"error": type(exc).__name__}
    try:
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        sys.stdout.flush()
    finally:
        finished.set()


if __name__ == "__main__":
    _main()
