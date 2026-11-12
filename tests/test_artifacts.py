from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from folder_memory.artifacts import ArtifactPage, save_artifact


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='funny-artifact-')
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.root = self.base / 'project'
        self.root.mkdir()


    def test_export_serializes_user_data_as_inert_json(self):
        payload = '</script><script>globalThis.bad = true;</script>'
        result = save_artifact(ArtifactPage('list', {'title': payload, 'items': [payload]}), self.root)
        html = Path(result['path']).read_text()
        self.assertNotIn(payload, html)
        self.assertIn('Content-Security-Policy', html)
        self.assertIn('funny-artifact-data', html)
        self.assertEqual(json.loads(Path(result['data_path']).read_text())['items'], [payload])

    def test_uses_worktree_root_and_ignores_generated_files(self):
        if not shutil.which('git'):
            self.skipTest('Git is required')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        sub = self.root / 'nested'
        sub.mkdir()
        legacy = self.root / '.funny' / 'artifacts'
        legacy.mkdir(parents=True)
        (legacy / '.gitignore').write_text('*\n')
        original = legacy / 'index.html'
        original.write_text('Existing artifact')
        info = save_artifact(ArtifactPage('clock', {'title': 'Test'}), sub)
        self.assertTrue(Path(info['path']).is_relative_to(self.root / '.handy' / 'artifacts'))
        self.assertFalse((sub / '.handy').exists())
        self.assertEqual(original.read_text(), 'Existing artifact')
        status = subprocess.check_output(['git', '-C', str(self.root), 'status', '--porcelain'], text=True)
        self.assertEqual(status, '')

    def test_concurrent_exports_and_symlink_boundary(self):
        page = ArtifactPage('dice', {'title': 'Test', 'count': 1, 'sides': 6})
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = [result['path'] for result in pool.map(lambda _: save_artifact(page, self.root), range(8))]
        self.assertEqual(len(set(paths)), 8)
        self.assertTrue(all(Path(path).is_file() for path in paths))
        another = self.base / 'other'
        another.mkdir()
        (another / '.handy').symlink_to(self.root)
        with self.assertRaises(OSError):
            save_artifact(page, another)
        self.assertFalse((self.root / 'artifacts').exists())
