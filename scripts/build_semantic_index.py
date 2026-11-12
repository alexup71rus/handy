"""Build the semantic index from technical reference packs."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from folder_memory.embeddings import Embedder
from folder_memory.model_backend import create_model
from folder_memory.semantic import SemanticStore
from folder_memory.knowledge import ReferenceLibrary
from folder_memory.knowledge_quality import deduplicate_documents


def documents():
    for item in ReferenceLibrary.entries():
        yield {'id':'reference:'+item['id'],'kind':'reference','text':item['name']+': '+item['description']+' '+', '.join(item['aliases']),'metadata':{'entry':item},'source':item['sources'][0]}
    for item in ReferenceLibrary.topics():
        text = item['title'] + '. ' + item['text']
        if item.get('terms'):
            text += ' ' + item['id'].partition('.')[0] + ' ' + ', '.join(item['terms'])
        yield {'id':'topic:'+item['id'],'kind':'topic','text':text,'metadata':{'topic':item['id'],'entry':item},'source':item['sources'][0]}


def build_index(output, embedder, source_documents, progress=None):
    """Build a private SQLite snapshot and publish only a complete database."""
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".semantic-build-", dir=output.parent) as directory:
        staging = Path(directory) / "semantic.sqlite3"
        if output.exists():
            with closing(sqlite3.connect(output.as_uri()+"?mode=ro",uri=True)) as source:
                with closing(sqlite3.connect(staging)) as target:
                    source.backup(target)
        store = SemanticStore(staging, embedder)
        try:
            unique, dedup = deduplicate_documents(source_documents)
            result = store.upsert(unique, progress, prune=True)
            result['deduplication'] = dedup
        finally:
            close = getattr(source_documents, "close", None)
            if close:
                close()
        stats = store.stats()
        # A published database is self-contained: checkpoint and leave WAL mode
        # before the atomic rename; never publish a sidecar belonging to staging.
        with closing(sqlite3.connect(staging)) as db:
            if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise RuntimeError("Could not checkpoint staged semantic index")
            if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
                raise RuntimeError("Could not close staged semantic WAL")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Staged semantic index failed integrity check")
        stats["bytes"] = staging.stat().st_size
        if output.exists():
            # Path-named WAL sidecars must not attach to the new inode.
            # If another reader keeps WAL busy, preserve the old complete DB.
            with closing(sqlite3.connect(output, timeout=5)) as old:
                if old.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                    raise RuntimeError("Live semantic index is busy")
                if old.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
                    raise RuntimeError("Could not close live semantic WAL")
                old.execute("BEGIN EXCLUSIVE")
                try:
                    os.replace(staging, output)
                finally:
                    old.rollback()
        else:
            os.replace(staging, output)
        return result, stats


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'.runtime/neural.json')
    parser.add_argument('--output',type=Path,default=ROOT/'data/semantic.sqlite3')
    args=parser.parse_args()
    started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='funny-embed-index-') as temp:
        state=Path(temp);config=json.loads(args.config.read_text())
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));config['embedding']['port']=sock.getsockname()[1]
        (state/'neural.json').write_text(json.dumps(config))
        token=state/'token';token.write_text(secrets.token_urlsafe(32));token.chmod(0o600)
        model=create_model(state,profile='embedding');embedder=Embedder(model)
        try:
            def progress(inserted,unchanged):
                if inserted%256==0:
                    print(json.dumps({'indexed':inserted,'unchanged':unchanged,'seconds':round(time.monotonic()-started,2)}),flush=True)
            result,stats=build_index(args.output,embedder,documents(),progress)
            print(json.dumps({'result':result,'stats':stats,'memory':model.stats(),'seconds':round(time.monotonic()-started,2)},ensure_ascii=False),flush=True)
        finally:
            model.close()


if __name__=='__main__':main()
