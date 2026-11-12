"""A native app update must not remove an executable macOS still attributes work to."""
import plistlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from integration.macos import build as macos_build


class MacOSBuildTests(unittest.TestCase):
    def test_running_bundle_is_preserved_and_other_app_does_not_block_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / 'Handy Menu.app'
            python = Path(sys.executable)
            state = root / 'state'
            success = subprocess.CompletedProcess([], 0, stdout='')
            with mock.patch.object(macos_build.sys, 'platform', 'darwin'), mock.patch.object(macos_build.subprocess, 'run', return_value=success) as run:
                macos_build.build(destination, python, state)
                plist = destination / 'Contents/Info.plist'
                original = plist.read_bytes()
                sentinel = destination / 'keep-existing-app'
                sentinel.write_text('existing bundle')
                run.return_value = subprocess.CompletedProcess([], 0, stdout=str(destination / 'Contents/MacOS/HandyMenu') + '\n')
                with self.assertRaisesRegex(ValueError, 'Закрой приложение Handy'):
                    macos_build.build(destination, python, state)
                self.assertEqual(plist.read_bytes(), original)
                self.assertTrue(sentinel.exists())
                run.return_value = subprocess.CompletedProcess([], 0, stdout=str(root / 'Other.app/Contents/MacOS/HandyMenu') + '\n')
                macos_build.build(destination, python, state)
                self.assertFalse(sentinel.exists())
                self.assertIn('NSDocumentsFolderUsageDescription', plistlib.loads(plist.read_bytes()))


if __name__ == '__main__':
    unittest.main()
