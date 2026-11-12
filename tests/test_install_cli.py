"""Reinstalling the CLI must update the settings app, without replacing a live app."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import install_cli


class InstallCLITests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (self.root/'handy').write_text('#!/bin/sh\n')
        interpreter=self.root/'.venv/bin/python'
        interpreter.parent.mkdir(parents=True);interpreter.touch()
        self.bundle=self.root/'.runtime/Handy Menu.app'
        self.bundle.mkdir(parents=True)
        self.version=self.bundle/'version';self.version.write_text('old app')
        self.bin=self.root/'bin'
        self.enterContext(patch.object(install_cli,'PROJECT',self.root))
        self.enterContext(patch.object(install_cli.sys,'platform','darwin'))
        self.enterContext(patch.object(install_cli.shutil,'which',side_effect=lambda name:'/tools/'+name))
        self.enterContext(patch('folder_memory.pi_localization.localized_cli'))

    def test_reinstall_updates_existing_app_before_exposing_command(self):
        def run(args,**kwargs):
            if str(self.root/'integration/macos/build.py') in args:
                self.assertFalse((self.bin/'handy').exists())
                self.version.write_text('new app')
            return subprocess.CompletedProcess(args,0)
        with patch('integration.macos.build.require_closed'), patch.object(install_cli.subprocess,'run',side_effect=run):
            command=install_cli.install(self.bin)
        self.assertEqual(self.version.read_text(),'new app')
        self.assertEqual(command.resolve(),self.root/'handy')

    def test_linux_installs_command_without_macos_build(self):
        with patch.object(install_cli.sys, 'platform', 'linux'), \
                patch.object(install_cli.subprocess, 'run') as run, \
                patch('integration.macos.build.require_closed') as require_closed:
            command = install_cli.install(self.bin)
        self.assertEqual(command.resolve(), self.root / 'handy')
        require_closed.assert_not_called()
        self.assertFalse(any(str(self.root / 'integration/macos/build.py') in call.args[0]
                             for call in run.call_args_list))

    def test_live_app_blocks_install_before_dependencies_or_command_change(self):
        with patch('integration.macos.build.require_closed',side_effect=ValueError('Закрой приложение Handy')), \
                patch.object(install_cli.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'Закрой приложение Handy'):
                install_cli.install(self.bin)
        run.assert_not_called()
        self.assertEqual(self.version.read_text(),'old app')
        self.assertFalse((self.bin/'handy').exists())


if __name__=='__main__':unittest.main()
