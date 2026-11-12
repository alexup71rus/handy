"""SQLite initialization for confirmed project knowledge."""

import hashlib
import os
import sqlite3
from pathlib import Path


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()



class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA cache_size=-2048;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, root TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS project_knowledge(
                id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL REFERENCES workspaces(id),
                key TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, version INTEGER NOT NULL,
                encoder TEXT, vector BLOB, UNIQUE(workspace,key)
            );
        """)
        os.chmod(self.path, 0o600)

    def close(self):
        self.db.close()

    def workspace(self, root):
        p = Path(root).expanduser().resolve(strict=True)
        if not p.is_dir():
            raise ValueError("Выбранный путь не является папкой.")
        wid = digest(str(p))[:24]
        self.db.execute("INSERT OR IGNORE INTO workspaces(id,root) VALUES(?,?)", (wid, str(p)))
        self.db.commit()
        return wid, p
