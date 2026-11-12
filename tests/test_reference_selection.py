import unittest
from unittest.mock import Mock, patch

from folder_memory.knowledge import ReferenceLibrary


class ReferenceSelectionTests(unittest.TestCase):

    def test_http_codes_do_not_match_parts_of_ip_addresses_or_versions(self):
        for question in ('Как подключиться по SSH к 203.0.113.10?',
                         'Объясни адрес 10.0.0.200', 'Что нового в версии 2.503.0?'):
            with self.subTest(question=question):
                self.assertNotIn('topic:http.status', [item['id'] for item in ReferenceLibrary.topic_search(question)])
        for question in ('Что означает HTTP 503?', 'Почему сервер отвечает 404.', 'Что значит 2xx?'):
            with self.subTest(question=question):
                self.assertIn('topic:http.status', [item['id'] for item in ReferenceLibrary.topic_search(question)])


    def test_developer_catalog_and_exact_identifiers(self):
        catalog = ReferenceLibrary.developer_catalog()
        self.assertEqual(len(catalog), 20)
        for key, technology in catalog.items():
            topics = technology['topics']
            self.assertGreaterEqual(len(topics), 5)
            self.assertTrue(all(item['id'].startswith(key + '.') and item['sources'] for item in topics))
        for question, topic in (
            ('Чем отличаются wanted и latest в npm outdated?', 'npm.outdated_wanted_latest'),
            ('Как работает composer why?', 'composer.why'),
            ('Зачем нужен JSON_THROW_ON_ERROR в PHP?', 'php.json_throw'),
            ('Как работает import type в TypeScript?', 'typescript.type_only_import'),
            ('Как работает import в TypeScript?', None),
            ('Проверь npm outdated в этом проекте.', None),
        ):
            with self.subTest(question=question):
                records = ReferenceLibrary.topic_search(question)
                self.assertEqual([item['id'] for item in records], ['topic:' + topic] if topic else [])

    def test_qualified_commands_and_case_sensitive_flags(self):
        library = ReferenceLibrary()
        cases = [
            ('Что показывает docker compose ps --all?', 'docker.compose.ps'),
            ('What does docker-compose logs --tail 20 api print?', 'docker.compose.logs'),
            ('What does docker compose --profile dev logs worker display?', 'docker.compose.logs'),
            ('What is docker container logs?', 'docker.logs'),
            ('What does ssh -G print?', 'ssh.effective_config'),
            ('What does ssh -g permit?', 'ssh'),
            ('tldr:ru:common:lsof', 'tldr:ru:common:lsof'),
        ]
        for question, expected in cases:
            with self.subTest(question=question):
                self.assertEqual([item['id'] for item in library.search(question)], [expected])


    def test_platform_specific_cards_and_wrong_os(self):
        library = ReferenceLibrary()
        self.assertEqual(library.search('Объясни sed -i на macOS')[0]['id'], 'sed:osx')
        self.assertEqual(library.search('Объясни GNU sed -i на macOS')[0]['id'], 'sed:linux')
        self.assertEqual(library.search('Как использовать команду systemctl на macOS?'), [])
        self.assertEqual(library.search('What does log show do on Ubuntu?'), [])

    def test_prose_paths_and_live_actions_do_not_admit_static_cli_cards(self):
        library = ReferenceLibrary()
        for question in (
            'Does JSON support comments?',
            'Зачем удалённый доступ в онлайн-банке?',
            'Why does my head hurt?',
            'What is cat food?',
            'How does cat behave at home?',
            'Where can I find a replacement?',
            'Что на https://jq/guide',
            'Что за файл в /tmp/docker-compose/logs?',
            'Проверь, кто сейчас слушает порт 8080 через lsof.',
            'Restart the api container now with docker compose restart api.',
            'Посчитай строки в моём отчёте report.csv.',
            'Как find ищет файлы в этом проекте? Выполни проверку.',
        ):
            with self.subTest(question=question):
                self.assertEqual(library.search(question), [])

    def test_semantic_topic_requires_grounding_and_can_abstain(self):
        retrieval = Mock()
        retrieval.search.return_value = [
            {'id': 'topic:react.effects', 'score': .98},
            {'id': 'topic:node.modules', 'score': .81},
        ]
        library = ReferenceLibrary(retrieval)
        self.assertEqual(library.semantic_topics('Как разобраться с форматом ESM?'), [])
        retrieval.search.return_value.reverse()
        retrieval.search.return_value[1]['score'] = .64
        self.assertEqual(library.semantic_topics('Как разобраться с форматом ESM?')[0]['id'], 'topic:node.modules')
        self.assertEqual(library.semantic_topics('В чём разница форматов изображений?'), [])
        self.assertEqual(retrieval.search.call_count, 2)
        retrieval.search.return_value = [{'id': 'topic:node.modules', 'score': .69}]
        self.assertEqual(library.semantic_topics('Как разобраться с форматом ESM?'), [])

    def test_composed_answer_uses_exact_source_cards_in_mention_order(self):
        retrieval = Mock()
        library = ReferenceLibrary(retrieval)
        catalog = {entry['id']: entry for entry in library.developer_topics()}
        cases = [
            ('Как сочетать в React useActionState и useOptimistic?',
             ['react.action_state', 'react.optimistic_ui']),
            ('Сравни `useOptimistic`, `useTransition` и `useActionState` в React',
             ['react.optimistic_ui', 'react.transitions_pending', 'react.action_state']),
        ]
        for question, expected in cases:
            with self.subTest(question=question):
                cards = library.answer_sources(question, guidance=True)
                self.assertEqual([card['id'] for card in cards], ['topic:' + key for key in expected])
                for card, key in zip(cards, expected, strict=True):
                    self.assertEqual(card['text'], catalog[key]['text'])
                    self.assertEqual(card['sources'], catalog[key]['sources'])
                    self.assertEqual(card['source'], catalog[key]['sources'][0])
        retrieval.search.assert_not_called()

    def test_composed_sources_preserve_single_card_and_reject_ambiguous_scope(self):
        library = ReferenceLibrary()
        single = 'Как работает useOptimistic в React?'
        self.assertEqual(library.answer_sources(single), library.topic_search(single))
        cli = 'Что показывает docker compose ps --all?'
        self.assertEqual(library.answer_sources(cli, guidance=True), library.guidance(cli))
        for question in (
            'Как сочетать useActionState и useOptimistic?',
            'Как сочетать в React useActionState и useOptimistic? Прочитай README.md.',
            'Сравни в React useActionState, useOptimistic, useTransition и useId?',
        ):
            with self.subTest(question=question):
                self.assertEqual(library.answer_sources(question), [])
        catalog = library.developer_topics()
        item = next(entry for entry in catalog if entry['id'] == 'react.action_state')
        duplicate = {**item, 'id': 'react.same_identifier'}
        with patch.object(ReferenceLibrary, 'developer_topics', return_value=[*catalog, duplicate]):
            self.assertEqual(library.answer_sources('Как сочетать в React useActionState и useOptimistic?'), [])
