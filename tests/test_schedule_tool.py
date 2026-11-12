from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from folder_memory.schedule_tool import finish_change, list_schedules, prepare_change
from folder_memory.scheduled_price import PriceUnavailable
from folder_memory.schedules import Schedules


class ScheduleToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.root, self.other = base / 'project', base / 'other'
        self.root.mkdir()
        self.other.mkdir()
        self.app = SimpleNamespace(state_dir=base, db=base / 'memory.sqlite3')
        self.worker = self.enterContext(patch('folder_memory.schedule_tool.start_worker', return_value={'running': True}))
        self.creation = {'operation': 'create', 'url': 'https://shop.example/item',
                         'interval_count': 2, 'interval_unit': 'hour', 'direction': 'down', 'title': 'Тестовый товар'}

    def prepare(self, options=None):
        return prepare_change(self.app, self.root, 'session', self.creation if options is None else options)

    def finish(self, prepared, approved=True):
        return finish_change(self.app, self.root, 'session', prepared['data']['plan_id'], approved=approved)

    def jobs(self, root=None):
        return list_schedules(self.app, root or self.root, 'session')['data']['jobs']

    def test_confirmed_lifecycle_and_exact_replayed_receipt(self):
        prepared = self.prepare()
        self.assertEqual(prepared['status'], 'approval_required')
        preview = prepared['data']['preview']
        self.assertEqual(preview['settings']['interval'], {'count': 2, 'unit': 'hour'})
        self.assertEqual(preview['settings']['url'], self.creation['url'])
        self.assertEqual(preview['condition']['direction'], 'down')
        self.assertIsNone(preview['condition']['numeric_threshold'])
        self.assertEqual(preview['condition']['notification_channel'], 'desktop')
        self.assertFalse(preview['condition']['authenticated_browser'])
        self.assertEqual(preview['root'], str(self.root))
        self.assertEqual(self.jobs(), [])
        self.worker.assert_not_called()

        result = self.finish(prepared)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['data']['condition'], preview['condition'])
        self.assertEqual(self.finish(prepared), result)
        self.worker.assert_called_once()
        job_id = result['data']['job']['id']
        self.assertEqual(job_id, preview['job_id'])
        self.assertEqual(self.jobs()[0]['id'], job_id)
        self.assertEqual(self.jobs(self.other), [])

        edit = self.prepare({'operation': 'edit', 'job_id': job_id,
                             'interval_count': 1, 'interval_unit': 'week', 'title': 'Уточнённое название'})
        edited = self.finish(edit)['data']['job']
        self.assertEqual(edited['interval'], edit['data']['preview']['settings']['interval'])
        self.assertEqual(self.jobs()[0]['title'], 'Уточнённое название')
        for operation, enabled in (('disable', False), ('enable', True)):
            change = self.prepare({'operation': operation, 'job_id': job_id})
            self.assertIs(change['data']['preview']['settings']['enabled'], enabled)
            self.assertIs(self.finish(change)['data']['job']['enabled'], enabled)
        deletion = self.prepare({'operation': 'delete', 'job_id': job_id})
        self.assertEqual(self.finish(deletion)['data']['job']['deleted'], True)
        self.assertEqual(self.jobs(), [])

    def test_preview_is_bound_and_rejection_cannot_be_replayed_as_approval(self):
        prepared = self.prepare()
        plan = prepared['data']['plan_id']
        for root, session in ((self.root, 'another'), (self.other, 'session')):
            with self.subTest(root=root, session=session), self.assertRaises(PermissionError):
                finish_change(self.app, root, session, plan, approved=True)
        cancelled = self.finish(prepared, False)
        self.assertEqual(cancelled['data'], {'plan_id': plan, 'cancelled': True, 'performed': False,
                                             'reason': 'user_declined'})
        self.assertEqual(self.finish(prepared), cancelled)
        self.assertEqual(self.jobs(), [])
        self.worker.assert_not_called()

    def test_expiry_leaves_schedule_untouched(self):
        prepared = self.prepare()
        with patch('folder_memory.schedule_tool.time.time', return_value=prepared['data']['expires_at'] + 1):
            expired = self.finish(prepared)
        self.assertEqual(expired['status'], 'error')
        self.assertEqual(expired['data']['code'], 'expired')
        self.assertEqual(self.finish(prepared), expired)
        self.assertEqual(self.jobs(), [])
        self.worker.assert_not_called()

    def test_approved_stale_revision_cannot_replace_a_newer_change(self):
        job = self.finish(self.prepare())['data']['job']
        prepared = self.prepare({'operation': 'edit', 'job_id': job['id'], 'title': 'Устаревшее имя'})
        Schedules(self.app.state_dir).edit(job['workspace'], job['id'], job['revision'], title='Новое имя')
        result = self.finish(prepared)
        self.assertEqual(result['data']['code'], 'schedule_conflict')
        self.assertIs(result['data']['performed'], False)
        self.assertEqual(self.jobs()[0]['title'], 'Новое имя')

    def test_in_progress_approval_is_not_reexecuted(self):
        prepared = self.prepare()
        started, release = threading.Event(), threading.Event()

        def wait_for_release(_):
            started.set()
            self.assertTrue(release.wait(5))
            return {'running': True}

        self.worker.side_effect = wait_for_release
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(self.finish, prepared)
            try:
                self.assertTrue(started.wait(5))
                duplicate = self.finish(prepared)
                self.assertEqual(duplicate['data']['code'], 'outcome_unknown')
                self.assertEqual(len(self.jobs()), 1)
            finally:
                release.set()
            completed = result.result(timeout=5)
        self.assertEqual(self.finish(prepared), completed)
        self.worker.assert_called_once()

    def test_invalid_parameters_and_private_urls_do_not_make_jobs(self):
        for extra in ({'interval_count': True}, {'interval_count': 0}, {'interval_unit': 'year'},
                      {'url': 'http://127.0.0.1/item'}, {'url': 'https://name:secret@shop.example/item'},
                      {'threshold': 100}, {'job_id': ''}, {'direction': 'below'}, {'title': 'line\nbreak'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.prepare({**self.creation, **extra})
        for options in ({'operation': 'edit', 'job_id': 'a' * 12, 'interval_count': 2},
                        {'operation': 'delete', 'job_id': 'a' * 12, 'url': self.creation['url']}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.prepare(options)
        self.assertEqual(self.jobs(), [])
        self.worker.assert_not_called()

    def test_worker_start_failure_still_reports_the_saved_job(self):
        self.worker.side_effect = OSError('unavailable')
        result = self.finish(self.prepare())
        self.assertEqual(result['status'], 'ok')
        self.assertIsNone(result['data']['worker']['running'])
        self.assertIn('Состояние фонового исполнителя не подтверждено', result['text'])
        self.assertEqual(self.jobs()[0]['id'], result['data']['job']['id'])

    def test_artifact_is_a_snapshot_of_the_current_workspace(self):
        job = self.finish(self.prepare())['data']['job']
        result = list_schedules(self.app, self.root, 'session', artifact=True)
        files = result['data']['artifact']
        self.assertTrue(Path(files['path']).is_file())
        data = json.loads(Path(files['data_path']).read_text())
        self.assertEqual(data['jobs'][0]['id'], job['id'])
        self.assertTrue(data['jobs'][0]['enabled'])
        self.assertFalse(data['worker_running'])
        self.assertIn('Проверки остановлены', data['jobs'][0]['state'])

    def test_large_list_keeps_all_tasks_and_errors_and_saves_the_full_snapshot(self):
        workspace = hashlib.sha256(str(self.root).encode()).hexdigest()[:24]
        host = '.'.join(['h' * 63] * 61 + ['example'])
        title, error = '📦' * 120, 'Ошибка проверки ' + '📦' * 220

        def unavailable(_):
            raise PriceUnavailable(error)

        schedules = Schedules(self.app.state_dir, check=unavailable)
        jobs = [schedules.create(workspace, url=f'https://{host}/item', interval={'count': 1, 'unit': 'day'},
                                 direction='down', title=title, now=0) for _ in range(64)]
        schedules.tick(now=0, limit=64)
        paused = jobs[-1]
        schedules.edit(workspace, paused['id'], paused['revision'], enabled=False, now=0)

        for artifact in (False, True):
            with self.subTest(artifact=artifact):
                result = list_schedules(self.app, self.root, 'session', artifact=artifact)
                self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 55 * 1024)
                data = result['data']
                self.assertTrue(data['details_truncated'])
                self.assertFalse(data['worker_running'])
                self.assertFalse(data['partial'])
                rows = {row['id']: row for row in data['jobs']}
                self.assertEqual(set(rows), {job['id'] for job in jobs})
                self.assertEqual(rows[paused['id']]['state'], 'На паузе')
                self.assertIn('Проверки остановлены', rows[jobs[0]['id']]['state'])
                self.assertTrue(all(row['error'].startswith('Ошибка проверки') for row in rows.values()))
                self.assertTrue(all(row['error'].endswith('…') for row in rows.values()))
                if artifact:
                    snapshot = json.loads(Path(data['artifact']['data_path']).read_text())
                    self.assertEqual(len(snapshot['jobs']), len(jobs))
                    self.assertTrue(all(row['title'] == title and row['host'] == host and row['error'] == error
                                        for row in snapshot['jobs']))


if __name__ == '__main__':
    unittest.main()
