"""Authenticated read-only Handy knowledge lookup."""

from pathlib import Path

from .chat_evidence import collect_evidence, evidence_text


def query(app, body):
    if not isinstance(body, dict) or set(body) - {"input", "root", "session"}:
        raise ValueError("Expected only input, root and optional session")
    text = body.get("input")
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        raise ValueError("input must be a nonempty string of at most 8000 characters")
    root = body.get("root")
    if not isinstance(root, str) or not root or not Path(root).is_absolute():
        raise ValueError("root must be an absolute existing directory")
    try:
        root = Path(root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("root must be an absolute existing directory")
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("root must be an absolute existing directory") from error
    if "session" in body:
        session = body["session"]
        if not isinstance(session, str) or not session.strip() or len(session) > 128 or any(
                ord(char) < 32 or ord(char) == 127 for char in session):
            raise ValueError("session must be a nonempty string of at most 128 characters without controls")
    records = collect_evidence(app, text.strip(), root, explicit=True)
    return {
        "status": "ok",
        "text": evidence_text(records) if records else "В справке и подтверждённой памяти Handy подходящих записей нет.",
        "data": {"records": records},
        "question": None,
        "question_id": None,
    }
