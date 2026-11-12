"""Import a small pinned tldr reference pack. Never execute imported examples."""

import argparse
import concurrent.futures
import hashlib
import itertools
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
from folder_memory.command_policy import validate_read_recipe

REVISION = "758e8f23e08dcc26023eac7544c4d502e728b0de"
REPOSITORY = "https://github.com/tldr-pages/tldr"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PACK_BYTES = 5 * 1024 * 1024
ATTRIBUTION = "tldr-pages team and contributors"
PLATFORMS = {"common", "linux", "osx"}
DENIED_PAGES = set("rm kill killall pkill mv cp chmod chown chgrp sudo su doas dd mkfs diskutil shutdown reboot halt poweroff eval exec source xargs".split())
TARGETS = [("common", name) for name in "ls pwd cat head tail wc grep rg du df file stat sort uniq cut ps echo uname find curl ssh git jq lsof python".split()]
TARGETS += [("osx", name) for name in "sw_vers system_profiler mdfind mdls launchctl".split()]
TARGETS += [("linux", name) for name in "systemctl journalctl ss ip free dmesg vmstat".split()]
TARGETS += [("osx", name) for name in "log vm_stat sysctl sed".split()]
TARGETS += [("common", name) for name in "sed docker-compose docker-compose-logs docker-logs".split()]
COMPOUND_NAMES = {name: name.replace("-", " ") for name in ("docker-compose", "docker-compose-logs", "docker-logs")}
PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")


class FetchError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def source_path(platform, command, language):
    if platform not in PLATFORMS or language not in {"ru", "en"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", command) or command in DENIED_PAGES:
        raise ValueError("Unsupported reference page")
    return ("pages.ru" if language == "ru" else "pages") + "/" + platform + "/" + command + ".md"


def source_url(path, revision=REVISION):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("A full immutable commit SHA is required")
    if path != "LICENSE.md" and not re.fullmatch(r"pages(?:\.ru)?/(?:common|linux|osx)/[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}\.md", path):
        raise ValueError("Unsupported repository path")
    return "https://raw.githubusercontent.com/tldr-pages/tldr/" + revision + "/" + path


def fetch(url, *, deadline):
    """TLS-verified system curl, streaming byte cap and an overall deadline."""
    remaining = min(12, deadline - time.monotonic())
    if remaining <= 0:
        raise FetchError("deadline", "Import deadline reached")
    binary = shutil.which("curl")
    if not binary:
        raise FetchError("transport", "curl is required; no package will be installed")
    process = subprocess.Popen([binary, "--silent", "--show-error", "--fail", "--proto", "=https", "--proto-redir", "=https", "--location", "--max-redirs", "2", "--connect-timeout", "4", "--max-time", str(remaining), "--max-filesize", str(MAX_PAGE_BYTES), "--write-out", "\n%{http_code}", url], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "body")
    selector.register(process.stderr, selectors.EVENT_READ, "error")
    body, error = bytearray(), bytearray()
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise FetchError("deadline", "Import deadline reached")
            for key, _ in selector.select(timeout=min(.2, max(.01, deadline - time.monotonic()))):
                available = MAX_PAGE_BYTES + 5 - len(body) if key.data == "body" else 65536
                chunk = os.read(key.fd, min(65536, max(1, available)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "body":
                    body.extend(chunk)
                    if len(body) > MAX_PAGE_BYTES + 4:
                        raise FetchError("too_large", "Page exceeds byte budget")
                elif len(error) < 4096:
                    error.extend(chunk[:4096 - len(error)])
        code = process.wait(timeout=1)
        raw, _, status = body.rpartition(b"\n")
        if code or status != b"200":
            raise FetchError(status.decode(errors="replace") or "transport", error.decode(errors="replace")[:250])
        if len(raw) > MAX_PAGE_BYTES:
            raise FetchError("too_large", "Page exceeds byte budget")
        return bytes(raw)
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        process.stdout.close()
        process.stderr.close()


def validation_variants(command):
    """Expand placeholders only to validate; never store these synthetic values."""
    if not isinstance(command, str) or len(command) > 4096:
        return []
    spans, choices = [], []
    for match in PLACEHOLDER.finditer(command):
        value = match[1]
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        if re.search(r"[\s$`;&<>\\(){}]", value) or "..." in value or "…" in value:
            return []
        options = value.split("|")
        normalized = []
        for option in options:
            if not option:
                return []
            if option.startswith("-"):
                if not re.fullmatch(r"--?[A-Za-z0-9][A-Za-z0-9-]*", option):
                    return []
                normalized.append(option)
            elif re.fullmatch(r"\d+(?:-\d+)?", option):
                normalized.append(option)
            else:
                normalized.append("example")
        spans.append((match.start(), match.end()))
        choices.append(list(dict.fromkeys(normalized)))
    stripped = PLACEHOLDER.sub("example", command)
    if "{{" in stripped or "}}" in stripped or len(choices) > 8:
        return []
    combinations = 1
    for values in choices:
        combinations *= len(values)
    if combinations > 16:
        return []
    results = []
    for values in itertools.product(*choices):
        parts, start = [], 0
        for (first, last), value in zip(spans, values):
            parts.extend((command[start:first], value))
            start = last
        parts.append(command[start:])
        results.append("".join(parts))
    return results


def safe_example(command):
    variants = validation_variants(command)
    if not variants:
        return False
    try:
        for variant in variants:
            validate_read_recipe(variant)
    except ValueError:
        return False
    return True


def parse_page(raw, *, command, platform, language, path, revision=REVISION):
    if command in DENIED_PAGES or len(raw) > MAX_PAGE_BYTES:
        raise ValueError("Denied or oversized page")
    text = raw.decode("utf-8")
    descriptions, examples = [], []
    explanation = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("> ") and not re.search(r"https?://|www\.", line):
            descriptions.append(line[2:])
        elif line.startswith("- "):
            explanation = line[2:][:1500]
        elif line.startswith("`") and line.endswith("`") and not line.startswith("```"):
            code = line[1:-1]
            if safe_example(code):
                examples.append({"description": explanation, "command": code})
    description = " ".join(descriptions)[:4000]
    if not description:
        raise ValueError("Page has no usable description")
    digest = hashlib.sha256(raw).hexdigest()
    original = source_url(path, revision)
    return {"id": "tldr:" + language + ":" + platform + ":" + command, "name": COMPOUND_NAMES.get(command, command), "description": description, "aliases": [], "signatures": [entry["command"] for entry in examples[:16]], "examples": [entry["command"] for entry in examples[:16]], "example_details": examples[:16], "platform": platform, "language": language, "reference_only": True, "executable": False, "effect": "reference", "caveats": [], "sources": [original], "provenance": {"repository": REPOSITORY, "revision": revision, "path": path, "sha256": digest, "license": "CC-BY-4.0", "attribution": ATTRIBUTION}}


def build(output, *, revision=REVISION, targets=TARGETS, timeout=150, fetcher=fetch):
    source_url("LICENSE.md", revision)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("A finite positive import deadline is required")
    if not 1 <= len(targets) <= 60:
        raise ValueError("Import requires 1–60 explicitly selected pages")
    deadline = time.monotonic() + min(max(timeout, 1), 180)
    license_data = fetcher(source_url("LICENSE.md", revision), deadline=deadline)
    if len(license_data) > MAX_PAGE_BYTES:
        raise ValueError("License exceeds byte budget")
    pages, rejected = [], []
    lock = threading.Lock()

    def load(target):
        platform, command = target
        for language in ("ru", "en"):
            path = source_path(platform, command, language)
            try:
                raw = fetcher(source_url(path, revision), deadline=deadline)
                return parse_page(raw, command=command, platform=platform, language=language, path=path, revision=revision)
            except (FetchError, ValueError, UnicodeError) as error:
                if language == "en":
                    with lock:
                        rejected.append({"platform": platform, "command": command, "status": str(getattr(error, "status", "invalid"))})
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        for item in executor.map(load, targets):
            if item:
                pages.append(item)
    pages.sort(key=lambda item: (item["platform"], item["name"]))
    rejected.sort(key=lambda item: (item["platform"], item["command"]))
    output = Path(output)
    manifest_path = output.with_name("tldr-manifest.json")
    license_path = output.with_name("tldr-LICENSE.md")
    pack = {"schema_version": 1, "purpose": "Pinned CLI reference descriptions and conservatively filtered examples. Data only, never executable tool definitions or authorization.", "repository": REPOSITORY, "revision": revision, "license": "CC-BY-4.0", "attribution": ATTRIBUTION, "entries": pages}
    encoded = (json.dumps(pack, ensure_ascii=False, indent=2) + "\n").encode()
    manifest = {"schema_version": 1, "repository": REPOSITORY, "revision": revision, "reference_only": True, "attribution": ATTRIBUTION, "license": {"spdx": "CC-BY-4.0", "path": license_path.name, "source": source_url("LICENSE.md", revision), "sha256": hashlib.sha256(license_data).hexdigest()}, "pack": {"path": output.name, "bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}, "budgets": {"max_page_bytes": MAX_PAGE_BYTES, "max_pack_bytes": MAX_PACK_BYTES, "max_seconds": min(max(timeout, 1), 180), "workers": 4}, "pages": [{"id": entry["id"], "language": entry["language"], "platform": entry["platform"], "path": entry["provenance"]["path"], "sha256": entry["provenance"]["sha256"], "source": entry["sources"][0], "safe_examples": len(entry["examples"])} for entry in pages], "missing": rejected}
    manifest_data = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    if len(encoded) + len(manifest_data) + len(license_data) > MAX_PACK_BYTES:
        raise ValueError("Reference pack exceeds byte budget")
    if not pages:
        raise ValueError("No reference pages were downloaded")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(encoded)
    manifest_path.write_bytes(manifest_data)
    license_path.write_bytes(license_data)
    return {"pages": len(pages), "safe_examples": sum(len(p["examples"]) for p in pages), "languages": {language: sum(p["language"] == language for p in pages) for language in ("ru", "en")}, "pack_bytes": len(encoded) + len(manifest_data) + len(license_data), "missing": rejected, "revision": revision}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT / "data/tldr-reference.json")
    parser.add_argument("--revision", default=REVISION)
    parser.add_argument("--timeout", type=float, default=150)
    args = parser.parse_args()
    print(json.dumps(build(args.output, revision=args.revision, timeout=args.timeout), ensure_ascii=False))
