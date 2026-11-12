from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from folder_memory.memory_tool import finish_memory, prepare_memory, profile_memory, recall_memory
from folder_memory.personal_memory import PersonalMemory
from folder_memory.query_api import query
from folder_memory.storage import Store


class MemoryToolTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root, self.other = self.base / 'project', self.base / 'other'
        self.root.mkdir()
        self.other.mkdir()
        self.app = SimpleNamespace(db=self.base / 'memory.sqlite3', semantic=None)
        Store(self.app.db).close()
        self.personal = {'scope': 'personal', 'subject': 'Сервис-X', 'field': 'телефон поддержки',
                         'value': '8 800 123-45-67', 'evidence': 'Запомни телефон поддержки Сервис-X: 8 800 123-45-67'}
        self.project = {'scope': 'project', 'title': 'Запуск тестов', 'body': 'npm test',
                        'evidence': 'В этом проекте тесты запускаются через npm test'}

    def prepare(self, options=None, *, root=None, session='chat'):
        return prepare_memory(self.app, root or self.root, session, self.personal if options is None else options)

    def finish(self, prepared, *, approved=True, root=None, session='chat'):
        return finish_memory(self.app, root or self.root, session, prepared['data']['plan_id'], approved=approved)

    def recall(self, options=None, *, root=None):
        return recall_memory(self.app, root or self.root, 'new-chat',
                             options or {'scope': 'personal', 'subject': 'Сервис-X'})['data']['records']

    def test_personal_memory_requires_confirmation_and_is_global(self):
        prepared = self.prepare()
        self.assertEqual(prepared['status'], 'approval_required')
        self.assertEqual(prepared['data']['preview']['value'], self.personal['value'])
        self.assertIsNone(prepared['data']['preview']['previous_value'])
        self.assertEqual(self.recall(), [])
        result = self.finish(prepared)
        self.assertTrue(result['data']['performed'])
        self.assertEqual(self.finish(prepared), result)
        self.assertEqual(self.recall(root=self.other)[0]['value'], self.personal['value'])
        reopened = SimpleNamespace(db=self.app.db)
        records = recall_memory(reopened, self.other, 'other-harness',
                                {'scope': 'personal', 'subject': 'Сервис-X', 'field': 'телефон поддержки'})['data']['records']
        self.assertEqual(records[0]['id'], result['data']['record']['id'])
        self.assertNotIn('evidence', records[0])

    def test_project_memory_uses_worktree_and_stays_scoped(self):
        (self.root / '.git').write_text('gitdir: /not-used')
        nested = self.root / 'src'
        nested.mkdir()
        prepared = self.prepare(self.project, root=nested)
        self.assertEqual(prepared['data']['preview']['root'], str(self.root))
        result = self.finish(prepared, root=nested)
        self.assertEqual(result['data']['record']['root'], str(self.root))
        request = {'scope': 'project', 'title': self.project['title']}
        self.assertEqual(self.recall(request, root=nested)[0]['body'], 'npm test')
        self.assertEqual(self.recall(request, root=self.other), [])
        matches = self.recall({'scope': 'project', 'query': 'ЗАПУСК'})
        self.assertEqual(matches[0]['title'], self.project['title'])

    def test_rejection_and_expiry_cannot_be_replayed_as_approval(self):
        cancelled = self.prepare()
        result = self.finish(cancelled, approved=False)
        self.assertTrue(result['data']['cancelled'])
        self.assertEqual(result['data']['reason'], 'user_declined')
        self.assertEqual(self.finish(cancelled), result)
        expired = self.prepare()
        with patch('folder_memory.memory_tool.time.time', return_value=expired['data']['expires_at'] + 1):
            result = self.finish(expired)
        self.assertEqual(result['data']['code'], 'expired')
        self.assertEqual(self.finish(expired), result)
        self.assertEqual(self.recall(), [])

    def test_approval_is_bound_to_session_and_root(self):
        prepared = self.prepare()
        for values in ({'root': self.other}, {'session': 'other'}):
            with self.subTest(values=values), self.assertRaises(PermissionError):
                self.finish(prepared, **values)
        self.assertEqual(self.recall(), [])

    def test_stale_personal_and_project_previews_do_not_overwrite(self):
        for initial, field, value in ((self.personal, 'value', '8 800 999-00-11'),
                                      (self.project, 'body', 'npm run test')):
            with self.subTest(scope=initial['scope']):
                self.finish(self.prepare(initial))
                changed = {**initial, field: value, 'evidence': f'Пользователь указал {value}'}
                stale = self.prepare(changed)
                newer = {**changed, field: 'newer exact value', 'evidence': 'Пользователь указал newer exact value'}
                self.finish(self.prepare(newer))
                failed = self.finish(stale)
                self.assertEqual(failed['data']['code'], 'memory_conflict')
                self.assertIs(failed['data']['performed'], False)
                self.assertEqual(self.finish(stale), failed)
                request = ({'scope': 'personal', 'subject': initial['subject']} if initial['scope'] == 'personal'
                           else {'scope': 'project', 'title': initial['title']})
                self.assertEqual(self.recall(request)[0][field], 'newer exact value')

    def test_replay_returns_original_snapshot_after_later_update(self):
        prepared = self.prepare()
        first = self.finish(prepared)
        options = {**self.personal, 'value': 'new value', 'evidence': 'Запомни new value'}
        self.finish(self.prepare(options))
        self.assertEqual(self.finish(prepared), first)
        self.assertEqual(self.recall()[0]['value'], 'new value')

    def test_concurrent_confirmations_have_one_result(self):
        prepared = self.prepare()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.finish(prepared), range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.recall()), 1)
        with closing(sqlite3.connect(self.app.db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM personal_memory_operations').fetchone()[0], 1)

    def test_profile_needs_explicit_consent_and_supports_flag_updates(self):
        options = {'scope': 'personal', 'subject': 'Пользователь', 'field': 'имя',
                   'value': 'Саша', 'evidence': 'Меня зовут Саша'}
        saved = self.finish(self.prepare(options))
        self.assertEqual(profile_memory(self.app), [])
        enabled = self.prepare({**options, 'use_in_chat': True})
        self.assertFalse(enabled['data']['preview']['previous_use_in_chat'])
        self.assertTrue(enabled['data']['preview']['use_in_chat'])
        self.assertEqual(profile_memory(self.app), [])
        result = self.finish(enabled)
        self.assertEqual(result['data']['record']['id'], saved['data']['record']['id'])
        self.assertEqual(result['data']['record']['version'], 1)
        self.assertEqual(profile_memory(self.app), [{'source': 'Подтверждённый профиль пользователя',
                                                   'subject': 'Пользователь', 'field': 'имя', 'value': 'Саша'}])
        self.finish(self.prepare({**options, 'use_in_chat': False}))
        self.assertEqual(profile_memory(self.app), [])

    def test_contacts_unknown_fields_and_other_people_cannot_be_profile(self):
        for options in ({**self.personal, 'use_in_chat': True},
                        {'scope': 'personal', 'subject': 'Друг', 'field': 'имя', 'value': 'Лена',
                         'evidence': 'Подругу зовут Лена', 'use_in_chat': True},
                        {'scope': 'personal', 'subject': 'Пользователь', 'field': 'любая информация', 'value': 'example',
                         'evidence': 'Запомни example', 'use_in_chat': True},
                        {'scope': 'personal', 'subject': 'Пользователь', 'field': 'имя',
                         'value': 'token=super-secret', 'evidence': 'Имя token=super-secret', 'use_in_chat': True},
                        {**self.project, 'use_in_chat': True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.prepare(options)
        self.finish(self.prepare())
        self.assertEqual(profile_memory(self.app), [])

    def test_profile_flag_conflict_requires_new_confirmation(self):
        options = {'scope': 'personal', 'subject': 'Пользователь', 'field': 'язык',
                   'value': 'русский', 'evidence': 'Отвечай на русском: русский'}
        self.finish(self.prepare(options))
        first = self.prepare({**options, 'use_in_chat': True})
        stale = self.prepare({**options, 'use_in_chat': True})
        self.finish(first)
        failed = self.finish(stale)
        self.assertEqual(failed['data']['code'], 'memory_conflict')
        self.assertEqual(profile_memory(self.app)[0]['value'], 'русский')

    def test_failed_application_rolls_back_fact_and_receipt(self):
        prepared = self.prepare(self.project)
        from folder_memory.memory_tool import _save_project

        def fail_after_save(db, proposal):
            _save_project(db, proposal)
            raise RuntimeError('fixture failure')

        with patch('folder_memory.memory_tool._save_project', side_effect=fail_after_save), self.assertRaises(RuntimeError):
            self.finish(prepared)
        self.assertEqual(self.recall({'scope': 'project', 'title': self.project['title']}), [])
        self.assertTrue(self.finish(prepared)['data']['performed'])

    def test_grounding_and_malformed_values_are_rejected(self):
        for options in ({**self.personal, 'value': 'invented'},
                        {**self.project, 'body': 'invented'},
                        {**self.project, 'evidence': '{"npm test": "different"}'},
                        {**self.personal, 'use_in_chat': 1},
                        {**self.personal, 'scope': 'unknown'},
                        {**self.personal, 'extra': True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.prepare(options)
        self.assertEqual(self.recall(), [])

    def test_existing_personal_fact_remains_available(self):
        with closing(sqlite3.connect(self.app.db)) as db:
            memory = PersonalMemory(db)
            proposal = memory.prepare('Старый сервис', 'контакт', 'AB-042', 'Запомни AB-042')
            memory.save(proposal, 'old-harness-confirmation')
            db.commit()
        self.assertEqual(self.recall({'scope': 'personal', 'subject': 'Старый сервис'})[0]['value'], 'AB-042')
        self.assertEqual(profile_memory(self.app), [])

    def test_confirmed_facts_are_available_through_existing_rag_lookup(self):
        self.finish(self.prepare())
        personal = query(self.app, {'input': 'Какой телефон поддержки Сервис-X?', 'root': str(self.other)})
        self.assertIn(self.personal['value'], personal['text'])
        self.finish(self.prepare(self.project))
        project = query(self.app, {'input': self.project['title'], 'root': str(self.root)})
        self.assertIn(self.project['body'], project['text'])
        outside = query(self.app, {'input': self.project['title'], 'root': str(self.other)})
        self.assertNotIn(self.project['body'], outside['text'])

    def test_personal_query_finds_saved_long_subject_without_replacing_exact_identity(self):
        subject = 'телефон поддержки тестового сервиса Смок'
        options = {'scope': 'personal', 'subject': subject, 'field': 'support_phone',
                   'value': '555 0101', 'evidence': 'Запомни телефон поддержки тестового сервиса Смок: 555 0101'}
        self.finish(self.prepare(options))
        reopened = SimpleNamespace(db=self.app.db)
        result = recall_memory(reopened, self.other, 'another-harness-session',
                               {'scope': 'personal', 'query': 'Смок'})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['records'], [])
        candidate, = result['data']['candidates']
        self.assertEqual((candidate['subject'], candidate['field'], candidate['value']),
                         (subject, 'support_phone', '555 0101'))
        self.assertEqual(candidate['matched_fields'], {'subject': ['смок']})
        self.assertEqual(candidate['source'], 'user-confirmed')
        self.assertNotIn('evidence', candidate)
        self.assertEqual(result['data']['match']['mode'], 'all_word_terms')
        self.assertEqual(self.recall({'scope': 'personal', 'subject': 'Смок'}), [])
        self.assertEqual(profile_memory(self.app), [])

    def test_personal_query_disambiguates_multiple_saved_subjects(self):
        for subject, value in (('Сервис Смок', '555 0101'), ('Мастерская Смок', '555 0202')):
            self.finish(self.prepare({'scope': 'personal', 'subject': subject, 'field': 'support_phone',
                                      'value': value, 'evidence': f'{subject}: {value}'}))
        result = recall_memory(self.app, self.other, 'other-session', {'scope': 'personal', 'query': 'Смок'})
        self.assertEqual(result['status'], 'needs_input')
        self.assertTrue(result['data']['ambiguous'])
        self.assertEqual(result['data']['records'], [])
        self.assertEqual({row['subject']: row['value'] for row in result['data']['candidates']},
                         {'Сервис Смок': '555 0101', 'Мастерская Смок': '555 0202'})
        limited = recall_memory(self.app, self.other, 'other-session',
                                {'scope': 'personal', 'query': 'Смок', 'limit': 1})
        self.assertEqual(limited['status'], 'needs_input')
        self.assertTrue(limited['data']['has_more'])
        self.assertEqual(len(limited['data']['candidates']), 1)

    def test_personal_query_matches_all_terms_and_respects_an_exact_field_filter(self):
        self.finish(self.prepare({'scope': 'personal', 'subject': 'Сервис Смок', 'field': 'support_phone',
                                  'value': '555 0101', 'evidence': 'Сервис Смок: 555 0101'}))
        for args in ({'query': 'Смок отсутствующее'}, {'query': 'Незнакомый'},
                     {'query': 'Смок', 'field': 'email'}, {'query': 'Смок', 'field': 'phone'}):
            with self.subTest(args=args):
                response = recall_memory(self.app, self.root, 'search', {'scope': 'personal', **args})
                self.assertEqual(response['data']['candidates'], [])
                self.assertEqual(response['data']['records'], [])
        response = recall_memory(self.app, self.root, 'search',
                                 {'scope': 'personal', 'query': 'СМОК support 0101', 'field': 'SUPPORT_PHONE'})
        candidate, = response['data']['candidates']
        self.assertEqual(candidate['matched_fields'], {'subject': ['смок'], 'field': ['support'], 'value': ['0101']})

    def test_personal_query_unicode_word_normalization_preserves_saved_values(self):
        subject = 'Кафе Café Ёлка'
        self.finish(self.prepare({'scope': 'personal', 'subject': subject, 'field': 'reference_code',
                                  'value': 'Case-Sensitive-AB42', 'evidence': 'Запомни Case-Sensitive-AB42'}))
        response = recall_memory(self.app, self.root, 'search',
                                 {'scope': 'personal', 'query': 'CAFE\u0301 елка'})
        candidate, = response['data']['candidates']
        self.assertEqual(candidate['subject'], subject)
        self.assertEqual(candidate['value'], 'Case-Sensitive-AB42')

    def test_personal_query_reads_only_confirmed_facts_and_creates_no_tables(self):
        with closing(sqlite3.connect(self.app.db)) as db:
            before = db.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
        empty = recall_memory(self.app, self.root, 'search', {'scope': 'personal', 'query': 'Сервис'})
        self.assertEqual(empty['data']['candidates'], [])
        with closing(sqlite3.connect(self.app.db)) as db:
            self.assertEqual(db.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall(), before)
        pending = self.prepare()
        self.assertEqual(recall_memory(self.app, self.root, 'search',
                         {'scope': 'personal', 'query': 'Сервис'})['data']['candidates'], [])
        self.finish(pending)
        with closing(sqlite3.connect(self.app.db)) as db:
            db.execute("UPDATE personal_facts SET source='user-provided'")
            db.commit()
            before = db.execute('SELECT * FROM personal_facts').fetchall()
        response = recall_memory(self.app, self.root, 'search', {'scope': 'personal', 'query': 'Сервис'})
        self.assertEqual(response['data']['candidates'], [])
        with closing(sqlite3.connect(self.app.db)) as db:
            self.assertEqual(db.execute('SELECT * FROM personal_facts').fetchall(), before)

    def test_personal_query_validation_and_scope_are_explicit(self):
        for args in ({'query': None}, {'query': []}, {'query': ''}, {'query': ' '},
                     {'query': 'a' * 241}, {'query': '!!!'},
                     {'query': 'Смок', 'subject': 'Смок'}, {'query': 'Смок', 'subject': None},
                     {'query': 'Смок', 'field': None}, {'query': 'Смок', 'limit': True}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                recall_memory(self.app, self.root, 'search', {'scope': 'personal', **args})
        with self.assertRaises(ValueError):
            recall_memory(self.app, self.root, 'search', {'query': 'Смок'})
