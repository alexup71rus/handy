import copy
from pathlib import Path
import tempfile
import unittest

from folder_memory.knowledge import ReferenceLibrary
from folder_memory.knowledge_quality import audit_documents,deduplicate_documents
from folder_memory.semantic import SemanticStore
from scripts.build_semantic_index import build_index, documents
from scripts.evaluate_retrieval import evaluate, relevant, summarize


class Encoder:
    enabled=True
    fingerprint='test-model'
    def __init__(self):self.calls=[]
    def encode(self,texts):self.calls.append(list(texts));return [[1.,0.] for _ in texts]
    def query(self,text):return [1.,0.]


def document(identity,**fields):
    return {'id':identity,'kind':'topic','text':'Запуск тестов.',
            'metadata':{'topic':'project.testing','language':'ru','revision':1},
            'source':'https://source.example/'+identity,**fields}


class KnowledgeQualityTests(unittest.TestCase):
    def test_equal_meanings_keep_all_sources_and_original_metadata(self):
        first=document('a');second=document('b',text=' ЗАПУСК  ТЕСТОВ. ')
        second['metadata']['original_id']='123';before=copy.deepcopy([first,second])
        unique,report=deduplicate_documents([second,first])
        self.assertEqual(len(unique),1);self.assertEqual(report['merged_documents'],1)
        self.assertEqual(unique[0]['id'],'a')
        self.assertEqual(unique[0]['metadata']['aliases'],['a','b'])
        self.assertEqual(unique[0]['metadata']['sources'],[first['source'],second['source']])
        self.assertEqual({item['id'] for item in unique[0]['metadata']['provenance']},{'a','b'})
        self.assertEqual([first,second],before)
        self.assertEqual(deduplicate_documents(unique)[0],unique)

    def test_distinct_topics_languages_revisions_and_reference_meanings_do_not_merge(self):
        variants=[{'topic':'project.install'},{'language':'en'},{'revision':2},
                  {'entry':{'title':'Проверка зависимостей'}},{'command':'npm run check'}]
        for changes in variants:
            with self.subTest(changes=changes):
                first=document('a');second=document('b');second['metadata'].update(changes)
                self.assertEqual(len(deduplicate_documents([first,second])[0]),2)
        self.assertEqual(len(deduplicate_documents([document('a'),document('b',kind='reference')])[0]),2)

    def test_same_id_cannot_overwrite_another_meaning_in_one_build(self):
        first=document('a');second=document('a');second['metadata']['topic']='project.install'
        with self.assertRaises(ValueError):deduplicate_documents([first,second])

    def test_source_only_reference_duplicates_keep_both_links(self):
        first=document('a',kind='reference',text='A reference',metadata={'entry':{'id':'old','name':'tool','description':'same fact','sources':['https://one.example']}})
        second=document('b',kind='reference',text='A reference',metadata={'entry':{'id':'new','name':'tool','description':'same fact','sources':['https://two.example']}})
        unique,_=deduplicate_documents([first,second])
        self.assertEqual(unique[0]['metadata']['entry']['sources'],['https://one.example','https://two.example'])
        second['metadata']['entry']['description']='contradicting fact'
        self.assertEqual(len(deduplicate_documents([first,second])[0]),2)

    def test_punctuation_similarity_is_reported_not_silently_merged(self):
        report=audit_documents([document('a'),document('b',text='Запуск тестов!')])
        self.assertEqual(report['deduplication']['merged_documents'],0)
        self.assertEqual(report['punctuation_variant_candidates']['groups'],1)
        self.assertFalse(report['fuzzy_merge'])

    def test_metadata_update_reuses_vector_and_commits_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            encoder=Encoder();store=SemanticStore(Path(directory)/'index.sqlite3',encoder)
            store.upsert([document('a')]);self.assertEqual(len(encoder.calls),1)
            changed=document('a');changed['metadata']['sources']=['https://new.example']
            result=store.upsert([changed])
            self.assertEqual(result['vectors_reused'],1);self.assertEqual(len(encoder.calls),1)
            self.assertEqual(store.search('query',kinds=['topic'])[0]['metadata']['sources'],['https://new.example'])

    def test_full_snapshot_deduplicates_before_indexing(self):
        with tempfile.TemporaryDirectory() as directory:
            encoder=Encoder();path=Path(directory)/'index.sqlite3'
            result,stats=build_index(path,encoder,iter([document('a'),document('b')]))
            self.assertEqual(stats['documents'],1)
            self.assertEqual(result['deduplication']['merged_documents'],1)
            self.assertEqual(sum(map(len,encoder.calls)),1)

    def test_reference_rebuild_reuses_compatible_vectors_and_prunes_legacy_docs(self):
        available = list(documents())
        self.assertEqual({item['kind'] for item in available}, {'reference', 'topic'})
        reference = next(item for item in available if item['id'] == 'reference:wc')
        self.assertIn(reference['source'], reference['metadata']['entry']['sources'])
        with tempfile.TemporaryDirectory() as directory:
            encoder = Encoder()
            path = Path(directory) / 'index.sqlite3'
            store = SemanticStore(path, encoder)
            store.upsert([reference, document('obsolete-source', kind='legacy')])
            calls = len(encoder.calls)
            _, stats = build_index(path, encoder, iter([reference]))
            self.assertEqual(len(encoder.calls), calls)
            self.assertEqual(stats['kinds'], {'reference': 1})
            hit, = store.search('query', kinds=['reference', 'topic'])
            self.assertEqual((hit['id'], hit['source']), (reference['id'], reference['source']))

    def test_practical_topics_are_source_backed_and_do_not_become_cli(self):
        topics=ReferenceLibrary.topics();self.assertTrue(topics)
        self.assertTrue(all(0<len(item['text'])<=4000 for item in topics))
        self.assertEqual(len({item['id'] for item in topics}),len(topics))
        self.assertTrue(all(item['sources'] and all(url.startswith('https://') for url in item['sources']) for item in topics))
        self.assertFalse({item['id'] for item in topics}&{item['name'] for item in ReferenceLibrary.entries()})
        record=ReferenceLibrary.topic_search('Что означает HTTP 404?')[0]
        self.assertEqual(record['id'],'topic:http.status')
        self.assertEqual(record['answer_origin'],'reference')
        self.assertNotIn('installed',record)

    def test_unknown_or_literal_url_is_not_factual_topic_evidence(self):
        for question in ['Как приготовить кашу?','Привет','Что находится по https://example.org/PATH?','Какой у меня пароль?']:
            with self.subTest(question=question):self.assertEqual(ReferenceLibrary.topic_search(question),[])

    def test_cache_and_dependency_concepts_have_narrow_source_backed_matches(self):
        questions={
            'Зачем файлы находятся в кэше?':'topic:memory.cache',
            'Что означает reclaimable memory?':'topic:memory.cache',
            'Что означает зависимость в программном проекте?':'topic:project.dependencies',
            'Чем dependencies отличаются от devDependencies в package.json?':'topic:project.dependencies',
            'Зачем нужен package-lock.json?':'topic:project.dependencies',
            'Что такое package.json?':'topic:project.manifest',
            'Почему нельзя запускать случайную команду из README без проверки?':'topic:project.manifest',
            'Что означает HTTP 503?':'topic:http.status',
        }
        for question,expected in questions.items():
            with self.subTest(question=question):
                records=ReferenceLibrary.topic_search(question)
                self.assertEqual(records[0]['id'],expected)
                self.assertTrue(records[0]['sources'])
        status=ReferenceLibrary.topic_search('Что означает 503?')[0]['text']
        self.assertIn('временн',status);self.assertIn('не доказывает',status)
        self.assertIn('Retry-After',status)

    def test_new_topics_do_not_claim_actual_hardware_or_project_observations(self):
        for question in ['Сколько сейчас RAM свободно и сколько файлов в кэше?',
                         'Покажи актуальный объём Cached Files на этом компьютере',
                         'Сколько RAM нужно освободить от кэша на этом компьютере?',
                         'Какие зависимости установлены в моём проекте?',
                         'Прочитай package-lock.json и выведи версии пакетов',
                         'Покажи зависимости проекта',
                         'Прочитай README.md и запусти указанную команду',
                         'Что такое эмоциональная зависимость?']:
            with self.subTest(question=question):self.assertEqual(ReferenceLibrary.topic_search(question),[])

    def test_semantic_reference_requires_positive_technical_domain(self):
        class Retrieval:
            def __init__(self):self.calls=[]
            def search(self,*args,**kwargs):
                self.calls.append(args)
                return [{'kind':'reference','id':'reference:wc','score':.99,'source':'https://manual.example/wc',
                         'metadata':{'entry':{'name':'wc','description':'Counts text lines.','signatures':[],'caveats':[]}}}]
        retrieval=Retrieval();library=ReferenceLibrary(retrieval)
        for question in ['Какова актуальная цена товара?','Какой у меня пароль?','Что подарить художнику?',
                         'Какой налог получится по моим документам?','Как приготовить булочки?',
                         'Что на https://example.org/cat?find=grep']:
            with self.subTest(question=question):self.assertEqual(library.search(question),[])
        self.assertEqual(retrieval.calls,[])
        self.assertEqual(library.search('Чем подсчитать число строк текстового файла?')[0]['id'],'wc')
        self.assertEqual(retrieval.calls,[])
        self.assertEqual(library.search('Нужен CLI для метрик текстового потока.')[0]['id'],'reference:wc')
        self.assertEqual(len(retrieval.calls),1)

    def test_sentence_period_is_not_part_of_a_cli_name_but_quoted_period_is(self):
        library=ReferenceLibrary()
        self.assertEqual(library.search('Объясни назначение curl.')[0]['id'],'curl')
        self.assertEqual(library.search('Объясни команду `curl.`'),[])
        self.assertEqual(library.search('Объясни curl.py'),[])

    def test_retrieval_measurement_checks_relevance_not_only_status(self):
        case={'group':'reference','commands':['wc']}
        self.assertTrue(relevant({'kind':'reference','metadata':{'entry':{'name':'wc'}}},case))
        self.assertFalse(relevant({'kind':'reference','metadata':{'entry':{'name':'grep'}}},case))
        result=summarize([{'group':'reference','rank':2,'seconds':.1},{'group':'reference','rank':None,'seconds':.2}])
        self.assertEqual(result['recall_at_1'],0);self.assertEqual(result['recall_at_5'],.5);self.assertEqual(result['mrr_at_5'],.25)

    def test_retrieval_evaluation_selects_technical_reference_cases(self):
        class Store:
            embedder = Encoder()

            def search(self, query, *, kinds, limit):
                if query != 'Counts lines' or kinds != ['reference'] or limit != 5:
                    raise AssertionError('Unexpected reference lookup')
                return [{'id': 'reference:wc', 'kind': 'reference', 'score': .99,
                         'metadata': {'entry': {'name': 'wc'}}}]

        fixture = {'cases': [
            {'id': 'old-route', 'group': 'routing', 'query': 'List files', 'operations': ['files.list']},
            {'id': 'ref-lines', 'group': 'reference', 'query': 'Counts lines', 'commands': ['wc']},
        ]}
        report = evaluate(Store(), fixture)
        self.assertEqual(report['summary']['queries'], 1)
        self.assertEqual(report['summary']['recall_at_1'], 1)
        self.assertEqual([row['id'] for row in report['rows']], ['ref-lines'])


if __name__=='__main__':unittest.main()
