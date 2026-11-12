"""Remove recognizable credentials before adding reference text to a prompt."""

import re


_KEY_LABEL = r"(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?"
_PRIVATE_KEY = re.compile(r"-----BEGIN (?P<label>" + _KEY_LABEL + r")-----.*?(?:-----END (?P=label)-----|\Z)", re.S)
_TOKEN = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{22,}|gh[pousr]_[A-Za-z0-9]{30,}|"
    r"hf_[A-Za-z0-9]{28,}|AKIA[A-Z0-9]{16})\b"
)
_URL_CREDENTIALS = re.compile(r"https?://[^\s/@]+:[^\s/@]+@")
_CREDENTIAL_VALUE = re.compile(
    r"""(?i)(\b(?:authorization\s*:\s*(?:basic|bearer)\s+|(?:\w+_)?(?:password|token|api_key|secret)\s*=\s*)["']?)"""
    r"""(?![$<{])[^\s<>{}"']+"""
)


def redact_text(text):
    text = _PRIVATE_KEY.sub("[скрыто]", text)
    text = _TOKEN.sub("[скрыто]", text)
    text = _CREDENTIAL_VALUE.sub(r"\1[скрыто]", text)
    return _URL_CREDENTIALS.sub("https://[скрыто]@", text)
