"""Personal, user-confirmed facts, independent of a project or directory.

This storage layer never interprets consent. The dialogue core must confirm the
prepared proposal before calling ``save``. Saves do not commit: the caller owns
the transaction containing the fact, its provenance and the pending question.
"""

import hashlib
import json
import re
import sqlite3
import time
import unicodedata


class MemoryConflictError(ValueError):
    """The fact changed after the user was shown the proposed replacement."""


def evidence_values(text):
    """Return user-supplied text spans, excluding keys of structured input.

    Explicit semantic frames are a supported user interface, so their decoded
    string values count as evidence. JSON syntax/field names do not. Keeping
    spans separate prevents a proposed value from joining unrelated fields.
    """
    try:
        structured = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return (text,)
    if not isinstance(structured, (dict, list)):
        return (text,)
    pending, values = [structured], []
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return tuple(values)


def _clean(value, name, maximum, *, multiline=False):
    if not isinstance(value, str):
        raise ValueError(name + " must be a string")
    value = unicodedata.normalize("NFC", value).strip()
    if not value or len(value) > maximum:
        raise ValueError(name + " has an invalid length")
    for char in value:
        if unicodedata.category(char) == "Cc" and not (multiline and char in "\t\n\r"):
            raise ValueError(name + " contains control characters")
        if "\u202a" <= char <= "\u202e" or "\u2066" <= char <= "\u2069":
            raise ValueError(name + " contains directional controls")
    return value


def _key(value):
    return " ".join(value.casefold().replace("ё", "е").split())


def _words(value):
    return tuple(re.findall(r"[^\W_]+", _key(value), flags=re.UNICODE))


def _field_words(value):
    # A conservative inflection aid, not a semantic model or synonym database.
    return {
        re.sub(r"(?:иями|ами|ями|ого|ему|ах|ях|ов|ев|ой|ий|ая|ое|ие|ые|ом|ем|у|ю|а|я|ы|и|е)$", "", word)
        if len(word) > 5 and re.search("[а-я]", word) else word
        for word in _words(value)
    }


class PersonalMemory:
    def __init__(self, connection: sqlite3.Connection, *, initialize=True):
        self.db = connection
        if not initialize:
            return
        # execute(), rather than executescript(), preserves an owner's existing
        # transaction. No workspace identifier participates in personal memory.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS personal_facts(
                id INTEGER PRIMARY KEY,
                subject TEXT NOT NULL,
                subject_key TEXT NOT NULL,
                field TEXT NOT NULL,
                field_key TEXT NOT NULL,
                value TEXT NOT NULL,
                evidence TEXT NOT NULL,
                source TEXT NOT NULL,
                version INTEGER NOT NULL,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                UNIQUE(subject_key,field_key)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS personal_fact_history(
                fact_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                snapshot TEXT NOT NULL,
                PRIMARY KEY(fact_id,version),
                FOREIGN KEY(fact_id) REFERENCES personal_facts(id)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS personal_memory_operations(
                operation_id TEXT PRIMARY KEY,
                proposal_hash TEXT NOT NULL,
                result TEXT NOT NULL,
                created REAL NOT NULL
            )
        """)

    def _rows(self, sql, parameters=()):
        cursor = self.db.execute(sql, parameters)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor]

    def _record(self, subject_key, field_key):
        rows = self._rows(
            "SELECT * FROM personal_facts WHERE subject_key=? AND field_key=?",
            (subject_key, field_key),
        )
        return {**rows[0], "scope": "personal"} if rows else None

    def _validated(self, subject, field, value, evidence):
        subject = _clean(subject, "subject", 240)
        field = _clean(field, "field", 160)
        value = _clean(value, "value", 4000, multiline=True)
        evidence = _clean(evidence, "evidence", 16000, multiline=True)
        # Values are not casefolded, reformatted or inferred from approximate
        # matches. This also preserves case-sensitive identifiers faithfully.
        if not any(" ".join(value.split()) in " ".join(span.split()) for span in evidence_values(evidence)):
            raise ValueError("The proposed value is absent from the user's evidence")
        return subject, field, value, evidence

    def prepare(self, subject, field, value, evidence):
        """Validate and preview a new/replacement assertion without writing."""
        subject, field, value, evidence = self._validated(subject, field, value, evidence)
        prior = self._record(_key(subject), _key(field))
        return {
            "schema_version": 1,
            "scope": "personal",
            "subject": subject,
            "field": field,
            "value": value,
            "evidence": evidence,
            "source": "user-provided",
            "prior": prior,
            "expected_id": prior["id"] if prior else None,
            "expected_version": prior["version"] if prior else None,
            "changes_value": prior is None or prior["value"] != value,
        }

    def save(self, proposal, operation_id):
        """Save a confirmed proposal; leave the entire transaction uncommitted.

        The operation ID identifies the concrete pending confirmation. A replay
        returns its original snapshot even after another update, while reusing
        the same ID for a different assertion is rejected. Stale previews must
        be confirmed again instead of silently overwriting a newer fact.
        """
        operation_id = _clean(operation_id, "operation_id", 200)
        if not isinstance(proposal, dict) or proposal.get("schema_version") != 1 or proposal.get("scope") != "personal":
            raise ValueError("Expected a prepared personal-memory proposal")
        if "expected_id" not in proposal or "expected_version" not in proposal:
            raise ValueError("The proposal has no expected revision")
        subject, field, value, evidence = self._validated(
            proposal.get("subject"), proposal.get("field"), proposal.get("value"), proposal.get("evidence")
        )
        signature = json.dumps(
            {"subject_key": _key(subject), "field_key": _key(field), "value": value, "evidence": evidence},
            ensure_ascii=False, sort_keys=True,
        )
        proposal_hash = hashlib.sha256(signature.encode()).hexdigest()
        if not self.db.in_transaction:
            # An explicit outer transaction prevents RELEASE SAVEPOINT from
            # committing when save() was called without an existing one.
            self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("SAVEPOINT funny_personal_save")
        try:
            previous_op = self._rows(
                "SELECT proposal_hash,result FROM personal_memory_operations WHERE operation_id=?",
                (operation_id,),
            )
            if previous_op:
                if previous_op[0]["proposal_hash"] != proposal_hash:
                    raise ValueError("The operation ID belongs to another proposal")
                result = json.loads(previous_op[0]["result"])
            else:
                current = self._record(_key(subject), _key(field))
                revision = (current["id"], current["version"]) if current else (None, None)
                if revision != (proposal["expected_id"], proposal["expected_version"]):
                    raise MemoryConflictError("Personal memory changed; a new confirmation is required")
                now = time.time()
                if current is None:
                    self.db.execute("""
                        INSERT INTO personal_facts(subject,subject_key,field,field_key,value,evidence,source,version,created,updated)
                        VALUES(?,?,?,?,?,?,'user-confirmed',1,?,?)
                    """, (subject, _key(subject), field, _key(field), value, evidence, now, now))
                elif current["value"] != value:
                    self.db.execute("""
                        UPDATE personal_facts SET subject=?,field=?,value=?,evidence=?,source='user-confirmed',version=version+1,updated=?
                        WHERE id=?
                    """, (subject, field, value, evidence, now, current["id"]))
                result = self._record(_key(subject), _key(field))
                snapshot = json.dumps(result, ensure_ascii=False, sort_keys=True)
                self.db.execute(
                    "INSERT OR IGNORE INTO personal_fact_history(fact_id,version,snapshot) VALUES(?,?,?)",
                    (result["id"], result["version"], snapshot),
                )
                self.db.execute(
                    "INSERT INTO personal_memory_operations(operation_id,proposal_hash,result,created) VALUES(?,?,?,?)",
                    (operation_id, proposal_hash, snapshot, now),
                )
            self.db.execute("RELEASE SAVEPOINT funny_personal_save")
            return result
        except BaseException:
            self.db.execute("ROLLBACK TO SAVEPOINT funny_personal_save")
            self.db.execute("RELEASE SAVEPOINT funny_personal_save")
            raise

    def recall(self, subject, field="", limit=5):
        """Return recorded assertions, never fill missing information by guess."""
        subject = _clean(subject, "subject", 240)
        if not isinstance(field, str) or len(field) > 160:
            raise ValueError("field has an invalid length")
        if field.strip():
            field = _clean(field, "field", 160)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        subject_key = _key(subject)
        rows = self._rows("SELECT * FROM personal_facts WHERE subject_key=? ORDER BY updated DESC,id", (subject_key,))
        if not rows:
            # Punctuation-only differences may match ("Service-X" / "Service X").
            # Reordering, partial service names and approximate embeddings do
            # not establish entity identity in this layer.
            wanted = _words(subject)
            if not wanted:
                return []
            # Unrelated subjects must not displace the matching name. LIKE is
            # only a prefilter; identity still requires exact ordered tokens.
            pattern = "%" + "%".join(wanted) + "%"
            candidates = self._rows("SELECT DISTINCT subject_key FROM personal_facts WHERE subject_key LIKE ? ORDER BY subject_key LIMIT 2001", (pattern,))
            if len(candidates) > 2000:
                # An unseen punctuation-equivalent name could be another
                # candidate. A truncated search cannot establish uniqueness.
                return []
            keys = [r["subject_key"] for r in candidates if wanted and _words(r["subject_key"]) == wanted]
            if len(keys) != 1:
                return []
            rows = self._rows("SELECT * FROM personal_facts WHERE subject_key=? ORDER BY updated DESC,id", (keys[0],))
        if field.strip():
            exact = [r for r in rows if r["field_key"] == _key(field)]
            if exact:
                rows = exact
            else:
                wanted = _field_words(field)
                rows = [r for r in rows if wanted and wanted.issubset(_field_words(r["field"]))]
        return [{**r, "scope": "personal"} for r in rows[:limit]]

    def history(self, fact_id):
        if type(fact_id) is not int or fact_id < 1:
            raise ValueError("fact_id must be a positive integer")
        rows = self._rows("SELECT snapshot FROM personal_fact_history WHERE fact_id=? ORDER BY version", (fact_id,))
        return [json.loads(r["snapshot"]) for r in rows]

    def confirmed_fields(self, fields):
        """Read exact fields without exposing their original evidence."""
        keys = tuple(_key(field) for field in fields)
        if not keys:
            return []
        slots = ",".join("?" for _ in keys)
        return self._rows(
            f"SELECT id,version,subject,value FROM personal_facts "
            f"WHERE source='user-confirmed' AND field_key IN ({slots}) ORDER BY subject_key,id",
            keys,
        )
