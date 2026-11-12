from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from folder_memory import pi_scheduler as scheduler


class PiSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / 'state'

    def tearDown(self):
        self.temporary.cleanup()

    def create(self, **changes):
        settings = dict(title='Check', prompt='Answer now', cwd=self.root, run_at=time.time() - 1)
        settings.update(changes)
        return scheduler.create_job(self.state, **settings)

    def test_validation_prevents_ambiguous_and_invalid_schedules(self):
        for changes in ({'run_at': None}, {'interval_seconds': 0}, {'run_at': float('nan')},
                        {'cwd': 'relative'}, {'prompt': ' '}, {'title': ''}, {'run_at': True}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, OSError)):
                self.create(**changes)
        self.assertEqual(scheduler.list_jobs(self.state), [])

    def test_one_shot_produces_independent_session_and_does_not_repeat(self):
        job = self.create()
        sessions = []

        def execute(state, record, session, stop):
            self.assertEqual(record['prompt'], 'Answer now')
            sessions.append(session)
            return 'Actual answer'

        with patch.object(scheduler, '_execute', side_effect=execute):
            runs = scheduler.tick(self.state)
            self.assertEqual(scheduler.tick(self.state), [])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['status'], 'succeeded')
        self.assertEqual(runs[0]['answer'], 'Actual answer')
        self.assertEqual(runs[0]['cwd'], str(self.root))
        self.assertEqual(runs[0]['session_path'], str(sessions[0]))
        self.assertEqual(sessions[0].parent, self.state / 'pi/sessions')
        self.assertFalse(scheduler.list_jobs(self.state)[0]['enabled'])
        self.assertEqual(runs[0]['completed_at'], runs[0]['finished_at'])
        scheduler.mark_notified(self.state, runs[0]['id'])
        self.assertTrue(scheduler.list_runs(self.state)[0]['notified'])
        scheduler.delete_job(self.state, job['id'])
        self.assertEqual(scheduler.list_jobs(self.state), [])
        self.assertEqual(len(scheduler.list_runs(self.state)), 1)

    def test_recurring_jobs_do_not_catch_up_with_a_burst_after_downtime(self):
        job = self.create(interval_seconds=3600, run_at=time.time() - 86400)
        with patch.object(scheduler, '_execute', return_value='Done') as execute:
            scheduler.tick(self.state)
        self.assertEqual(execute.call_count, 1)
        updated = scheduler.list_jobs(self.state)[0]
        self.assertEqual(updated['id'], job['id'])
        self.assertTrue(updated['enabled'])
        self.assertGreater(updated['next_run'], time.time())

    def test_concurrent_ticks_never_duplicate_a_claim(self):
        self.create()
        entered, release = threading.Event(), threading.Event()

        def execute(*args):
            entered.set()
            release.wait(5)
            return 'Once'

        with patch.object(scheduler, '_execute', side_effect=execute) as execute, ThreadPoolExecutor(2) as pool:
            future = pool.submit(scheduler.tick, self.state)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(scheduler.tick(self.state), [])
            finally:
                release.set()
            self.assertEqual(len(future.result(timeout=5)), 1)
            self.assertEqual(execute.call_count, 1)

    def test_failure_is_recorded_and_one_shot_remains_consumed(self):
        self.create()
        with patch.object(scheduler, '_execute', side_effect=ValueError('Provider unavailable')):
            run = scheduler.tick(self.state)[0]
        self.assertEqual(run['status'], 'failed')
        self.assertIn('Provider unavailable', run['error'])
        self.assertEqual(run['answer'], '')
        self.assertEqual(scheduler.tick(self.state), [])

    def test_edit_pause_and_reenable_apply_without_destroying_history(self):
        job = self.create()
        scheduler.update_job(self.state, job['id'], enabled=False, title='Paused')
        with patch.object(scheduler, '_execute', return_value='Done') as execute:
            self.assertEqual(scheduler.tick(self.state), [])
            execute.assert_not_called()
            scheduler.update_job(self.state, job['id'], enabled=True, prompt='Changed')
            scheduler.tick(self.state)
            self.assertEqual(execute.call_args.args[1]['prompt'], 'Changed')
        self.assertEqual(scheduler.list_runs(self.state)[0]['title'], 'Paused')
        with self.assertRaises(ValueError):
            scheduler.update_job(self.state, job['id'], enabled='yes')
        with self.assertRaises(ValueError):
            scheduler.update_job(self.state, 'missing', title='No')

    def test_interrupted_run_is_failed_on_next_owner_without_reexecution(self):
        self.create()
        with scheduler._database(self.state) as db:
            _, identifier, _ = scheduler._claim(db, self.state)
        self.assertEqual(scheduler.tick(self.state), [])
        run = scheduler.list_runs(self.state)[0]
        self.assertEqual(run['id'], identifier)
        self.assertEqual(run['status'], 'failed')
        self.assertIn('прерван', run['error'])

    def test_event_parser_requires_successful_final_assistant_text(self):
        def event(text, **extra):
            return json.dumps({'type': 'message_end', 'message': {
                'role': 'assistant', 'content': [{'type': 'text', 'text': text}], **extra}}).encode()

        self.assertEqual(scheduler._answer(event('Answer')), 'Answer')
        for raw in (b'', b'not-json', event('Earlier') + b'\n' + event('', stopReason='error', errorMessage='Bad key'),
                    event('Earlier') + b'\n' + event('', stopReason='toolUse'),
                    event('Earlier') + b'\n' + event('Сейчас создам…', stopReason='toolUse')):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                scheduler._answer(raw)
        # Pi may retry a transient provider failure before producing a final answer.
        self.assertEqual(scheduler._answer(event('', stopReason='error') + b'\n' + event('Recovered')), 'Recovered')

    def test_subprocess_contract_checks_exit_answer_and_actual_saved_session(self):
        job = self.create()
        module = ModuleType('folder_memory.pi_runtime')
        session = self.root / 'native.jsonl'

        def command(source):
            import os
            module.scheduled_command = lambda *_: ([sys.executable, '-c', source], dict(os.environ))

        success = json.dumps({'type': 'message_end', 'message': {
            'role': 'assistant', 'content': [{'type': 'text', 'text': 'Real child answer'}]}})
        command(f'from pathlib import Path; Path({str(session)!r}).write_text("session"); print({success!r})')
        with patch.dict(sys.modules, {'folder_memory.pi_runtime': module}):
            self.assertEqual(scheduler._execute(self.state, job, session, threading.Event()), 'Real child answer')
            session.unlink()
            command(f'print({success!r})')
            with self.assertRaisesRegex(ValueError, 'не сохранил сессию'):
                scheduler._execute(self.state, job, session, threading.Event())
            command('import sys; print("Provider failed", file=sys.stderr); sys.exit(2)')
            with self.assertRaisesRegex(ValueError, 'Provider failed'):
                scheduler._execute(self.state, job, session, threading.Event())

    def test_worker_lifecycle_confirms_ownership_and_stops_cleanly(self):
        self.assertFalse(scheduler.worker_status(self.state)['running'])
        try:
            started = scheduler.start_worker(self.state)
            self.assertTrue(started['running'])
            self.assertIsInstance(started['pid'], int)
            self.assertEqual(scheduler.start_worker(self.state)['pid'], started['pid'])
        finally:
            stopped = scheduler.stop_worker(self.state)
        self.assertFalse(stopped['running'])

    def test_start_waits_for_pid_publication_after_lock_is_acquired(self):
        child = Mock()
        child.poll.return_value = None
        states = [{'running': False, 'pid': None}, {'running': True, 'pid': None},
                  {'running': True, 'pid': 12345}]
        with patch.object(scheduler, 'worker_status', side_effect=states), \
                patch.object(scheduler.subprocess, 'Popen', return_value=child), \
                patch.object(scheduler.time, 'sleep'):
            self.assertEqual(scheduler.start_worker(self.state), states[-1])

    def test_disabled_scheduler_neither_claims_tasks_nor_starts_worker(self):
        self.create()
        (self.state / 'pi-settings.json').write_text('{"scheduler_enabled":false}')
        with patch.object(scheduler, '_execute') as execute:
            self.assertEqual(scheduler.tick(self.state), [])
            execute.assert_not_called()
        self.assertTrue(scheduler.list_jobs(self.state)[0]['enabled'])
        self.assertEqual(scheduler.list_runs(self.state), [])
        with self.assertRaisesRegex(ValueError, 'выключен'):
            scheduler.start_worker(self.state)

    def test_running_worker_exits_when_settings_disable_scheduler(self):
        try:
            self.assertTrue(scheduler.start_worker(self.state)['running'])
            (self.state / 'pi-settings.json').write_text('{"scheduler_enabled":false}')
            deadline = time.monotonic() + 5
            while scheduler.worker_status(self.state)['running'] and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(scheduler.worker_status(self.state)['running'])
        finally:
            scheduler.stop_worker(self.state)

    def test_exited_pi_parent_does_not_leave_its_helper_process_group_running(self):
        job = self.create()
        module = ModuleType('folder_memory.pi_runtime')
        import os
        session = self.root / 'native.jsonl'
        heartbeat = self.root / 'helper-heartbeat'
        helper = ("import signal,time\nfrom pathlib import Path\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  f"path=Path({str(heartbeat)!r})\n"
                  "while True:\n with path.open('a') as f: f.write('x')\n time.sleep(0.02)\n")
        response = json.dumps({'type': 'message_end', 'message': {
            'role': 'assistant', 'content': [{'type': 'text', 'text': 'Finished'}]}})
        source = ("import subprocess,sys,time\nfrom pathlib import Path\n"
                  f"subprocess.Popen([sys.executable,'-c',{helper!r}])\n"
                  f"while not Path({str(heartbeat)!r}).exists(): time.sleep(0.01)\n"
                  f"Path({str(session)!r}).write_text('session')\nprint({response!r})\n")
        module.scheduled_command = lambda *_: ([sys.executable, '-c', source], dict(os.environ))
        with patch.dict(sys.modules, {'folder_memory.pi_runtime': module}):
            self.assertEqual(scheduler._execute(self.state, job, session, threading.Event()), 'Finished')
        size = heartbeat.stat().st_size
        time.sleep(0.15)
        self.assertEqual(heartbeat.stat().st_size, size)

    def test_timeout_stops_child_and_records_failure(self):
        self.create()
        module = ModuleType('folder_memory.pi_runtime')
        import os
        module.scheduled_command = lambda *_: ([sys.executable, '-c', 'import time; time.sleep(60)'], dict(os.environ))
        with patch.dict(sys.modules, {'folder_memory.pi_runtime': module}), patch.object(scheduler, 'RUN_TIMEOUT', 0.01):
            started = time.monotonic()
            run = scheduler.tick(self.state)[0]
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(run['status'], 'failed')
        self.assertIn('время', run['error'])

    def test_excessive_stdout_or_stderr_stops_run_before_timeout(self):
        import os
        module = ModuleType('folder_memory.pi_runtime')
        for source in ("import sys; sys.stdout.write('x'*4096); sys.stdout.flush()",
                       "import sys,time; sys.stderr.write('x'*4096); sys.stderr.flush(); time.sleep(60)"):
            with self.subTest(source=source):
                self.create()
                module.scheduled_command = lambda *_: ([sys.executable, '-c', source], dict(os.environ))
                with patch.dict(sys.modules, {'folder_memory.pi_runtime': module}), \
                        patch.object(scheduler, 'MAX_OUTPUT', 1024), patch.object(scheduler, 'RUN_TIMEOUT', 5):
                    started = time.monotonic()
                    run = scheduler.tick(self.state)[0]
                self.assertLess(time.monotonic() - started, 3)
                self.assertEqual(run['status'], 'failed')
                self.assertIn('Вывод задачи', run['error'])

    def test_disabling_settings_cancels_an_active_prompt(self):
        self.create()
        module = ModuleType('folder_memory.pi_runtime')
        import os
        module.scheduled_command = lambda *_: ([sys.executable, '-c', 'import time; time.sleep(60)'], dict(os.environ))

        def disable():
            time.sleep(0.1)
            (self.state / 'pi-settings.json').write_text('{"scheduler_enabled":false}')

        changer = threading.Thread(target=disable)
        with patch.dict(sys.modules, {'folder_memory.pi_runtime': module}):
            changer.start()
            try:
                run = scheduler.tick(self.state)[0]
            finally:
                changer.join()
        self.assertEqual(run['status'], 'failed')
        self.assertIn('выключен', run['error'])


if __name__ == '__main__':
    unittest.main()
