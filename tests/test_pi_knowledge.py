import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from folder_memory import pi_knowledge as knowledge


class PiKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.state = self.base / 'state'
        self.root = self.base / 'project'; self.root.mkdir()
        self.other = self.base / 'other'; self.other.mkdir()
        self.personal = dict(scope='personal', subject='Сервис-X', field='телефон поддержки',
                             value='8 800 123-45-67', evidence='Запомни телефон поддержки Сервис-X: 8 800 123-45-67')
        self.project = dict(scope='project', title='Запуск тестов', body='npm test',
                            evidence='Запомни: в этом проекте тесты запускаются через npm test')

    def test_personal_recall_survives_reopen_and_is_not_workspace_scoped(self):
        self.assertEqual(knowledge.memory_recall(self.state, self.root, scope='personal',subject='Сервис-X')['records'], [])
        self.assertFalse(self.state.exists())
        receipt = knowledge.memory_save(self.state, self.root, **self.personal)
        self.assertTrue(receipt['saved'])
        recalled = knowledge.memory_recall(self.state, self.other, scope='personal', subject='Сервис-X')
        self.assertEqual(recalled['records'][0]['value'], self.personal['value'])
        self.assertNotIn('evidence', recalled['records'][0])
        with closing(sqlite3.connect(self.state/'memory.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM personal_facts').fetchone()[0], 1)

    def test_project_scope_is_shared_with_subdirectory_but_not_other_project(self):
        (self.root/'.git').mkdir(); nested=self.root/'src';nested.mkdir()
        receipt=knowledge.memory_save(self.state,nested,**self.project)
        row=receipt['records'][0]
        self.assertEqual(knowledge.memory_recall(self.state,self.root,scope='project',title='Запуск тестов')['records'][0]['body'],'npm test')
        self.assertEqual(knowledge.memory_recall(self.state,self.other,scope='project',title='Запуск тестов')['records'],[])
        with self.assertRaises(ValueError):
            knowledge.memory_delete(self.state,self.other,scope='project',record_id=row['id'],version=row['version'])
        self.assertTrue(knowledge.memory_delete(self.state,self.root,scope='project',record_id=row['id'],version=row['version'])['deleted'])

    def test_save_requires_source_value_and_rejects_mixed_scope_fields(self):
        with self.assertRaises(ValueError):
            knowledge.memory_save(self.state,self.root,**{**self.personal,'evidence':'Другое сообщение пользователя'})
        with self.assertRaises(ValueError):
            knowledge.memory_save(self.state,self.root,**self.project,subject='me')
        self.assertEqual(knowledge.memory_recall(self.state,self.root,scope='personal',subject='Сервис-X')['records'],[])

    def test_delete_checks_revision_and_erases_saved_history_and_receipts(self):
        receipt=knowledge.memory_save(self.state,self.root,**self.personal);old=receipt['records'][0]
        updated={**self.personal,'value':'8 800 999-00-11','evidence':'Обнови телефон: 8 800 999-00-11'}
        current=knowledge.memory_save(self.state,self.root,**updated)['records'][0]
        with self.assertRaises(ValueError):
            knowledge.memory_delete(self.state,self.root,scope='personal',record_id=old['id'],version=old['version'])
        knowledge.memory_delete(self.state,self.root,scope='personal',record_id=current['id'],version=current['version'])
        with closing(sqlite3.connect(self.state/'memory.sqlite3')) as db:
            for table in ('personal_fact_history','memory_chat_usage','personal_memory_operations','memory_tool_plans'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],0,table)
            self.assertEqual(db.execute("SELECT subject,field,value,evidence,source FROM personal_facts").fetchone(),('','','','','deleted'))
        self.assertEqual(knowledge.memory_recall(self.state,self.root,scope='personal',subject='Сервис-X')['records'],[])
        replacement=knowledge.memory_save(self.state,self.root,**self.personal)['records'][0]
        self.assertNotEqual(replacement['id'],old['id'])
        with self.assertRaises(ValueError):
            knowledge.memory_delete(self.state,self.root,scope='personal',record_id=old['id'],version=old['version'])

    def test_all_reply_records_fit_single_utf8_budget(self):
        for n in range(5):
            value='Запись '+str(n)+' '+('Ж'*3000)
            knowledge.memory_save(self.state,self.root,scope='personal',subject='Тест',field='поле '+str(n),value=value,evidence='Запомни '+value)
        result=knowledge.memory_recall(self.state,self.root,scope='personal',subject='Тест',limit=5)
        self.assertLessEqual(knowledge._size(result),knowledge.MAX_REPLY_BYTES)
        self.assertTrue(result['has_more'])
        self.assertTrue(result['records'][0]['truncated'])

    def test_reference_lookup_has_sources_and_never_contacts_model_or_service(self):
        with patch.object(knowledge,'_service_references',side_effect=AssertionError('Exact match must stay local')):
            result=knowledge.knowledge_search(self.state,self.root,'Что означает HTTP 404?')
        self.assertTrue(result['records'])
        self.assertEqual(result['mode'],'reference')
        self.assertFalse(result['semantic_used'])
        self.assertTrue(result['records'][0]['source'].startswith('https://'))
        self.assertLessEqual(knowledge._size(result),knowledge.MAX_REPLY_BYTES)

    def test_library_name_finds_topic_instead_of_interpreter_manual(self):
        with patch.object(knowledge,'_service_references',side_effect=AssertionError('Known topic stays local')):
            for query in ('python asyncio', 'Python asyncio.TaskGroup', 'Что такое asyncio в Python?'):
                with self.subTest(query=query):
                    result=knowledge.knowledge_search(self.state,self.root,query)
                    self.assertIn('asyncio-task.html',result['records'][0]['source'])
                    self.assertIn('asyncio.create_task',result['records'][0]['text'])

    def test_semantic_fallback_is_bounded_and_reports_real_unavailability(self):
        with patch.object(knowledge.ReferenceLibrary,'answer_sources',return_value=[]), patch.object(knowledge,'_service_references',return_value=([
                {'source':'https://example.com/reference','text':'Ж'*8000},
                {'source':'https://example.com/second','text':'Ж'*8000},
                {'source':'https://example.com/third','text':'Ж'*8000}],None)):
            result=knowledge.knowledge_search(self.state,self.root,'technical paraphrase')
        self.assertTrue(result['semantic_used']);self.assertTrue(result['has_more'])
        self.assertLessEqual(knowledge._size(result),knowledge.MAX_REPLY_BYTES)
        with patch.object(knowledge.ReferenceLibrary,'answer_sources',return_value=[]), patch.object(knowledge,'_service_references',side_effect=OSError('offline')):
            result=knowledge.knowledge_search(self.state,self.root,'technical paraphrase')
        self.assertFalse(result['semantic_used']);self.assertEqual(result['records'],[])
        self.assertIn('warning',result)

    def test_mcp_tools_reopen_real_memory_and_live_disabled_settings_block_calls(self):
        server=FastMCP('test');knowledge.register_knowledge(server,self.state,self.root)
        async def check():
            names={tool.name for tool in await server.list_tools()}
            self.assertEqual(names,{'memory_recall','memory_save','memory_delete','knowledge_search'})
            _,receipt=await server.call_tool('memory_save',self.personal)
            _,recalled=await server.call_tool('memory_recall',{'scope':'personal','subject':'Сервис-X'})
            self.assertEqual(recalled['records'][0]['value'],self.personal['value'])
            _,sourced=await server.call_tool('knowledge_search',{'query':'Что означает HTTP 404?'})
            self.assertTrue(sourced['records'])
            with patch('folder_memory.pi_runtime.load_settings',return_value={'memory_enabled':False,'rag_enabled':False}):
                with self.assertRaises(ToolError):
                    await server.call_tool('memory_recall',{'scope':'personal','subject':'Сервис-X'})
                with self.assertRaises(ToolError):
                    await server.call_tool('knowledge_search',{'query':'Что означает HTTP 404?'})
        asyncio.run(check())

    def test_authenticated_service_rag_uses_encoder_endpoint_and_filters_legacy_capabilities(self):
        import os
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from folder_memory.runtime import service_proof, write_json
        self.state.mkdir()
        token='a'*40
        (self.state/'token').write_text(token)
        requests=[]
        ready=[True]
        state=self.state
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reply(self,payload):
                raw=json.dumps(payload).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                requests.append(self.path)
                if self.path.startswith('/api/status?nonce='):
                    nonce=self.path.partition('=')[2]
                    status={'name':'funny','owner':'managed','pid':os.getpid(),'stopped':False}
                    status['proof']=service_proof(token,nonce,state,status)
                    self.reply(status)
                elif self.path=='/health':self.reply({'semantic':{'ready':ready[0]}})
                else:raise AssertionError(self.path)
            def do_POST(self):
                requests.append(self.path)
                assert self.path=='/api/query'
                assert self.headers['Authorization']=='Bearer '+token
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert body['root']==str(self_root)
                self.reply({'data':{'records':[
                    {'source':'https://docs.example/reference','text':'Matching semantic reference'},
                    {'source':'Документация Handy','text':'Obsolete price watcher capability'},
                    {'source':'Подтвержденная память','value':'Unrequested private memory'}]}})
        self_root=self.root
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        write_json(self.state/'engine.json',{'port':server.server_port})
        with patch.object(knowledge.ReferenceLibrary,'answer_sources',return_value=[]):
            result=knowledge.knowledge_search(self.state,self.root,'semantic test')
            self.assertTrue(result['semantic_used'])
            self.assertEqual(len(result['records']),1)
            self.assertEqual(result['records'][0]['text'],'Matching semantic reference')
            ready[0]=False;before=requests.count('/api/query')
            result=knowledge.knowledge_search(self.state,self.root,'semantic test')
            self.assertFalse(result['semantic_used']);self.assertIn('warning',result)
            self.assertEqual(requests.count('/api/query'),before)


if __name__=='__main__':unittest.main()
