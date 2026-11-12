"""Real files exercise native filesystem tools without a model or shell."""

from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from folder_memory.file_access import SystemPathError, TextSourceChanged, read_text
from folder_memory.filesystem_tools import list_directory, read_file
from folder_memory.tool_catalog import TOOL_SCHEMAS


def flatten(entries):
    return [row for entry in entries for row in (entry, *flatten(entry.get('children', [])))]


class FilesystemToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.config = self.base / 'handy-config'
        environment = patch.dict(os.environ, {'HANDY_CONFIG_HOME': str(self.config)})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = self.base / 'project with spaces папка'
        self.root.mkdir()
        (self.root / 'README.md').write_text('Project fixture.\n', encoding='utf-8')
        (self.root / 'src').mkdir()
        (self.root / 'src/main.py').write_text('def main():\n    return 42\n', encoding='utf-8')

    def test_default_tree_matches_actual_parentage_and_exact_workspace_path(self):
        data = list_directory(self.root)['data']
        self.assertEqual(data['root'], str(self.root))
        self.assertEqual(data['path'], str(self.root))
        self.assertEqual(data['depth'], 2)
        self.assertEqual(data['returned_entries'], 3)
        self.assertFalse(data['partial'])
        self.assertFalse(data['truncated'])
        self.assertEqual([row['name'] for row in data['entries']], ['README.md', 'src'])
        self.assertEqual(data['entries'][1]['children'][0]['path'], 'src/main.py')
        self.assertEqual(data['tree'], str(self.root) + '/\n├── README.md\n└── src/\n    └── main.py')

    def test_hidden_filter_is_complete_for_the_selected_view(self):
        (self.root / '.handy').mkdir()
        (self.root / '.handy/notes.txt').write_text('internal fixture')
        data = list_directory(self.root)['data']
        self.assertTrue(data['hidden_filtered'])
        self.assertFalse(data['partial'])
        self.assertFalse(data['truncated'])
        self.assertEqual(data['returned_entries'], 3)
        self.assertNotIn('.handy', data['tree'])

    def test_service_directories_are_stubs_but_explicit_targets_are_allowed(self):
        for name in ('.git', '.handy', 'node_modules'):
            (self.root / name / 'inside').mkdir(parents=True)
            (self.root / name / 'inside/file.txt').write_text('fixture')
        data = list_directory(self.root, depth=4, include_hidden=True)['data']
        for name in ('.git', '.handy', 'node_modules'):
            row = next(row for row in data['entries'] if row['name'] == name)
            self.assertEqual(row['type'], 'directory')
            self.assertEqual(row['skip_reason'], 'service_directory')
            self.assertEqual(row['children'], [])
        self.assertFalse(data['partial'])
        self.assertFalse(data['truncated'])
        readme = next(line for line in data['tree'].splitlines() if 'README.md' in line)
        self.assertTrue(readme.startswith(('├── ', '└── ')))
        explicit = list_directory(self.root, path='node_modules', depth=3)['data']
        self.assertEqual(explicit['path'], str(self.root / 'node_modules'))
        self.assertIn('node_modules/inside/file.txt', [row['path'] for row in flatten(explicit['entries'])])

    def test_symlink_nodes_are_complete_leaves_without_recursion_or_cycles(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'outside-marker.txt').write_text('fixture')
        (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        (self.root / 'cycle').symlink_to(self.root, target_is_directory=True)
        data = list_directory(self.root, depth=4)['data']
        for name in ('linked', 'cycle'):
            row = next(row for row in data['entries'] if row['name'] == name)
            self.assertEqual(row['type'], 'symlink')
            self.assertFalse(row['followed'])
            self.assertNotIn('children', row)
        self.assertFalse(data['partial'])
        self.assertFalse(data['truncated'])
        self.assertFalse(data['symlinks_followed'])
        self.assertNotIn('outside-marker', data['tree'])
        explicit = list_directory(self.root, path='linked')['data']
        self.assertEqual(explicit['path'], str(outside))
        self.assertEqual(explicit['entries'][0]['path'], '../outside/outside-marker.txt')

    def test_global_entry_and_depth_limits_are_explicit(self):
        for index in range(3):
            directory = self.root / f'folder-{index}'
            directory.mkdir()
            for child in range(10):
                (directory / f'file-{child}.txt').write_text('fixture')
        data = list_directory(self.root, depth=4, max_entries=8)['data']
        self.assertEqual(data['returned_entries'], 8)
        self.assertEqual(len(flatten(data['entries'])), 8)
        self.assertTrue(data['partial'])
        self.assertTrue(data['truncated'])
        self.assertIn('entry_limit', data['omitted_reasons'])
        shallow = list_directory(self.root, depth=1)['data']
        self.assertTrue(shallow['partial'])
        self.assertFalse(shallow['truncated'])
        self.assertIn('depth_limit', shallow['omitted_reasons'])

    def test_large_directory_output_is_bounded_even_with_long_names(self):
        for index in range(100):
            (self.root / ('x' * 170 + f'-{index}.txt')).write_text('fixture')
        reply = list_directory(self.root, depth=4, max_entries=200)
        self.assertTrue(reply['data']['truncated'])
        self.assertIn('output_limit', reply['data']['omitted_reasons'])
        self.assertLessEqual(len(json.dumps(reply, ensure_ascii=False)), 16000)

    def test_tree_escapes_caption_controls_and_box_characters_but_preserves_real_paths(self):
        name = 'fake\n├── child\t\u202efile.txt'
        (self.root / name).write_text('fixture')
        data = list_directory(self.root)['data']
        row = next(row for row in data['entries'] if row['name'] == name)
        self.assertEqual(row['path'], name)
        self.assertIn(r'fake\u000a\u251c\u2500\u2500 child\u0009\u202efile.txt', data['tree'])
        self.assertEqual(len(data['tree'].splitlines()), 5)

    def test_stat_is_performed_while_the_scandir_iterator_is_open(self):
        active = False
        observed = (self.root / 'README.md').lstat()

        def entry_stat(*, follow_symlinks):
            self.assertTrue(active, 'DirEntry stat outlived the iterator owning its descriptor')
            self.assertFalse(follow_symlinks)
            return observed

        @contextmanager
        def entries(descriptor):
            nonlocal active
            self.assertTrue(os.fstat(descriptor))
            active = True
            try:
                yield iter([SimpleNamespace(name='README.md', stat=entry_stat)])
            finally:
                active = False

        with patch('folder_memory.filesystem_tools.os.scandir', entries):
            data = list_directory(self.root)['data']
        self.assertEqual(data['entries'][0]['type'], 'file')

    def test_home_system_directory_is_a_stub_and_never_opened_for_recursion(self):
        home = self.base / 'home'
        (home / 'Library/Caches').mkdir(parents=True)
        (home / 'Library/Caches/private.txt').write_text('system fixture')
        (home / 'Documents').mkdir()
        (home / 'Documents/notes.txt').write_text('user fixture')
        real_open = os.open
        opened = []

        def open_checked(path, flags, *args, **kwargs):
            if str(path) == 'Library' and kwargs.get('dir_fd') is not None:
                self.fail('Recursive listing must not open the home system directory')
            descriptor = real_open(path, flags, *args, **kwargs)
            opened.append(descriptor)
            return descriptor

        with patch('folder_memory.file_access.Path.home', return_value=home), \
                patch('folder_memory.filesystem_tools.os.open', open_checked):
            data = list_directory(home, depth=4)['data']
            library = next(row for row in data['entries'] if row['name'] == 'Library')
            self.assertEqual(library['skip_reason'], 'system_directory')
            self.assertFalse(library['expanded'])
            self.assertEqual(library['children'], [])
            self.assertFalse(data['partial'])
            self.assertIn('Documents/notes.txt', [row['path'] for row in flatten(data['entries'])])
            self.assertNotIn('private.txt', data['tree'])
            self.assertIn('Library/ [системная папка]', data['tree'])
            with self.assertRaises(SystemPathError):
                list_directory(home, path='Library')
        for descriptor in set(opened):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_replaced_child_directory_is_not_followed_and_all_descriptors_close(self):
        for replacement in ('symlink', 'different_inode'):
            with self.subTest(replacement=replacement):
                root = self.base / replacement
                (root / 'child').mkdir(parents=True)
                (root / 'child/original.txt').write_text('original fixture')
                outside = self.base / ('outside-' + replacement)
                outside.mkdir()
                (outside / 'unexpected.txt').write_text('unexpected fixture')
                real_open = os.open
                opened, swapped = [], False

                def open_checked(path, flags, *args, **kwargs):
                    nonlocal swapped
                    if str(path) == 'child' and kwargs.get('dir_fd') is not None and not swapped:
                        swapped = True
                        (root / 'child').rename(root / 'original')
                        if replacement == 'symlink':
                            (root / 'child').symlink_to(outside, target_is_directory=True)
                        else:
                            (root / 'child').mkdir()
                            (root / 'child/unexpected.txt').write_text('unexpected fixture')
                    descriptor = real_open(path, flags, *args, **kwargs)
                    opened.append(descriptor)
                    return descriptor

                with patch('folder_memory.filesystem_tools.os.open', open_checked):
                    data = list_directory(root, depth=4)['data']
                self.assertTrue(swapped)
                self.assertEqual(data['entries'][0]['skip_reason'], 'unavailable')
                self.assertFalse(data['entries'][0]['expanded'])
                self.assertEqual(data['entries'][0]['children'], [])
                self.assertTrue(data['partial'])
                self.assertNotIn('unexpected.txt', data['tree'])
                for descriptor in set(opened):
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)

    def test_large_utf8_file_uses_shared_revision_windows_and_finishes_at_eof(self):
        source = self.root / 'large text.txt'
        body = '\ufeffПривет\n' + 'я' * 12000 + '\nFinal\n'
        source.write_text(body, encoding='utf-8')
        args = {'path': source.name, 'max_chars': 4999}
        parts = []
        for _ in range(10):
            data = read_file(self.root, **args)['data']
            self.assertEqual(data['root'], str(self.root))
            self.assertEqual(data['path'], str(source))
            self.assertEqual(data['unit'], 'bytes')
            self.assertLessEqual(len(data['text']), 4999)
            parts.append(data['text'])
            if data['eof']:
                self.assertIsNone(data['next_offset'])
                self.assertFalse(data['truncated'])
                break
            self.assertGreater(data['next_offset'], args.get('offset', 0))
            args.update(offset=data['next_offset'], revision=data['revision'])
        else:
            self.fail('Read continuation did not reach EOF')
        self.assertEqual(''.join(parts), body.lstrip('\ufeff'))

    def test_shared_reader_handles_files_larger_than_the_full_read_limit(self):
        source = self.root / 'big.txt'
        source.write_text('x' * (256 * 1024 + 100))
        data = read_file(self.root, path=source.name)['data']
        self.assertEqual(len(data['text']), 5000)
        self.assertTrue(data['truncated'])
        self.assertFalse(data['eof'])
        self.assertEqual(data['total'], source.stat().st_size)

    def test_changed_file_revision_is_not_silently_read_as_the_same_source(self):
        source = self.root / 'change.txt'
        source.write_text('original contents')
        first = read_file(self.root, path=source.name, max_chars=5)['data']
        source.write_text('changed and longer contents')
        with self.assertRaises(TextSourceChanged):
            read_file(self.root, path=source.name, offset=first['next_offset'], revision=first['revision'])

    def test_explicit_outside_path_is_allowed_without_changing_the_workspace(self):
        source = self.base / 'outside data.txt'
        source.write_text('outside fixture')
        for path in (str(source), '../outside data.txt'):
            with self.subTest(path=path):
                data = read_file(self.root, path=path)['data']
                self.assertEqual(data['root'], str(self.root))
                self.assertEqual(data['path'], str(source))
                self.assertEqual(data['text'], 'outside fixture')

    def test_secret_system_and_binary_guards_are_preserved(self):
        (self.root / '.env').write_text('DUMMY_SECRET=fixture')
        (self.root / '.env.example').write_text('DUMMY_KEY=')
        (self.root / '.ssh').mkdir()
        (self.root / '.ssh/config').write_text('private fixture')
        (self.root / 'alias').symlink_to(self.root / '.env')
        for path in ('.env', '.ssh/config', 'alias'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                read_file(self.root, path=path)
        self.assertEqual(read_file(self.root, path='.env.example')['data']['text'], 'DUMMY_KEY=')
        with self.assertRaises(SystemPathError):
            read_file(self.root, path='/etc/hosts')
        with self.assertRaises(SystemPathError):
            list_directory(self.root, path='/')
        (self.root / 'binary.bin').write_bytes(b'fixture\x00binary')
        with self.assertRaisesRegex(ValueError, 'двоичный'):
            read_file(self.root, path='binary.bin')

    def test_managed_credentials_are_private_with_default_and_overridden_config_roots(self):
        home = self.base / 'home'
        locations = (
            ({'HOME': str(home)}, home / '.config' / 'handy'),
            ({'HANDY_CONFIG_HOME': str(self.base / 'exact-config')}, self.base / 'exact-config'),
            ({'XDG_CONFIG_HOME': str(self.base / 'xdg')}, self.base / 'xdg' / 'handy'),
        )
        for environment, config in locations:
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                    patch('folder_memory.file_access.Path.home', return_value=home):
                credentials = config / 'credentials'
                credentials.mkdir(parents=True)
                key = credentials / 'legacy-provider'
                key.write_text('fixture-only-provider-secret')
                alias = config / 'legacy-alias'
                alias.symlink_to(key)
                for source in (key, alias):
                    with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'секрет'):
                        read_file(self.root, path=str(source))
                with self.assertRaisesRegex(ValueError, 'секрет'):
                    read_text(credentials, key.name)
                with self.assertRaises(ValueError):
                    list_directory(self.root, path=str(credentials))
                data = list_directory(self.root, path=str(config), depth=4)['data']
                row = next(row for row in data['entries'] if row['name'] == 'credentials')
                self.assertEqual(row['skip_reason'], 'private')
                self.assertEqual(row['children'], [])

    def test_configured_external_key_is_private_and_metadata_changes_are_observed(self):
        self.config.mkdir()
        profile = {'kind': 'chat', 'backend': 'openai', 'model_id': 'fixture',
                   'base_url': 'http://127.0.0.1:9999/v1'}
        metadata = self.config / 'models.json'
        for name in ('provider-access', 'replacement-provider-access'):
            key = self.base / name
            key.write_text('fixture-only-external-secret')
            key.chmod(0o600)
            document = json.dumps({'version': 1, 'selected_chat': 'provider', 'selected_embedding': None,
                                   'profiles': {'provider': {**profile, 'api_key_file': str(key)}}})
            metadata.write_text(document)
            metadata.chmod(0o600)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'секрет'):
                read_file(self.root, path=str(key))
            with self.assertRaisesRegex(ValueError, 'секрет'):
                read_text(self.base, key.name)
            self.assertEqual(metadata.read_text(), document)
        alias = self.root / 'key-alias'
        alias.symlink_to(key)
        with self.assertRaisesRegex(ValueError, 'секрет'):
            read_file(self.root, path=alias.name)

    def test_ordinary_project_config_and_credentials_directories_remain_readable(self):
        for relative in ('credentials/provider', '.config/service/settings.txt'):
            source = self.root / relative
            source.parent.mkdir(parents=True)
            source.write_text('ordinary project fixture')
            self.assertEqual(read_file(self.root, path=relative)['data']['text'], 'ordinary project fixture')
        self.assertFalse(self.config.exists(), 'Reading project files must not initialize model configuration')

    def test_configured_key_parent_alias_stays_private_after_its_target_changes(self):
        self.config.mkdir()
        alias = self.base / 'provider-directory'
        metadata = self.config / 'models.json'
        metadata.write_text(json.dumps({'profiles': {'provider': {'api_key_file': str(alias / 'access')}}}))
        for name in ('first-provider', 'replacement-provider'):
            directory = self.base / name
            directory.mkdir()
            key = directory / 'access'
            key.write_text('fixture-only-provider-secret')
            alias.unlink(missing_ok=True)
            alias.symlink_to(directory, target_is_directory=True)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'секрет'):
                read_file(self.root, path=str(key))



    def test_invalid_arguments_do_not_escape_as_type_errors_or_unbounded_reads(self):
        for args in ({'depth': True}, {'depth': 0}, {'depth': 5}, {'max_entries': 0},
                     {'max_entries': 201}, {'include_hidden': 'yes'}, {'path': ''}, {'path': None}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                list_directory(self.root, **args)
        for args in ({'offset': 1}, {'max_lines': 81}, {'max_lines': False}, {'max_chars': 5001},
                     {'max_chars': 1}, {'revision': 42}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                read_file(self.root, path='README.md', **args)


if __name__ == '__main__':
    unittest.main()
