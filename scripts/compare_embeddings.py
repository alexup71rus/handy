"""Compare configured encoders privately; stop production model workers first.
No downloads, tool execution or live-index publication.
"""

import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from folder_memory.embeddings import Embedder
from folder_memory.knowledge_quality import audit_documents
from folder_memory.memory_budget import descendant_pids,tree_memory
from folder_memory.model_backend import create_model
from folder_memory.semantic import SemanticStore
from scripts.build_semantic_index import build_index,documents
from scripts.evaluate_retrieval import evaluate


def save(path,report):
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


def seed(source,target):
    if target.exists() or not source.is_file():return False
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as old:
        with closing(sqlite3.connect(target)) as new:old.backup(new)
    return True


def run_variant(label,configuration,index,source_documents,fixture,work_dir,seed_index=None):
    result={'variant':label,'status':'running','index':index.name}
    if seed_index:result['seeded_existing_vectors']=seed(seed_index,index)
    peak={'rss_bytes':0,'charged_bytes':0,'samples':0,'sample_interval_seconds':.25,'sampled_lower_bound':True}
    stop=threading.Event()
    def sample():
        while not stop.is_set():
            values=tree_memory(descendant_pids(os.getpid()))
            for key in ('rss_bytes','charged_bytes'):peak[key]=max(peak[key],values.get(key) or 0)
            peak['samples']+=1
            stop.wait(.25)
    monitor=threading.Thread(target=sample,daemon=True);monitor.start()
    started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='.encoder-'+label+'-',dir=work_dir) as directory:
        state=Path(directory);config=json.loads(configuration.read_text())
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));config['embedding']['port']=sock.getsockname()[1]
        (state/'neural.json').write_text(json.dumps(config))
        token=state/'token';token.write_text(secrets.token_urlsafe(32));token.chmod(0o600)
        model=create_model(state,profile='embedding');encoder=Embedder(model)
        result['encoder']={key:model.config.get(key) for key in ['name','dimensions','model_sha256','pooling','query_prefix','passage_prefix','context','batch_tokens','micro_batch_tokens']}
        try:
            begin=time.monotonic()
            passage=encoder.encode(['Порт — сетевая точка обслуживания; PID обозначает процесс.'])[0]
            query=encoder.query('Чем номер порта отличается от PID?')
            result['probe']={'seconds':time.monotonic()-begin,'dimensions':len(query),
                             'query_norm':math.sqrt(sum(value*value for value in query)),
                             'passage_norm':math.sqrt(sum(value*value for value in passage)),
                             'query_passage_cosine':sum(a*b for a,b in zip(query,passage)),
                             'encoder_fingerprint':encoder.fingerprint}
            if len(query)!=int(model.config['dimensions']):raise ValueError('Unexpected encoder dimensions')
            print(json.dumps({'variant':label,'event':'probe','result':result['probe']}),flush=True)
            begin=time.monotonic();last_progress=begin
            def progress(inserted,unchanged):
                nonlocal last_progress
                now=time.monotonic()
                if now-last_progress>=20:
                    print(json.dumps({'variant':label,'event':'index_progress','inserted':inserted,'unchanged':unchanged,'seconds':round(now-begin,2)}),flush=True)
                    last_progress=now
            build,stats=build_index(index,encoder,iter(source_documents),progress)
            result['build']={'seconds':time.monotonic()-begin,'result':build,'stats':stats}
            print(json.dumps({'variant':label,'event':'index_done','seconds':result['build']['seconds'],'result':build,'documents':stats['documents']}),flush=True)
            result['retrieval']=evaluate(SemanticStore(index,encoder),fixture)
            result['memory']={key:value for key,value in model.stats().items() if key in {'tree_rss_bytes','peak_tree_rss_bytes','peak_charged_bytes','reported_buffers_bytes','budget_bytes'}}
            result['status']='complete'
        except Exception as exc:
            result['status']='failed';result['error']=type(exc).__name__+': '+str(exc)
            log=state/'embedding-llama.log'
            if log.exists():
                target=work_dir/(label+'-failure.log');target.write_bytes(log.read_bytes()[-32768:]);target.chmod(0o600)
        finally:
            model.close();stop.set();monitor.join(timeout=3)
    result['peak_tree']=peak;result['seconds']=time.monotonic()-started
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-config',type=Path,default=ROOT/'.runtime/neural.json')
    parser.add_argument('--candidate-config',type=Path,required=True)
    parser.add_argument('--seed-index',type=Path,default=ROOT/'data/semantic.sqlite3')
    parser.add_argument('--fixture',type=Path,default=ROOT/'tests/fixtures/retrieval_quality_v2.json')
    parser.add_argument('--work-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--run',action='store_true',required=True)
    args=parser.parse_args();args.work_dir.mkdir(parents=True,exist_ok=True);args.output.parent.mkdir(parents=True,exist_ok=True)
    raw=args.fixture.read_bytes();fixture=json.loads(raw);source_documents=list(documents())
    report={'scope':'Private sequential encoder/retrieval comparison on one immutable source-document snapshot; no task execution or answer generation.',
            'fixture_sha256':hashlib.sha256(raw).hexdigest(),'source_audit':audit_documents(source_documents),'variants':[]}
    save(args.output,report)
    for label,config in [('baseline',args.baseline_config),('candidate',args.candidate_config)]:
        result=run_variant(label,config,args.work_dir/(label+'.sqlite3'),source_documents,fixture,args.work_dir,args.seed_index if label=='baseline' else None)
        report['variants'].append(result);save(args.output,report)
        print(json.dumps({'variant':label,'event':'finished','status':result['status'],'seconds':result['seconds'],
                          'summary':result.get('retrieval',{}).get('summary',{}),'peak':result['peak_tree'],'error':result.get('error')},ensure_ascii=False),flush=True)
        if result['status']!='complete':raise SystemExit(1)


if __name__=='__main__':main()
