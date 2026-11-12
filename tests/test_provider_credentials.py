"""A provider protects its key even before any model is linked to it."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from folder_memory.filesystem_tools import read_file
from folder_memory.model_registry import ModelRegistry


class ProviderCredentialsTests(unittest.TestCase):
    def test_external_extensionless_provider_key_is_never_a_file_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {'HANDY_CONFIG_HOME': str(root / 'config')}):
                registry = ModelRegistry(root / 'state')
                registry.load()
                key = root / 'provider-access'
                key.write_text('PRIVATE_PROVIDER_FIXTURE')
                key.chmod(0o600)
                registry.save_provider('provider', {'name': 'API', 'base_url': 'https://provider.example/v1',
                                                   'api_key_file': str(key)})
                self.assertFalse(any(p.get('provider_id') == 'provider' for p in registry.profiles().values()))
                with self.assertRaisesRegex(ValueError, 'секрет'):
                    read_file(root, path=str(key))


if __name__ == '__main__':
    unittest.main()
