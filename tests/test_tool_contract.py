import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from folder_memory.artifact_tool import create_artifact
from folder_memory.tool_contract import argument_feedback, selected_contract


class ArgumentRepairTests(unittest.TestCase):
    def test_display_source_exclusivity_is_a_repairable_contract_before_any_file_write(self):
        args = {'kind': 'chart', 'path': 'sales.csv', 'data': 'x,y\na,1', 'format': 'csv', 'chart_type': 'bar'}
        repair = argument_feedback('handy_display', args)['data']['repair']
        self.assertEqual(repair['selected'], {'kind': 'chart'})
        self.assertEqual(repair['alternatives'], [['path'], ['data', 'format']])
        self.assertIn('path', repair['allowed_fields']['$'])
        self.assertIsNone(argument_feedback('handy_display', {'kind': 'chart', 'path': 'sales.csv', 'chart_type': 'bar'}))
    def test_artifact_variants_exclude_other_kinds_and_describe_only_the_selected_options(self):
        args = {'kind': 'pomodoro', 'options': {'focus_minutes': 15, 'count': 1, 'sides': 6, 'length': 24}}
        original = copy.deepcopy(args)
        repair = argument_feedback('handy_artifact', args)['data']['repair']
        self.assertEqual(repair['selected'], {'kind': 'pomodoro'})
        self.assertEqual(set(repair['unexpected_fields']), {'$.options.count', '$.options.sides', '$.options.length'})
        self.assertEqual(set(repair['allowed_fields']['$.options']),
                         {'title', 'focus_minutes', 'short_break_minutes', 'long_break_minutes', 'rounds'})
        self.assertEqual(repair['fields']['$.options.focus_minutes'],
                         {'type': 'integer', 'minimum': 1, 'maximum': 120, 'default': 25})
        self.assertEqual(args, original)
        self.assertIsNone(argument_feedback('handy_artifact', {'kind': 'pomodoro', 'options': {'focus_minutes': 15}}))

    def test_required_timer_fields_and_invalid_types_ranges_are_reported_without_guessing(self):
        repair = argument_feedback('handy_artifact', {'kind': 'timer'})['data']['repair']
        self.assertEqual(set(repair['missing_fields']), {'$.options', '$.options.seconds'})
        for value, rule in ((True, 'type'), ('90', 'type'), (0, 'minimum'), (86401, 'maximum')):
            with self.subTest(value=value):
                repair = argument_feedback('handy_artifact', {'kind': 'timer', 'options': {'seconds': value}})['data']['repair']
                self.assertIn({'path': '$.options.seconds', 'rule': rule,
                               'expected': {'type': 'integer', 'minimum': 1, 'maximum': 86400}[rule]}, repair['issues'])

    def test_memory_variants_require_source_evidence_and_never_mix_personal_and_project_fields(self):
        args = {'operation': 'remember', 'scope': 'project', 'title': 'Запуск', 'value': 'private source text'}
        repair = argument_feedback('handy_memory', args)['data']['repair']
        self.assertEqual(repair['selected'], {'operation': 'remember', 'scope': 'project'})
        self.assertEqual(set(repair['missing_fields']), {'$.body', '$.evidence'})
        self.assertEqual(repair['unexpected_fields'], ['$.value'])
        self.assertNotIn('private source text', json.dumps(repair))
        personal = {'operation': 'remember', 'scope': 'personal', 'subject': 'Сервис', 'field': 'phone',
                    'value': '555', 'evidence': 'Сервис: 555'}
        self.assertIsNone(argument_feedback('handy_memory', personal))
        self.assertIsNotNone(argument_feedback('handy_memory', {**personal, 'body': '555'}))

    def test_recall_requires_the_right_selector_without_saving_fields_or_ambiguous_selectors(self):
        valid = [
            {'operation': 'recall', 'scope': 'personal', 'subject': 'Сервис'},
            {'operation': 'recall', 'scope': 'personal', 'query': 'Сервис', 'field': 'phone'},
            {'operation': 'recall', 'scope': 'project'},
            {'operation': 'recall', 'scope': 'project', 'title': 'Запуск'},
            {'operation': 'recall', 'scope': 'project', 'query': 'тесты'},
        ]
        for args in valid:
            with self.subTest(args=args):
                self.assertIsNone(argument_feedback('handy_memory', args))
        invalid = [
            {'operation': 'recall', 'scope': 'personal'},
            {'operation': 'recall', 'scope': 'personal', 'subject': 'Сервис', 'query': 'Сервис'},
            {'operation': 'recall', 'scope': 'project', 'title': 'Запуск', 'query': 'тесты'},
            {'operation': 'recall', 'scope': 'project', 'evidence': 'не нужно'},
        ]
        for args in invalid:
            with self.subTest(args=args):
                self.assertIsNotNone(argument_feedback('handy_memory', args))
        repair = argument_feedback('handy_memory', invalid[1])['data']['repair']
        self.assertIn('subject', repair['allowed_fields']['$'])
        self.assertIn('query', repair['allowed_fields']['$'])
        self.assertEqual(repair['alternatives'], [['subject'], ['query']])

    def test_scheduler_create_requires_direction_and_lifecycle_operations_exclude_settings(self):
        create = {'operation': 'create', 'url': 'https://shop.example/item',
                  'interval_count': 1, 'interval_unit': 'day', 'direction': 'down'}
        self.assertIsNone(argument_feedback('handy_schedule_change', create))
        missing = {key: value for key, value in create.items() if key != 'direction'}
        self.assertIn('$.direction', argument_feedback('handy_schedule_change', missing)['data']['repair']['missing_fields'])
        self.assertIsNotNone(argument_feedback('handy_schedule_change', {'operation': 'delete',
            'job_id': 'abcdef123456', 'url': create['url']}))

    def test_unknown_variants_are_not_guessed_and_extension_tools_keep_their_own_validator(self):
        args = {'kind': 'not-real', 'options': {}}
        repair = argument_feedback('handy_artifact', args)['data']['repair']
        self.assertEqual(repair['selected'], {})
        self.assertEqual(len(repair['variants']), 5)
        self.assertLess(len(json.dumps(repair)), 1600)
        self.assertIsNone(selected_contract('extension_tool', {}))
        self.assertIsNone(argument_feedback('list_directory', {'path': '.'}))

    def test_artifact_core_rejects_invalid_arguments_before_any_write_and_accepts_a_corrected_call(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch('folder_memory.artifact_tool.save_artifact', side_effect=AssertionError('No invalid writes')):
                rejected = create_artifact(None, root, 'repair-test', 'pomodoro', {'focus_minutes': 15, 'sides': 6})
                self.assertEqual(rejected['status'], 'needs_input')
                self.assertEqual(rejected['data']['code'], 'artifact_input')
                self.assertEqual(rejected['data']['repair']['unexpected_fields'], ['$.options.sides'])
            result = create_artifact(None, root, 'repair-test', 'pomodoro', {'focus_minutes': 15})
            self.assertEqual(result['status'], 'ok', result)
            data = json.loads(Path(result['data']['artifact']['data_path']).read_text())
            self.assertEqual(data['focusMinutes'], 15)
