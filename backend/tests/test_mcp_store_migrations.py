import json
import unittest
from unittest.mock import MagicMock, Mock

from cryptography.fernet import Fernet

from app.mcp_store import McpStore


class BuiltinFetchMigrationTests(unittest.TestCase):
    def test_migrates_only_the_known_legacy_fetch_command(self):
        store = McpStore.__new__(McpStore)
        store._fernet = Fernet(Fernet.generate_key())
        legacy = {
            "transport": "stdio",
            "enabled": True,
            "command": "/opt/mcp-fetch-venv/bin/mcp-server-fetch",
            "args": [],
        }
        other = {
            "transport": "stdio",
            "enabled": True,
            "command": "custom-fetch",
            "args": [],
        }
        rows = [
            {"server_id": "legacy", "config_ciphertext": store._fernet.encrypt(json.dumps(legacy).encode()).decode()},
            {"server_id": "custom", "config_ciphertext": store._fernet.encrypt(json.dumps(other).encode()).decode()},
        ]
        connection = MagicMock()
        connection.execute.side_effect = [Mock(fetchall=Mock(return_value=rows)), Mock()]
        pool = MagicMock()
        pool.connection.return_value.__enter__.return_value = connection
        store._pool = pool

        migrated = store.migrate_builtin_fetch_to_http(
            "Fetch网页内容抓取",
            "/opt/mcp-fetch-venv/bin/mcp-server-fetch",
            "http://mcp-fetch:3000/mcp",
        )

        self.assertEqual(migrated, 1)
        update_sql, update_params = connection.execute.call_args_list[1].args
        self.assertIn("UPDATE mcp_servers", update_sql)
        config = json.loads(store._fernet.decrypt(update_params[0].encode()))
        self.assertEqual(config["transport"], "streamable_http")
        self.assertEqual(config["url"], "http://mcp-fetch:3000/mcp")
        self.assertNotIn("command", config)
        self.assertEqual(update_params[1], "legacy")


if __name__ == "__main__":
    unittest.main()
