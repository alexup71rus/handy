"""Generic lexical utilities and a data-defined file classifier."""

import json
import re
from functools import lru_cache
from pathlib import Path

RESOURCES = Path(__file__).parent / "resources"
STOP = set("а и в во на с со к ко от до по из за у о об для это этой этом здесь там мне меня ты он она оно мы вы они как что где ли бы пожалуйста the a an in of to for is are this that please".split())


@lru_cache(maxsize=16)
def _load(path, modified):
    return json.loads(Path(path).read_text())


def resource(name):
    p = RESOURCES / name
    return _load(str(p), p.stat().st_mtime_ns)


def tokens(text):
    return [w for w in re.findall(r"[\w-]+", text.lower().replace("ё", "е")) if w not in STOP and len(w) > 1]


def stem(word):
    return word[:7] if re.search("[а-я]", word) and len(word) > 7 else word


def normalized(text):
    return " ".join(stem(w) for w in tokens(text))


def search_terms(text):
    words = tokens(text)
    extra = []
    for prefix, alternatives in resource("search.json")["synonyms"].items():
        if any(w.startswith(prefix) or w in alternatives for w in words):
            extra.extend(alternatives)
    return list(dict.fromkeys(stem(w) for w in words + extra))[:24]


def file_tags(path):
    data = resource("file-types.json")
    p = Path(path)
    fmt = data["extensions"].get(p.suffix.lower(), p.suffix.lower().lstrip(".") or "без-расширения")
    kind = next((kind for kind, formats in data["groups"].items() if fmt in formats), "file")
    tags = ["type/" + kind, "format/" + fmt]
    for rule in data["path_rules"]:
        name_match = any(p.name.lower().startswith(prefix) for prefix in rule.get("basename_prefix", []))
        path_match = any(part in p.parts for part in rule.get("path_part", []))
        if name_match or path_match:
            tags.append(rule["tag"])
    return tags


_types = resource("file-types.json")
TEXT_SUFFIXES = {s for s, fmt in _types["extensions"].items() if fmt in _types["text_formats"]}
