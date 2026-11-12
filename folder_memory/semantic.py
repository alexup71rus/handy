"""Persistent vectors with bounded retrieval; scores never authorize tools."""

import array
import ctypes
import functools
import hashlib
import heapq
import json
import math
import platform
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path


@functools.lru_cache(maxsize=1)
def _blas():
    if platform.system() != "Darwin":
        return None
    try:
        lib = ctypes.CDLL('/System/Library/Frameworks/Accelerate.framework/Accelerate')
        function = lib.cblas_sgemv
        function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_float, ctypes.POINTER(ctypes.c_float), ctypes.c_int, ctypes.POINTER(ctypes.c_float), ctypes.c_int, ctypes.c_float, ctypes.POINTER(ctypes.c_float), ctypes.c_int]
        function.restype = None
        return function
    except (OSError, AttributeError):
        return None


def pack(vector):
    values = array.array('f', vector)
    if sys.byteorder != 'little':
        values.byteswap()
    return values.tobytes()


def scores(blobs, query):
    dimension = len(query)
    if any(len(blob) != dimension * 4 for blob in blobs):
        raise ValueError('Vector dimension mismatch')
    matrix = array.array('f')
    matrix.frombytes(b''.join(blobs))
    if sys.byteorder != 'little':
        matrix.byteswap()
    function = _blas()
    if function:
        q = array.array('f', query)
        out = array.array('f', [0.0]) * len(blobs)
        function(101, 111, len(blobs), dimension, 1.0, (ctypes.c_float * len(matrix)).from_buffer(matrix), dimension, (ctypes.c_float * dimension).from_buffer(q), 1, 0.0, (ctypes.c_float * len(out)).from_buffer(out), 1)
        return out
    return [sum(matrix[row * dimension + col] * value for col, value in enumerate(query)) for row in range(len(blobs))]


class SemanticStore:
    def __init__(self, path, embedder):
        self.path, self.embedder = Path(path).resolve(), embedder

    @contextmanager
    def connect(self, *, create=False):
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path)
            db.executescript('''PRAGMA journal_mode=WAL; PRAGMA cache_size=-4096;
                CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,kind TEXT NOT NULL,text TEXT NOT NULL,metadata TEXT NOT NULL,source TEXT NOT NULL,content_hash TEXT NOT NULL,model_hash TEXT NOT NULL,vector BLOB NOT NULL);
                CREATE INDEX IF NOT EXISTS document_kind ON documents(kind,model_hash);''')
        else:
            db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
            db.execute('PRAGMA cache_size=-4096')
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @property
    def ready(self):
        return self.path.is_file() and self.embedder.enabled

    def stats(self):
        if not self.path.exists():
            return {'ready': False, 'documents': 0}
        with self.connect() as db:
            counts = {row[0]:row[1] for row in db.execute('SELECT kind,COUNT(*) FROM documents WHERE model_hash=? GROUP BY kind',(self.embedder.fingerprint,))}
        model=getattr(self.embedder,'model',None)
        weights=getattr(model,'config',{}).get('model_sha256')
        return {'ready': self.ready and bool(counts), 'documents': sum(counts.values()), 'kinds': counts,
                'bytes': self.path.stat().st_size,
                'model_sha256':self.embedder.fingerprint, 'encoder_fingerprint':self.embedder.fingerprint,'weights_sha256':weights}

    def upsert(self, documents, progress=None, *, prune=False):
        batch, inserted, skipped, reused = [], 0, 0, 0
        with self.connect(create=True) as db:
            if prune:
                db.execute('CREATE TEMP TABLE live_documents(id TEXT PRIMARY KEY)')
            def flush():
                nonlocal inserted
                if not batch:
                    return
                vectors = self.embedder.encode([item['text'] for item in batch])
                for item, vector in zip(batch, vectors):
                    db.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?)',(item['id'],item['kind'],item['text'],json.dumps(item.get('metadata',{}),ensure_ascii=False),item.get('source',''),item['hash'],self.embedder.fingerprint,pack(vector)))
                db.commit()
                inserted += len(batch)
                batch.clear()
                if progress:
                    progress(inserted,skipped)
            for document in documents:
                item = dict(document)
                if prune:
                    db.execute('INSERT OR IGNORE INTO live_documents VALUES (?)',(item['id'],))
                item['text'] = item['text'][:1200]
                item['hash'] = hashlib.sha256(json.dumps({key:value for key,value in item.items() if key!='hash'},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                old = db.execute('SELECT content_hash,model_hash,text,vector FROM documents WHERE id=?',(item['id'],)).fetchone()
                if old and (old['content_hash'],old['model_hash'])==(item['hash'],self.embedder.fingerprint):
                    skipped += 1
                    continue
                if old and old['model_hash']==self.embedder.fingerprint and old['text']==item['text']:
                    # Metadata/provenance changes cannot change the vector of
                    # identical input. Reuse it when publishing a new snapshot.
                    db.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?)',(item['id'],item['kind'],item['text'],json.dumps(item.get('metadata',{}),ensure_ascii=False),item.get('source',''),item['hash'],self.embedder.fingerprint,old['vector']))
                    inserted += 1
                    reused += 1
                    continue
                batch.append(item)
                if len(batch)>=8:
                    flush()
            flush()
            db.commit()
            if prune:
                db.execute('DELETE FROM documents WHERE id NOT IN (SELECT id FROM live_documents)')
                db.commit()
        return {'inserted':inserted,'unchanged':skipped,'vectors_reused':reused}

    def search(self, text, *, kinds, limit=6):
        kinds=list(dict.fromkeys(kinds))
        if not kinds or not 1<=limit<=20 or not self.ready:
            return []
        vector = self.embedder.query(text)
        heap=[]
        placeholders=','.join('?' for _ in kinds)
        with self.connect() as db:
            cursor=db.execute('SELECT id,kind,text,metadata,source,vector FROM documents WHERE model_hash=? AND kind IN ('+placeholders+')',(self.embedder.fingerprint,*kinds))
            while rows:=cursor.fetchmany(256):
                values=scores([row['vector'] for row in rows],vector)
                for row,score in zip(rows,values):
                    if not math.isfinite(score):
                        continue
                    entry=(float(score),row['id'],dict(row))
                    if len(heap)<limit:
                        heapq.heappush(heap,entry)
                    elif entry[:2]>heap[0][:2]:
                        heapq.heapreplace(heap,entry)
        result=[]
        for score,identity,row in sorted(heap,reverse=True):
            row.pop('vector');row['score']=score;row['metadata']=json.loads(row['metadata']);result.append(row)
        return result
