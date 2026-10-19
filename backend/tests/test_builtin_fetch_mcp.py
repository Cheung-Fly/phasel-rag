import unittest
from unittest.mock import Mock, patch

from app import main
from app.auth_store import AuthenticatedUser


class BuiltinFetchMcpTests(unittest.TestCase):
    def test_registration_uses_fixed_isolated_command(self):
        store = Mock()
        store.save.return_value = {"name": "Fetch网页内容抓取", "transport": "stdio"}
        user = AuthenticatedUser(user_id="owner", username="owner")

        with patch.object(main, "get_mcp_store", return_value=store):
            result = main.add_builtin_fetch_mcp(user)

        self.assertEqual(result["server"]["name"], "Fetch网页内容抓取")
        store.save.assert_called_once_with(
            "owner",
            "Fetch网页内容抓取",
            {
                "transport": "streamable_http",
                "enabled": True,
                "url": "http://mcp-fetch:3000/mcp",
            },
        )


if __name__ == "__main__":
    unittest.main()
