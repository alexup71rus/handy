"""Extract only the existing PDF text layer in a bounded, isolated process."""

import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryFile
from threading import BoundedSemaphore
import time


MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 80
MAX_TEXT_BYTES = 256 * 1024
_MEMORY_BYTES = 384 * 1024 * 1024
_TIMEOUT = 5
_SLOT = BoundedSemaphore(1)
_BAD, _ENCRYPTED, _PAGES, _SIZE, _BACKEND = range(65, 70)
_MESSAGES = {
    _BAD: "Структура PDF повреждена или документ не читается.",
    _ENCRYPTED: "PDF зашифрован. Для чтения нужна разблокированная копия.",
    _PAGES: f"Можно прочитать PDF до {MAX_PDF_PAGES} страниц.",
    _SIZE: "Текст PDF больше 256 КиБ. Выбери меньший документ.",
    _BACKEND: "Системный адаптер PDF недоступен.",
}


class PDFTextError(ValueError):
    pass


def _bounded(raw, deadline, mode, binary=""):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PDFTextError("Чтение PDF не уложилось в 5 секунд.")
    command = [sys.executable, "-I", str(Path(__file__).resolve()), mode]
    if binary:
        command.append(binary)
    try:
        # Regular anonymous files let RLIMIT_FSIZE bound child writes. Keep
        # diagnostics separate from text and never send them to the chat.
        with TemporaryFile() as output, TemporaryFile() as errors:
            result = subprocess.run(command, input=raw, stdout=output, stderr=errors,
                                    env={"PATH": os.defpath, "LC_ALL": "C"}, timeout=remaining, check=False)
            output.seek(0)
            errors.seek(0)
            text = output.read(MAX_TEXT_BYTES + 1)
            diagnostics = errors.read(MAX_TEXT_BYTES + 1)
    except subprocess.TimeoutExpired:
        raise PDFTextError("Чтение PDF не уложилось в 5 секунд.") from None
    except OSError:
        raise PDFTextError(_MESSAGES[_BACKEND]) from None
    if result.returncode in {_SIZE, -signal.SIGXFSZ} or len(text) > MAX_TEXT_BYTES:
        raise PDFTextError(_MESSAGES[_SIZE])
    return result.returncode, text, diagnostics


def _linux_text(raw, deadline):
    pdfinfo, pdftotext = shutil.which("pdfinfo"), shutil.which("pdftotext")
    if not (pdfinfo and pdftotext):
        raise PDFTextError("Для чтения PDF на Linux нужны установленные pdfinfo и pdftotext из Poppler.")
    code, info, diagnostics = _bounded(raw, deadline, "info", pdfinfo)
    if b"incorrect password" in diagnostics.lower():
        raise PDFTextError(_MESSAGES[_ENCRYPTED])
    if code in {_BAD, _BACKEND}:
        raise PDFTextError(_MESSAGES[code])
    if code or b"Syntax Error:" in diagnostics:
        raise PDFTextError(_MESSAGES[_BAD])
    encrypted = re.findall(rb"(?m)^Encrypted:[ \t]+(yes|no)(?=[ \t\r\n]|$)", info)
    pages = re.findall(rb"(?m)^Pages:[ \t]+([0-9]{1,9})[ \t\r]*$", info)
    if encrypted == [b"yes"]:
        raise PDFTextError(_MESSAGES[_ENCRYPTED])
    # Require unambiguous tool fields even when document metadata has newlines.
    if encrypted != [b"no"] or len(pages) != 1 or int(pages[0]) == 0:
        raise PDFTextError(_MESSAGES[_BAD])
    if int(pages[0]) > MAX_PDF_PAGES:
        raise PDFTextError(_MESSAGES[_PAGES])
    code, text, diagnostics = _bounded(raw, deadline, "poppler", pdftotext)
    if code == 3 or b"incorrect password" in diagnostics.lower():
        raise PDFTextError(_MESSAGES[_ENCRYPTED])
    if code or b"Syntax Error:" in diagnostics:
        raise PDFTextError(_MESSAGES.get(code, _MESSAGES[_BAD]))
    return text


def extract_pdf(raw):
    if len(raw) > MAX_PDF_BYTES:
        raise PDFTextError("Можно прочитать PDF до 8 МиБ.")
    if (not re.match(rb"%PDF-[0-9]\.[0-9][\r\n]", raw)
            or not raw[-4096:].rstrip(b" \t\r\n\0").endswith(b"%%EOF")):
        raise PDFTextError(_MESSAGES[_BAD])
    deadline = time.monotonic() + _TIMEOUT
    if not _SLOT.acquire(timeout=_TIMEOUT):
        raise PDFTextError("Чтение PDF занято. Повтори запрос чуть позже.")
    try:
        if sys.platform == "darwin":
            code, text, _ = _bounded(raw, deadline, "pdfkit")
            if code:
                raise PDFTextError(_MESSAGES.get(code, "Чтение PDF завершилось ошибкой или превысило лимит ресурсов."))
        elif sys.platform.startswith("linux"):
            text = _linux_text(raw, deadline)
        else:
            raise PDFTextError(_MESSAGES[_BACKEND])
    finally:
        _SLOT.release()
    try:
        decoded = text.decode("utf-8")
    except UnicodeDecodeError:
        raise PDFTextError(_MESSAGES[_BAD]) from None
    decoded = decoded.replace("\f", "\n\n").strip()
    if not decoded:
        raise PDFTextError("В PDF нет доступного текстового слоя. Для скана понадобится OCR.")
    return decoded


def _darwin_address_space():
    import ctypes

    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    task = ctypes.c_uint.in_dll(system, "mach_task_self_")
    # MACH_TASK_BASIC_INFO begins with three mach_vm_size_t values.
    info = (ctypes.c_uint64 * 6)()
    count = ctypes.c_uint(ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_uint))
    if system.task_info(task, ctypes.c_int(20), ctypes.byref(info), ctypes.byref(count)) or not info[0]:
        raise OSError("Could not read PDF worker's virtual address space")
    return info[0]


def _limit_memory():
    # macOS maps a large sparse shared cache even in an idle process. Cap
    # additional address mappings after loading the system PDF framework.
    current = _darwin_address_space() if sys.platform == "darwin" else 0
    cap = current + _MEMORY_BYTES
    for inherited in resource.getrlimit(resource.RLIMIT_AS):
        if inherited != resource.RLIM_INFINITY:
            cap = min(cap, inherited)
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))


def _pdfkit():
    import ctypes as c

    objc = c.CDLL("/usr/lib/libobjc.A.dylib")
    c.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
    c.CDLL("/System/Library/Frameworks/PDFKit.framework/PDFKit")
    pointer = c.c_void_p
    objc.objc_getClass.argtypes = [c.c_char_p]
    objc.objc_getClass.restype = pointer
    objc.sel_registerName.argtypes = [c.c_char_p]
    objc.sel_registerName.restype = pointer

    def send(target, selector, result=pointer, arguments=(), types=()):
        function = c.CFUNCTYPE(result, pointer, pointer, *types)(("objc_msgSend", objc))
        return function(target, objc.sel_registerName(selector), *arguments)

    pool_type = objc.objc_getClass(b"NSAutoreleasePool")
    data_type = objc.objc_getClass(b"NSData")
    pdf_type = objc.objc_getClass(b"PDFDocument")
    if not (pool_type and data_type and pdf_type):
        return _BACKEND
    pool = send(pool_type, b"new")
    doc = None
    try:
        _limit_memory()
        raw = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
        if len(raw) > MAX_PDF_BYTES:
            return _BAD
        data = send(data_type, b"dataWithBytes:length:", arguments=(raw, len(raw)), types=(c.c_char_p, c.c_size_t))
        doc = send(send(pdf_type, b"alloc"), b"initWithData:", arguments=(data,), types=(pointer,))
        if not doc:
            return _BAD
        if send(doc, b"isEncrypted", c.c_bool) or send(doc, b"isLocked", c.c_bool):
            return _ENCRYPTED
        pages = send(doc, b"pageCount", c.c_size_t)
        if pages == 0:
            return _BAD
        if pages > MAX_PDF_PAGES:
            return _PAGES
        parts, used = [], 0
        for index in range(pages):
            page_pool = send(pool_type, b"new")
            try:
                page = send(doc, b"pageAtIndex:", arguments=(index,), types=(c.c_size_t,))
                if not page:
                    return _BAD
                string = send(page, b"string")
                if not string:
                    continue
                # NSUTF8StringEncoding = 4. Get the byte count before copying.
                length = send(string, b"lengthOfBytesUsingEncoding:", c.c_size_t, (4,), (c.c_size_t,))
                if not length:
                    continue
                used += length + (2 if parts else 0)
                if used > MAX_TEXT_BYTES:
                    return _SIZE
                address = send(string, b"UTF8String")
                if not address:
                    return _BAD
                parts.append(c.string_at(address, length))
            finally:
                send(page_pool, b"drain", None)
        sys.stdout.buffer.write(b"\n\n".join(parts))
        return 0
    finally:
        if doc:
            send(doc, b"release", None)
        send(pool, b"drain", None)


def _worker():
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_TEXT_BYTES + 1, MAX_TEXT_BYTES + 1))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if sys.argv[1:] == ["pdfkit"] and sys.platform == "darwin":
            return _pdfkit()
        if len(sys.argv) == 3 and sys.platform.startswith("linux"):
            mode, binary = sys.argv[1:]
            if mode in {"info", "poppler"}:
                _limit_memory()
                arguments = ["-"] if mode == "info" else ["-enc", "UTF-8", "-f", "1", "-l", str(MAX_PDF_PAGES), "-", "-"]
                os.execv(binary, [binary, *arguments])
    except (OSError, ValueError, AttributeError, MemoryError):
        return _BACKEND
    return _BACKEND


if __name__ == "__main__":
    raise SystemExit(_worker())
