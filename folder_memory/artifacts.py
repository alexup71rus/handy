"""Export offline pages to an isolated directory in the session's worktree."""

import base64
from contextlib import ExitStack, suppress
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

from .file_access import ARTIFACT_NAMESPACE, is_private, require_user_path


KINDS = frozenset({"table", "chart", "list", "clock", "dice", "password", "schedules", "toolbox", "timer", "pomodoro"})
_TEMPLATE_ALIASES = {"pomodoro": "timer"}
MAX_PAGE_BYTES = 768 * 1024
_ROOT = Path(__file__).resolve().parents[1] / "templates"
_IMPORT = re.compile(r"\Aimport \{ ([\w, ]+) \} from '../shared/report\.js';\n")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True)
class ArtifactPage:
    kind: str
    data: dict


def _sha256(value):
    return base64.b64encode(hashlib.sha256(value.encode("utf-8")).digest()).decode("ascii")


def render_artifact(page):
    if page.kind not in KINDS:
        raise ValueError("Неизвестный вид артефакта.")
    raw = json.dumps(page.data, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if len(raw.encode("utf-8")) > MAX_PAGE_BYTES:
        raise ValueError("Артефакт больше 768 КиБ. Выбери меньше строк или столбцов.")
    name = _TEMPLATE_ALIASES.get(page.kind, page.kind)
    template = (_ROOT / name / "index.html").read_text(encoding="utf-8")
    css = (_ROOT / "shared" / "styles.css").read_text(encoding="utf-8")
    shared = (_ROOT / "shared" / "report.js").read_text(encoding="utf-8")
    script = (_ROOT / name / f"{name}.js").read_text(encoding="utf-8")
    imported = _IMPORT.match(script)
    css_tag = '<link rel="stylesheet" href="../shared/styles.css">'
    script_tag = f'<script type="module" src="{name}.js"></script>'
    if not imported or css_tag not in template or script_tag not in template:
        raise ValueError("Шаблон артефакта не подходит для автономного HTML.")
    bindings = imported[1]
    shared = re.sub(r"^export ", "", shared, flags=re.M)
    bundle = f"const {{ {bindings} }} = (() => {{\n{shared}\nreturn {{ {bindings} }};\n}})();\n" + script[imported.end():]
    data = raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    csp = (f"default-src 'none'; script-src 'sha256-{_sha256(bundle)}'; style-src 'sha256-{_sha256(css)}'; "
           "base-uri 'none'; form-action 'none'")
    html = template.replace(css_tag, f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n<style>{css}</style>')
    html = html.replace(script_tag, f'<script id="funny-artifact-data" type="application/json">{data}</script>\n'
                                    f'<script type="module">{bundle}</script>')
    html = html.replace("<body>", '<body class="artifact-view">', 1)
    return html.encode("utf-8"), raw.encode("utf-8")


def project_root(base_root):
    root = require_user_path(base_root)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], env=env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        candidate = require_user_path(result.stdout.rstrip("\n"))
        if not root.is_relative_to(candidate):
            raise ValueError("Не удалось подтвердить рабочий корень проекта.")
        root = candidate
    if is_private(root):
        raise ValueError("Выбери рабочую папку или worktree проекта.")
    return root


def _open_dir(stack, name, parent=None):
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    stack.callback(os.close, fd)
    return fd


def _mkdir(name, parent):
    with suppress(FileExistsError):
        os.mkdir(name, 0o700, dir_fd=parent)


def _write(name, raw, parent):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    with os.fdopen(fd, "wb") as file:
        file.write(raw)


def save_artifact(page, base_root):
    html, data = render_artifact(page)
    root = project_root(base_root)
    name = page.kind + "-" + secrets.token_hex(10)
    staging = "." + name + ".tmp"
    with ExitStack() as stack:
        parent = _open_dir(stack, root.anchor)
        for part in root.parts[1:]:
            parent = _open_dir(stack, part, parent)
        for part in (ARTIFACT_NAMESPACE, "artifacts"):
            _mkdir(part, parent)
            parent = _open_dir(stack, part, parent)
        with suppress(FileExistsError):
            _write(".gitignore", b"*\n", parent)
        os.mkdir(staging, 0o700, dir_fd=parent)
        try:
            with ExitStack() as staging_stack:
                target = _open_dir(staging_stack, staging, parent)
                try:
                    _write("index.html", html, target)
                    _write("data.json", data, target)
                    os.rename(staging, name, src_dir_fd=parent, dst_dir_fd=parent)
                except BaseException:
                    for filename in ("index.html", "data.json"):
                        with suppress(FileNotFoundError):
                            os.unlink(filename, dir_fd=target)
                    raise
        except BaseException:
            with suppress(FileNotFoundError):
                os.rmdir(staging, dir_fd=parent)
            raise
    directory = root / ARTIFACT_NAMESPACE / "artifacts" / name
    return {"kind": page.kind, "title": page.data["title"], "path": str(directory / "index.html"),
            "data_path": str(directory / "data.json")}
