"""Activation failures never silently send a new-model chat to the old backend."""

import copy
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from folder_memory import model_activation as activation
from folder_memory.model_backend import model_config_digest, read_model_config
from folder_memory.runtime import write_json


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = self.directory / 'runtime'
        self.state.mkdir()
        self.enterContext(patch.dict(os.environ, {'HANDY_CONFIG_HOME': str(self.directory / 'global')}))
        self.old = {'name': 'old', 'enabled': False, 'context': 4096}
        self.new = {'name': 'new', 'enabled': False, 'context': 8192}
        write_json(self.state / 'neural.json', self.old)
        self.active = copy.deepcopy(self.old)
        self.events = []
        self.stop_error = self.start_error = None
        self.enterContext(patch.object(activation, 'engine_settings', return_value={'launcher': 'managed'}))
        self.enterContext(patch.object(activation, 'engine_status', side_effect=self.status))
        self.enterContext(patch.object(activation, 'stop_engine', side_effect=self.stop))
        self.enterContext(patch.object(activation, 'start_engine', side_effect=self.start))

    def status(self, *_):
        return {'status': 'running' if self.active is not None else 'stopped',
                'model_config': model_config_digest(self.active) if self.active is not None else None}

    def stop(self, *_):
        self.events.append(('stop', read_model_config(self.state)['name']))
        if self.stop_error:
            raise self.stop_error
        self.active = None

    def start(self, *_):
        config = read_model_config(self.state)
        self.events.append(('start', config['name']))
        if self.start_error and config == self.new:
            raise self.start_error
        self.active = config
        return self.status()

    def test_failed_stop_keeps_old_disk_config_and_next_attempt_retries_activation(self):
        self.stop_error = OSError('stop fixture failed')
        with self.assertRaises(OSError):
            activation.activate_models(self.state, self.new)
        self.assertEqual(read_model_config(self.state), self.old)
        self.assertEqual(self.active, self.old)
        self.stop_error = None
        activation.activate_models(self.state, self.new)
        self.assertEqual(self.active, self.new)
        self.assertEqual(self.events, [('stop', 'old'), ('stop', 'old'), ('start', 'new')])

    def test_old_activation_desync_is_repaired_even_when_disk_already_matches_desired(self):
        write_json(self.state / 'neural.json', self.new)
        activation.activate_models(self.state, self.new)
        self.assertEqual(self.active, self.new)
        self.assertEqual(self.events, [('stop', 'new'), ('start', 'new')])

    def test_failed_start_or_interrupt_restores_old_service_and_still_reports_failure(self):
        for error in (OSError('start fixture failed'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.active = copy.deepcopy(self.old)
                write_json(self.state / 'neural.json', self.old)
                self.start_error = error
                with self.assertRaises(type(error)):
                    activation.activate_models(self.state, self.new)
                self.assertEqual(read_model_config(self.state), self.old)
                self.assertEqual(self.active, self.old)

    def test_settings_do_not_start_stopped_service_but_restart_a_running_one(self):
        self.active = None
        activation.activate_models(self.state, self.new, start=False)
        self.assertEqual(self.events, [])
        self.assertEqual(read_model_config(self.state), self.new)
        self.assertIsNone(self.active)
        self.active = copy.deepcopy(self.old)
        activation.activate_models(self.state, self.new, start=False)
        self.assertEqual(self.active, self.new)

    def test_concurrent_activations_leave_disk_and_actual_model_consistent(self):
        errors = []
        def apply(config):
            try:
                activation.activate_models(self.state, config)
            except BaseException as error:
                errors.append(error)
        workers = [threading.Thread(target=apply, args=(config,)) for config in (self.new, self.old)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(read_model_config(self.state), self.active)

    def test_force_restarts_same_profile_for_updated_api_environment(self):
        activation.activate_models(self.state, self.old, force=True)
        self.assertEqual(self.events, [('stop', 'old'), ('start', 'old')])

    def test_unchanged_running_configuration_does_not_restart(self):
        activation.activate_models(self.state, self.old)
        self.assertEqual(self.events, [('start', 'old')])


if __name__ == '__main__':
    unittest.main()
