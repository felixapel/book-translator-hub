"""The checked-in managed hub input stays usable without Docker or secrets."""

import unittest
import tempfile
from pathlib import Path

from btctl_core import ReleaseIdentity, parse_env_text
from btctl_hub import HubInstallConfig


REPOSITORY = Path(__file__).resolve().parents[2]
IDENTITY = ReleaseIdentity(
    version="2.2.0",
    sha="0123456789abcdef0123456789abcdef01234567",
)


class ManagedHubExampleTests(unittest.TestCase):
    def test_example_parses_as_a_secret_free_unraid_hub_input(self):
        values = parse_env_text(
            (REPOSITORY / ".env.hub.example").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values.update({
                "BT_STATE_DIR": str(root / "state"),
                "BT_DATA_DIR": str(root / "data"),
                "BT_BACKUP_DIR": str(root / "backup"),
            })
            config = HubInstallConfig.from_mapping(values, IDENTITY)

        self.assertEqual(config.install_profile, "unraid")
        self.assertEqual(config.install_name, "book-translator-hub")
        self.assertEqual(set(config.reader_containers), {"cwa", "kavita"})
        versions = {reader.name: reader.version for reader in config.runtime.readers}
        self.assertEqual(versions["kavita"], "0.9.0.2")
        self.assertEqual(values["LLM_PROVIDER"], "local")
        self.assertEqual(values["LLM_API_KEY"], "")
        self.assertNotIn("BT_API_TOKEN", values)
        self.assertNotIn("BT_AUTH_MODE", values)
        self.assertNotIn("BT_ALLOW_INSECURE_AUTH", values)
