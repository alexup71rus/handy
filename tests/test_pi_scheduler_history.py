"""Deleting scheduler history never deletes chats, jobs or active executions."""
import json
from pathlib import Path
import tempfile
import unittest

from folder_memory.pi_scheduler import _database, create_job, delete_run, list_jobs, list_runs
from folder_memory.pi_control import dispatch


class SchedulerHistoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = self.root / 'state'
        self.chat = self.root / 'saved-chat.jsonl'
        self.chat.write_text('{"type":"session"}\n')
        self.job = create_job(self.state, title='A task', prompt='Test request', cwd=str(self.root), run_at=2000000000)
        with _database(self.state) as db:
            for identifier, status in [('done', 'succeeded'), ('failed', 'failed'), ('active', 'running')]:
                db.execute('INSERT INTO runs(id,job_id,title,cwd,session_path,status,started_at,answer) VALUES(?,?,?,?,?,?,?,?)',
                           (identifier, self.job['id'], 'Test run', str(self.root), str(self.chat), status, 1, 'Test answer'))

    def test_bridge_deletes_finished_result_and_retains_chat_and_schedule(self):
        before = self.chat.read_bytes()
        result = dispatch(self.state, 'delete-run', {'run_id':'done'})
        self.assertEqual(result, {'id':'done', 'deleted':True})
        self.assertNotIn('done', {run['id'] for run in list_runs(self.state)})
        self.assertEqual(list_jobs(self.state)[0]['id'], self.job['id'])
        self.assertEqual(self.chat.read_bytes(), before)

    def test_failed_run_can_be_removed_and_missing_or_running_run_cannot(self):
        delete_run(self.state, 'failed')
        with self.assertRaisesRegex(ValueError, 'выполняющейся'):
            delete_run(self.state, 'active')
        with self.assertRaisesRegex(ValueError, 'не найден'):
            delete_run(self.state, 'missing')
        self.assertEqual({run['id'] for run in list_runs(self.state)}, {'active','done'})


if __name__ == '__main__':
    unittest.main()
