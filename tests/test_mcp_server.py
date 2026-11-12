import asyncio
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from folder_memory.mcp_server import artifact_inventory, build_server, _timestamp


class MCPServerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'workspace'
        self.root.mkdir()
        self.state = self.base / 'state'

    def test_artifact_create_returns_published_files_and_list_reads_them(self):
        async def check():
            server = build_server(self.state, self.root, scheduler=False)
            _, receipt = await server.call_tool('artifact_create', {
                'kind': 'table', 'options': {'data': 'name,count\napples,2\n', 'format': 'csv', 'title': 'Stock'}})
            html = Path(receipt['data']['artifact']['path'])
            self.assertTrue(html.is_file())
            self.assertEqual(receipt['data']['artifact']['uri'], html.as_uri())
            self.assertIn('Stock', html.read_text())
            _, inventory = await server.call_tool('artifact_list', {})
            self.assertEqual(inventory['artifacts'][0]['path'], str(html))
            self.assertEqual(inventory['artifacts'][0]['kind'], 'table')
        asyncio.run(check())

    def test_invalid_artifact_does_not_publish_files(self):
        from mcp.server.fastmcp.exceptions import ToolError
        async def check():
            server = build_server(self.state, self.root, scheduler=False)
            with self.assertRaises(ToolError):
                await server.call_tool('artifact_create', {'kind': 'timer', 'options': {'seconds': -1}})
            self.assertEqual(artifact_inventory(self.root), [])
        asyncio.run(check())

    def test_inventory_does_not_read_symlinked_artifact_data(self):
        directory = self.root / '.handy/artifacts/table-abc'
        directory.mkdir(parents=True)
        (directory / 'index.html').write_text('page')
        outside = self.base / 'private.json'
        outside.write_text('{"title":"private"}')
        (directory / 'data.json').symlink_to(outside)
        self.assertEqual(artifact_inventory(self.root), [])

    def test_iso_schedule_timestamp_requires_timezone(self):
        self.assertEqual(_timestamp('2026-11-11T12:00:00+03:00'), _timestamp('2026-11-11T09:00:00Z'))
        with self.assertRaises(ValueError):
            _timestamp('2026-11-11T12:00:00')

    def test_stdio_protocol_initialize_list_and_call(self):
        async def check():
            command = StdioServerParameters(command=sys.executable, args=[
                '-m', 'folder_memory.mcp_server', '--state-dir', str(self.state),
                '--root', str(self.root), '--no-scheduler'])
            async with stdio_client(command) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.serverInfo.name, 'Handy')
                    tools = await session.list_tools()
                    self.assertEqual({tool.name for tool in tools.tools}, {
                        'artifact_create', 'artifact_list', 'memory_recall', 'memory_save', 'memory_delete', 'knowledge_search'})
                    schema = next(tool.inputSchema for tool in tools.tools if tool.name == 'artifact_create')
                    self.assertIn('clock', schema['properties']['kind']['enum'])
                    options = schema['properties']['options']
                    self.assertNotIn('$ref', options)
                    self.assertEqual(options['properties']['time_zone']['type'], 'string')
                    self.assertIn('America/New_York', options['properties']['time_zone']['description'])
                    self.assertLess(len(json.dumps(schema, separators=(',', ':')).encode()), 2200)
                    created = await session.call_tool('artifact_create', {
                        'kind': 'timer', 'options': {'seconds': 60, 'title': 'Break'}})
                    self.assertFalse(created.isError)
                    payload = created.structuredContent or json.loads(created.content[0].text)
                    self.assertTrue(Path(payload['data']['artifact']['path']).is_file())
                    invalid = await session.call_tool('artifact_create', {'kind': 'timer', 'options': {'seconds': 0}})
                    self.assertTrue(invalid.isError)
                    clock = await session.call_tool('artifact_create', {
                        'kind': 'clock', 'options': {'time_zone': 'America/New_York', 'title': 'Часы в Нью-Йорке'}})
                    self.assertFalse(clock.isError)
                    clock_payload = clock.structuredContent or json.loads(clock.content[0].text)
                    artifact = clock_payload['data']['artifact']
                    self.assertEqual(artifact['kind'], 'clock')
                    self.assertEqual(clock_payload['data']['settings']['timeZone'], 'America/New_York')
                    self.assertEqual(json.loads(Path(artifact['data_path']).read_text())['timeZone'], 'America/New_York')
        asyncio.run(check())

    def test_typed_schema_does_not_coerce_or_drop_invalid_kind_options(self):
        from mcp.server.fastmcp.exceptions import ToolError
        async def check():
            server = build_server(self.state, self.root, scheduler=False)
            for kind, options in [('timer', {'seconds': True}), ('timer', {'seconds': '60'}),
                                  ('timer', {'seconds': 60, 'unknown': 'ignored?'}),
                                  ('clock', {'time_zone': 'America/New_York', 'seconds': 60})]:
                with self.subTest(kind=kind, options=options), self.assertRaises(ToolError):
                    await server.call_tool('artifact_create', {'kind': kind, 'options': options})
            self.assertEqual(artifact_inventory(self.root), [])
        asyncio.run(check())

    def test_scheduler_crud_is_scoped_to_workspace_and_retains_receipt_on_worker_failure(self):
        from folder_memory import pi_scheduler
        from mcp.server.fastmcp.exceptions import ToolError
        other = self.base / 'other'
        other.mkdir()
        foreign = pi_scheduler.create_job(self.state, title='Other', prompt='Private task',
                                          cwd=str(other), run_at=2000000000)
        async def check():
            server = build_server(self.state, self.root, artifacts=False)
            with patch.object(pi_scheduler, 'start_worker', return_value={'running': True, 'pid': 123}):
                _, created = await server.call_tool('schedule_create', {
                    'title': 'Morning', 'prompt': 'Summarize files', 'run_at': '2030-01-01T09:00:00+03:00'})
                job = created['job']
                self.assertEqual(job['cwd'], str(self.root))
                _, inventory = await server.call_tool('schedule_list', {})
                self.assertEqual([item['id'] for item in inventory['jobs']], [job['id']])
                with self.assertRaises(ToolError):
                    await server.call_tool('schedule_delete', {'job_id': foreign['id']})
                with patch.object(pi_scheduler, 'start_worker', side_effect=ValueError('worker unavailable')):
                    _, updated = await server.call_tool('schedule_update', {'job_id': job['id'], 'enabled': False})
                self.assertFalse(updated['job']['enabled'])
                self.assertIn('worker unavailable', updated['worker_error'])
                _, deleted = await server.call_tool('schedule_delete', {'job_id': job['id']})
                self.assertEqual(deleted['deleted'], job['id'])
            self.assertEqual([item['id'] for item in pi_scheduler.list_jobs(self.state)], [foreign['id']])
        asyncio.run(check())

    def test_live_server_respects_features_disabled_after_initialization(self):
        from folder_memory import pi_scheduler
        from mcp.server.fastmcp.exceptions import ToolError
        self.state.mkdir()
        async def check():
            server = build_server(self.state, self.root)
            (self.state / 'pi-settings.json').write_text(json.dumps({
                'artifacts_enabled': False, 'scheduler_enabled': False}))
            with self.assertRaises(ToolError):
                await server.call_tool('artifact_create', {'kind': 'timer', 'options': {'seconds': 30}})
            with patch.object(pi_scheduler, 'start_worker') as worker:
                with self.assertRaises(ToolError):
                    await server.call_tool('schedule_create', {
                        'title': 'Disabled', 'prompt': 'Never run', 'run_at': 2000000000})
                worker.assert_not_called()
            self.assertEqual(pi_scheduler.list_jobs(self.state), [])
            self.assertFalse((self.root / '.handy').exists())
        asyncio.run(check())

    def test_workspace_history_is_limited_after_filtering_foreign_runs(self):
        from folder_memory import pi_scheduler
        with pi_scheduler._database(self.state) as db:
            rows = [('mine', str(self.root), 1), *[
                (f'foreign-{n}', str(self.base / 'other'), n + 2) for n in range(101)]]
            db.executemany('''INSERT INTO runs(id,job_id,title,cwd,session_path,status,started_at)
                VALUES(?,'job','A run',?,'chat.jsonl','succeeded',?)''', rows)
        async def check():
            server = build_server(self.state, self.root, artifacts=False)
            _, result = await server.call_tool('schedule_runs', {})
            self.assertEqual([run['id'] for run in result['runs']], ['mine'])
        asyncio.run(check())

    def test_mcp_cannot_mutate_job_moved_by_app_to_another_workspace(self):
        from folder_memory import pi_scheduler as jobs
        from mcp.server.fastmcp.exceptions import ToolError
        other = self.base / 'other'; other.mkdir()
        async def check():
            server = build_server(self.state, self.root, artifacts=False)
            for tool, changes in [('schedule_update', {'title': 'Wrong title'}), ('schedule_delete', {})]:
                job = jobs.create_job(self.state, title='Original', prompt='A task', cwd=self.root, run_at=2000000000)
                original_list = jobs.list_jobs
                def move_before_mutation(state):
                    snapshot = original_list(state)
                    jobs.update_job(state, job['id'], cwd=other)
                    return snapshot
                with patch.object(jobs, 'list_jobs', side_effect=move_before_mutation), self.assertRaises(ToolError):
                    await server.call_tool(tool, {'job_id': job['id'], **changes})
                saved = next(row for row in jobs.list_jobs(self.state) if row['id'] == job['id'])
                self.assertEqual((saved['cwd'], saved['title']), (str(other), 'Original'))
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
