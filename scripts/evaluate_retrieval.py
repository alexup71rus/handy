"""Audit local technical references and measure retrieval with --run."""

import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import secrets
import socket
import sqlite3
import statistics
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from folder_memory.embeddings import Embedder
from folder_memory.knowledge import ReferenceLibrary
from folder_memory.knowledge_quality import audit_documents
from folder_memory.model_backend import create_model
from folder_memory.semantic import SemanticStore
from scripts.build_semantic_index import documents


_KINDS = {'reference': ['reference'], 'topic': ['topic'], 'unknown': ['reference', 'topic']}


def audit_index(path):
    path=Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        rows=[{'id':r[0],'kind':r[1],'text':r[2],'metadata':json.loads(r[3]),'source':r[4]}
              for r in db.execute('SELECT id,kind,text,metadata,source FROM documents ORDER BY id')]
        models=dict(db.execute('SELECT model_hash,count(*) FROM documents GROUP BY model_hash'))
        integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
    return {**audit_documents(rows),'model_hashes':models,'bytes':path.stat().st_size,'integrity_check':integrity}


def relevant(hit,case):
    metadata=hit.get('metadata',{})
    if case['group']=='topic' and hit['kind']=='topic':
        return metadata.get('topic') in case['topics']
    if case['group']=='reference' and hit['kind']=='reference':
        return metadata.get('entry',{}).get('name') in case.get('commands',[])
    return False


def summarize(rows):
    positives=[row for row in rows if row['group']!='unknown']
    times=[row['seconds'] for row in rows]
    return {'queries':len(rows),'positive_queries':len(positives),
            'recall_at_1':sum(row['rank']==1 for row in positives)/len(positives) if positives else None,
            'recall_at_5':sum(row['rank'] is not None for row in positives)/len(positives) if positives else None,
            'mrr_at_5':sum(1/row['rank'] if row['rank'] else 0 for row in positives)/len(positives) if positives else None,
            'unknown_scores_above_reference_threshold':sum(row.get('score_above_reference_threshold',False) for row in rows),
            'p50_seconds':statistics.median(times) if times else None,
            'p95_seconds':sorted(times)[max(0,math.ceil(len(times)*.95)-1)] if times else None}


def evaluate(store,fixture):
    rows = []
    for case in fixture['cases']:
        group = case['group']
        if group == 'routing':
            continue
        if group not in _KINDS:
            raise ValueError(f'Unknown retrieval case group {group!r}')
        query = case['query']
        kinds = _KINDS[group]
        start = time.monotonic()
        store.embedder.query(query)
        embedding_seconds = time.monotonic() - start
        start = time.monotonic()
        hits = store.search(query, kinds=kinds, limit=5)
        search_seconds = time.monotonic() - start
        rank = next((index for index, hit in enumerate(hits, 1) if relevant(hit, case)), None)
        row = {
            'id': case['id'], 'group': group, 'query': query, 'kinds': kinds, 'rank': rank,
            'seconds': embedding_seconds + search_seconds,
            'query_embedding_seconds': embedding_seconds, 'search_seconds': search_seconds,
            'hits': [{key: hit[key] for key in ('id', 'kind', 'score')} for hit in hits],
        }
        if group == 'unknown':
            row['score_above_reference_threshold'] = bool(hits and hits[0]['score'] >= .77)
            row['application_reference_empty'] = not ReferenceLibrary(store).search(query)
            row['explicit_topic_empty'] = not ReferenceLibrary.topic_search(query)
        rows.append(row)
    groups = {group: summarize([row for row in rows if row['group'] == group])
              for group in sorted({row['group'] for row in rows})}
    return {'summary': summarize(rows), 'groups': groups, 'rows': rows}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',type=Path,default=ROOT/'tests/fixtures/retrieval_quality_v2.json')
    parser.add_argument('--index',type=Path,default=ROOT/'data/semantic.sqlite3')
    parser.add_argument('--config',type=Path,default=ROOT/'.runtime/neural.json')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--catalog',action='store_true',help='List developer technologies, topics and sources without loading a model.')
    parser.add_argument('--run',action='store_true')
    args=parser.parse_args()
    if args.catalog:
        print(json.dumps(ReferenceLibrary.developer_catalog(), ensure_ascii=False, indent=2))
        return
    if args.output is None:
        parser.error('--output is required unless --catalog is used')
    raw=args.fixture.read_bytes();fixture=json.loads(raw)
    if len({case['id'] for case in fixture['cases']})!=len(fixture['cases']):raise ValueError('Duplicate query ids')
    report={'scope':'Technical reference and topic retrieval. Measures relevant matches, unknown-query scores and retrieval latency.',
            'fixture_sha256':hashlib.sha256(raw).hexdigest(),'source_audit':audit_documents(documents()),'index_audit':audit_index(args.index)}
    report['topic_matcher']=[{'id':case['id'],'expected':case.get('topics',[]),'actual':[item['id'].removeprefix('topic:') for item in ReferenceLibrary.topic_search(case['query'])]} for case in fixture['cases'] if case['group'] in {'topic','unknown'}]
    if args.run:
        with tempfile.TemporaryDirectory(prefix='funny-retrieval-eval-') as temp:
            state=Path(temp);config=json.loads(args.config.read_text())
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));config['embedding']['port']=sock.getsockname()[1]
            (state/'neural.json').write_text(json.dumps(config));token=state/'token';token.write_text(secrets.token_urlsafe(32));token.chmod(0o600)
            model=create_model(state,profile='embedding');encoder=Embedder(model);store=SemanticStore(args.index,encoder)
            try:
                if not store.stats()['ready']:raise ValueError('Index fingerprint does not match the selected encoder; build a private index for that model first')
                report['retrieval']=evaluate(store,fixture)
                report['memory']=model.stats()
            finally:model.close()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'fixture_sha256':report['fixture_sha256'],'source_documents':report['source_audit']['documents'],
                      'index_documents':report['index_audit']['documents'],
                      'results':report.get('retrieval',{}).get('summary',{})},ensure_ascii=False))


if __name__=='__main__':main()
