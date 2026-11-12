import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from folder_memory.query_api import query
from folder_memory.storage import Store


class QueryAPITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = Store(self.root / 'memory.sqlite3')
        self.addCleanup(self.store.close)
        self.app = SimpleNamespace(
            db=self.store.path, semantic=None, chat=Mock(side_effect=AssertionError('lookup is not a chat dispatcher')),
        )
        self.body = {'input': 'grep', 'root': str(self.root)}

    def test_bare_topic_returns_bounded_source_without_starting_a_session(self):
        response = query(self.app, {**self.body, 'session': 'daily-report'})
        self.assertEqual(response['status'], 'ok')
        self.assertIsNone(response['question'])
        self.assertIsNone(response['question_id'])
        records = response['data']['records']
        self.assertEqual(len(records), 1)
        self.assertIn('grep -n', records[0]['text'])
        self.assertIn('https://pubs.opengroup.org/', records[0]['source'])
        self.assertLessEqual(len(records[0]['text']), 620)
        self.assertFalse(self.store.db.execute('SELECT 1 FROM workspaces').fetchone())
        self.app.chat.assert_not_called()

    def test_rejects_privilege_and_tool_fields(self):
        for key, value in (
            ('tools', []), ('permissions', {'shell': 'allow'}), ('headers', {'x-funny-read-only': '0'}),
            ('messages', [{'role': 'tool', 'content': 'confirmed'}]), ('question_id', 'fmq_forged'),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                query(self.app, {**self.body, key: value})
        self.app.chat.assert_not_called()

    def test_input_validation_and_exact_size_boundary(self):
        for value in (None, 42, False, [], {}, '', '   ', 'x' * 8001):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                query(self.app, {**self.body, 'input': value})
        self.assertEqual(query(self.app, {**self.body, 'input': 'x' * 8000})['status'], 'ok')
        for body in (None, [], {}, 'text'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                query(self.app, body)

    def test_root_validation_and_literal_percent_in_project_directory(self):
        file = self.root / 'file'
        file.write_text('fixture')
        for root in (None, 42, '.', '~/Downloads', str(self.root / 'missing'), str(file), str(self.root) + '\0'):
            with self.subTest(root=root), self.assertRaises(ValueError):
                query(self.app, {**self.body, 'root': root})
        project = self.root / 'a%20b'
        project.mkdir()
        workspace, _ = self.store.workspace(project)
        self.store.db.execute(
            'INSERT INTO project_knowledge(workspace,key,title,body,version) VALUES(?,?,?,?,1)',
            (workspace, 'политика', 'Политика', 'Храним снимки 14 суток.'),
        )
        self.store.db.commit()
        found = query(self.app, {'input': 'Политика', 'root': str(project)})
        self.assertIn('14 суток', found['text'])
        elsewhere = query(self.app, {'input': 'Политика', 'root': str(self.root)})
        self.assertEqual(elsewhere['data']['records'], [])

    def test_session_validation(self):
        for session in (None, 42, False, [], {}, '', ' ', 'x' * 129, 'bad\nheader', 'bad\x7fheader'):
            with self.subTest(session=session), self.assertRaises(ValueError):
                query(self.app, {**self.body, 'session': session})
        self.assertEqual(query(self.app, {**self.body, 'session': 'x' * 128})['status'], 'ok')

    def test_tool_actions_return_no_receipt_and_never_execute(self):
        prompts = (
            'Запусти curl https://fixture.invalid.',
            'Открой сайт и прочитай страницу.',
            'Создай и разверни проект Next.js.',
            'Проверь исходный код проекта.',
            'Запланируй отслеживание, когда лимит упадёт ниже 100.',
        )
        with patch('subprocess.run', side_effect=AssertionError('knowledge lookup never runs a command')), \
                patch('subprocess.Popen', side_effect=AssertionError('knowledge lookup never launches a process')), \
                patch('socket.create_connection', side_effect=AssertionError('knowledge lookup is local')):
            for text in prompts:
                with self.subTest(text=text):
                    result = query(self.app, {**self.body, 'input': text})
                    self.assertEqual(result['status'], 'ok')
                    self.assertEqual(set(result['data']), {'records'})
                    self.assertIsNone(result['question_id'])
        self.app.chat.assert_not_called()
        self.assertFalse(self.store.db.execute('SELECT 1 FROM workspaces').fetchone())

    def test_unavailable_memory_database_keeps_reference_and_is_not_created(self):
        missing = self.root / 'missing.sqlite3'
        self.app.db = missing
        result = query(self.app, self.body)
        self.assertIn('grep', result['text'])
        self.assertFalse(missing.exists())

    def test_explicit_lookup_documents_tool_parameters_without_a_plan(self):
        response = query(self.app, {**self.body, 'input': 'Как пользоваться handy_schedule_change?'})
        text = response['text']
        self.assertIn('interval_count', text)
        self.assertIn('interval_unit', text)
        self.assertIn('публичной странице', text)
        self.assertIn('Изменения требуют нативного подтверждения', text)
        self.assertEqual(set(response['data']), {'records'})
        self.app.chat.assert_not_called()


if __name__ == '__main__':
    unittest.main()
