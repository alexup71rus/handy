import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from folder_memory.artifact_tool import create_artifact
from tests.test_spreadsheet_text import grid, workbook


class ArtifactToolTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()

    def create(self, kind, **options):
        return create_artifact(None, self.root, 'typed-test', kind, options)

    def data(self, result):
        self.assertEqual(result['status'], 'ok', result)
        artifact = result['data']['artifact']
        html, data = Path(artifact['path']), Path(artifact['data_path'])
        self.assertTrue(html.is_relative_to(self.root / '.handy' / 'artifacts'))
        self.assertTrue(html.is_file())
        self.assertEqual(html.parent, data.parent)
        self.assertIn('Content-Security-Policy', html.read_text())
        return json.loads(data.read_text())

    def test_timer_records_exact_duration_and_published_paths(self):
        result = self.create('timer', seconds=90, title='Перерыв')
        self.assertEqual(self.data(result), {'title': 'Перерыв', 'mode': 'countdown', 'durationSeconds': 90})
        self.assertEqual(result['data']['settings']['durationSeconds'], 90)
        self.assertEqual(result['data']['artifact']['kind'], 'timer')

    def test_home_directory_launch_can_save_an_artifact_only_in_its_handy_namespace(self):
        with patch('pathlib.Path.home', return_value=self.root):
            result = self.create('timer', seconds=37)
        self.assertEqual(self.data(result)['durationSeconds'], 37)
        self.assertEqual(set(path.name for path in self.root.iterdir()), {'.handy'})

    def test_configurable_templates_use_normalized_settings(self):
        cases = (
            ('pomodoro', {'focus_minutes': 40, 'short_break_minutes': 7, 'rounds': 3},
             {'title': 'Pomodoro', 'mode': 'pomodoro', 'focusMinutes': 40, 'shortBreakMinutes': 7,
              'longBreakMinutes': 15, 'rounds': 3}),
            ('clock', {'time_zone': 'Москва'}, {'title': 'Часы', 'timeZone': 'Europe/Moscow'}),
            ('clock', {}, {'title': 'Часы'}),
            ('dice', {'count': 3, 'sides': 20}, {'title': 'Бросок кубиков', 'count': 3, 'sides': 20}),
            ('password', {'length': 32, 'groups': ['lowercase', 'digits']},
             {'title': 'Генератор паролей', 'length': 32, 'groups': ['lowercase', 'digits']}),
        )
        for kind, options, expected in cases:
            with self.subTest(kind=kind, options=options):
                self.assertEqual(self.data(self.create(kind, **options)), expected)

    def test_invalid_types_ranges_kinds_and_extra_parameters_do_not_write(self):
        cases = (
            ('timer', {}), ('timer', {'seconds': True}), ('timer', {'seconds': '90'}),
            ('timer', {'seconds': 0}), ('timer', {'seconds': 86401}),
            ('pomodoro', {'focus_minutes': 121}), ('pomodoro', {'rounds': 0}),
            ('dice', {'count': 21}), ('dice', {'sides': 1}),
            ('password', {'length': 3}), ('password', {'groups': []}),
            ('password', {'groups': ['digits', 'digits']}), ('password', {'groups': ['unknown']}),
            ('password', {'groups': [None]}), ('clock', {'time_zone': 'Not/A_Zone'}),
            ('clock', {'time_zone': None}), ('timer', {'seconds': 30, 'title': None}),
            ('timer', {'seconds': 30, 'command': 'echo unsupported'}), ('preview', {}),
            ('table', {'data': 'a\n1', 'format': 'csv', 'density': 'unknown'}),
            ('list', {'data': 'a', 'format': 'text', 'where': []}),
        )
        for kind, options in cases:
            with self.subTest(kind=kind, options=options):
                result = self.create(kind, **options)
                self.assertEqual(result['status'], 'needs_input', result)
                self.assertNotIn('artifact', result['data'])
        self.assertFalse((self.root / '.handy').exists())

    def test_chart_filters_literal_cells_and_returns_small_receipt(self):
        result = self.create('chart', data='month,amount,status\nMay,11,paid\nJun,40,open\n',
                             format='csv', x='month', y='amount', chart_type='bar',
                             where=[{'column': 'status', 'operator': 'eq', 'value': 'paid'}])
        page = self.data(result)
        self.assertEqual(page['chartType'], 'bar')
        self.assertEqual(page['points'], [{'label': 'May', 'value': 11}])
        self.assertEqual(result['data']['points'], 1)
        self.assertNotIn('rows', result['data'])

    def test_json_envelope_and_typed_conditions_preserve_values(self):
        result = self.create('table', data='{"data":{"items":[{"active":true,"amount":1.5},'
                             '{"active":false,"amount":5}]}}', format='json', density='compact',
                             where=[{'column': 'active', 'operator': 'eq', 'value': True}])
        page = self.data(result)
        self.assertEqual(page['density'], 'compact')
        self.assertEqual(page['rows'], [{'c0': 'true', 'c1': 1.5}])
        self.assertEqual(result['data']['where'][0]['value_type'], 'boolean')
        self.assertEqual(result['data']['rows'], 1)

    def test_file_input_is_confined_and_has_real_source_metadata(self):
        source = self.root / 'sales.csv'
        source.write_text('month,amount\nMay,12\n')
        result = self.create('chart', path='sales.csv', x='month', y='amount')
        self.assertEqual(self.data(result)['title'], 'sales')
        self.assertEqual(result['data']['sources'], [str(source)])
        (self.base / 'outside.csv').write_text('secret,value\nprivate,4\n')
        (self.root / 'escape.csv').symlink_to(self.base / 'outside.csv')
        (self.root / '.env.csv').write_text('secret,value\nprivate,4\n')
        for path in ('../outside.csv', str(self.base / 'outside.csv'), 'escape.csv', '.env.csv'):
            with self.subTest(path=path):
                rejected = self.create('table', path=path)
                self.assertEqual(rejected['status'], 'needs_input', rejected)
        self.assertEqual(self.create('table', path='sales.csv', format='json')['status'], 'needs_input')

    def test_list_has_actual_checklist_items(self):
        result = self.create('list', data='- [x] Готово\n- [ ] Проверить\n', format='text')
        page = self.data(result)
        self.assertTrue(page['checklist'])
        self.assertEqual(page['items'], [{'label': 'Готово', 'checked': True}, {'label': 'Проверить', 'checked': False}])
        self.assertEqual(result['data']['items'], 2)

    def test_relative_source_uses_session_directory_and_exports_to_worktree_root(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        nested = self.root / 'nested'
        nested.mkdir()
        source = nested / 'sales.csv'
        source.write_text('month,amount\nMay,17\n')
        result = create_artifact(None, nested, 'typed-test', 'chart', {'path': 'sales.csv'})
        self.assertEqual(self.data(result)['points'], [{'label': 'May', 'value': 17}])
        self.assertEqual(result['data']['sources'], [str(source)])

    def test_missing_ambiguous_or_unsupported_conditions_are_rejected(self):
        source = 'name,a,b\nMay,1,2\n'
        cases = (
            {'data': source}, {'data': source, 'format': 'csv', 'path': 'also.csv'},
            {'data': source, 'format': 'csv'},
            {'data': source, 'format': 'csv', 'x': 'name', 'y': 'missing'},
            {'data': source, 'format': 'csv', 'x': 'name', 'y': 'a',
             'where': [{'column': 'a', 'operator': 'gt', 'value': 0}]},
            {'data': source, 'format': 'csv', 'x': 'name', 'y': 'a',
             'where': [{'column': 'missing', 'operator': 'eq', 'value': 'x'}]},
            {'data': '{"data":[],"items":[]}', 'format': 'json'},
        )
        for options in cases:
            with self.subTest(options=options):
                self.assertEqual(self.create('chart', **options)['status'], 'needs_input')
        self.assertFalse((self.root / '.handy').exists())

    def test_workbook_sheet_selection_and_formula_guard_use_existing_reader(self):
        rows = '<row r="1"><c r="A1" t="inlineStr"><is><t>Month</t></is></c><c r="B1" t="inlineStr"><is><t>Amount</t></is></c></row>'
        (self.root / 'sales.xlsx').write_bytes(workbook([
            ('Sales', grid(rows + '<row r="2"><c r="A2" t="inlineStr"><is><t>May</t></is></c><c r="B2"><v>42</v></c></row>'), 'visible'),
            ('Forecast', grid(rows + '<row r="2"><c r="A2" t="inlineStr"><is><t>May</t></is></c><c r="B2"><f>2+2</f><v>4</v></c></row>'), 'visible'),
        ]))
        self.assertEqual(self.create('table', path='sales.xlsx')['status'], 'needs_input')
        result = self.create('chart', path='sales.xlsx', sheet='Sales', x='Month', y='Amount')
        self.assertEqual(self.data(result)['points'], [{'label': 'May', 'value': 42}])
        self.assertEqual(self.create('chart', path='sales.xlsx', sheet='Forecast')['status'], 'needs_input')

    def test_failed_publication_returns_error_without_success_receipt(self):
        with patch('folder_memory.artifact_tool.save_artifact', side_effect=OSError('write failed')):
            result = self.create('timer', seconds=30)
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('artifact', result['data'])


if __name__ == '__main__':
    unittest.main()
