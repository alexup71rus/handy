from contextlib import closing
import hashlib
import math
import os
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from folder_memory.embeddings import Embedder, EmbeddingError
from folder_memory.semantic import SemanticStore, pack, scores
from scripts.build_semantic_index import build_index


class EncoderModel:
    enabled=True
    def __init__(self,path):
        self.config={'dimensions':2,'model_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'model':str(path),'name':'fixture'}
        self.lock=threading.RLock();self.calls=[];self.closes=0
    def _start(self):pass
    def check_budget(self):pass
    def close(self):self.closes+=1
    def _request(self,path,body,timeout):
        if path == '/tokenize':
            raw = list(body['content'].encode())
            return {'tokens': [1, *raw, 2] if body.get('add_special') else raw}
        self.calls.append(body)
        return {'data':[{'index':i,'embedding':[3,4]} for i,_ in enumerate(body['input'])]}


class FakeEmbedder:
    enabled=True
    fingerprint='fixture-v1'
    def encode(self,texts,**kwargs):return [[1.0,0.0] if 'apple' in text else [0.0,1.0] for text in texts]
    def query(self,text):return [1.0,0.0]


class SemanticTests(unittest.TestCase):
    def test_encoder_uses_required_prefixes_normalization_and_bounded_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'model.gguf';path.write_bytes(b'original')
            model=EncoderModel(path);encoder=Embedder(model)
            vector=encoder.query('яблоко')
            encoder.query('яблоко');encoder.encode(['apple'])
            self.assertAlmostEqual(sum(x*x for x in vector),1.0)
            self.assertEqual(len(model.calls),2)
            self.assertEqual(model.calls[0]['input'],['query: яблоко'])
            self.assertEqual(model.calls[1]['input'],['passage: apple'])

    def test_unchanged_model_does_not_rehash(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'model.gguf';path.write_bytes(b'original')
            model=EncoderModel(path);encoder=Embedder(model)
            real_open=Path.open;reads=[];canonical=path.resolve()
            def record_open(candidate,*args,**kwargs):
                if candidate==canonical and (args[0] if args else kwargs.get('mode','r'))=='rb':reads.append(candidate)
                return real_open(candidate,*args,**kwargs)
            with patch.object(Path,'open',record_open):
                self.assertTrue(encoder.enabled)
                self.assertTrue(encoder.enabled)
                encoder.query('one');encoder.query('one');encoder.query('two')
                self.assertEqual(len(reads),1)

    def test_busy_live_index_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'semantic.sqlite3';encoder=FakeEmbedder()
            build_index(path,encoder,iter([{'id':'old','kind':'reference','text':'apple'}]))
            live=sqlite3.connect(path)
            try:
                live.execute('BEGIN')
                self.assertEqual(live.execute('select id from documents').fetchall(),[('old',)])
                with self.assertRaises((sqlite3.OperationalError,RuntimeError)):
                    build_index(path,encoder,iter([{'id':'new','kind':'reference','text':'new'}]))
                self.assertEqual(live.execute('select id from documents').fetchall(),[('old',)])
            finally:
                live.close()
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('select id from documents').fetchall(),[('old',)])

    def test_replaced_weights_invalidate_cache_and_reject_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'model.gguf';path.write_bytes(b'original')
            model=EncoderModel(path);encoder=Embedder(model)
            store=SemanticStore(Path(temporary)/'index.sqlite3',encoder)
            store.upsert([{'id':'x','kind':'reference','text':'apple'}])
            encoder.query('query'); encoder.query('query')
            self.assertEqual(len(model.calls),2)
            replacement=path.with_suffix('.new');replacement.write_bytes(b'different');os.replace(replacement,path)
            self.assertFalse(encoder.enabled)
            self.assertEqual(len(encoder.cache),0)
            self.assertEqual(store.search('query',kinds=['reference']),[])
            with self.assertRaises(EmbeddingError):encoder.query('query')
            self.assertEqual(len(model.calls),2)
            self.assertGreaterEqual(model.closes,1)
            model.config['model_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertTrue(encoder.enabled)
            self.assertEqual(store.search('query',kinds=['reference']),[])
            encoder.query('query');self.assertEqual(len(model.calls),3)

    def test_index_staging_preserves_complete_live_data_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'semantic.sqlite3';encoder=FakeEmbedder()
            old=[{'id':'old','kind':'reference','text':'apple old'}]
            build_index(path,encoder,iter(old))
            new=[{'id':'new'+str(i),'kind':'reference','text':'apple new '+str(i)} for i in range(9)]
            seen=[]
            def fail_after_batch(inserted, skipped):
                with closing(sqlite3.connect(path)) as live:
                    seen.append(live.execute('select id from documents').fetchall())
                raise RuntimeError('interrupted')
            with self.assertRaises(RuntimeError):build_index(path,encoder,iter(new),fail_after_batch)
            with closing(sqlite3.connect(path)) as live:
                self.assertEqual(live.execute('select id from documents').fetchall(),[('old',)])
            self.assertEqual(seen,[[('old',)]])
            build_index(path,encoder,iter(new))
            with closing(sqlite3.connect(path)) as live:
                self.assertEqual(live.execute('select count(*) from documents').fetchone()[0],9)
                self.assertEqual(live.execute('pragma integrity_check').fetchone()[0],'ok')

    def test_cosine_scores_agree_with_scalar_reference(self):
        result=scores([pack([1,0]),pack([0,1]),pack([.6,.8])],[.6,.8])
        for actual,expected in zip(result,[.6,.8,1]):self.assertAlmostEqual(actual,expected,places=6)
        with self.assertRaises(ValueError):scores([b'bad'],[1,0])

    def test_persisted_vectors_filter_scope_and_model_and_prune_removed_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            encoder=FakeEmbedder();store=SemanticStore(Path(temporary)/'index.sqlite3',encoder)
            docs=[{'id':'a','kind':'reference','text':'apple source','metadata':{'proof':1}}, {'id':'b','kind':'topic','text':'apple question'}, {'id':'c','kind':'reference','text':'pear source'}]
            self.assertEqual(store.upsert(docs)['inserted'],3)
            hits=store.search('яблоко',kinds=['reference'],limit=2)
            self.assertEqual([h['id'] for h in hits],['a','c'])
            self.assertEqual(hits[0]['metadata'],{'proof':1})
            self.assertEqual(store.upsert(docs)['unchanged'],3)
            encoder.fingerprint='different-model'
            self.assertEqual(store.search('яблоко',kinds=['reference']),[])
            encoder.fingerprint='fixture-v1'
            store.upsert(docs[:1],prune=True)
            self.assertEqual(store.stats()['documents'],1)

    def test_wrong_dimension_or_nonfinite_vectors_do_not_become_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            store=SemanticStore(Path(temporary)/'index.sqlite3',FakeEmbedder())
            store.upsert([{'id':'a','kind':'reference','text':'apple'}])
            with store.connect(create=True) as db:
                db.execute('UPDATE documents SET vector=?',(pack([math.nan,0]),));db.commit()
            self.assertEqual(store.search('query',kinds=['reference']),[])


if __name__=='__main__':unittest.main()
