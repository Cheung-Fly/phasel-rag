import importlib
import os
import unittest
from unittest.mock import patch

from app import site_store


class PluginSiteTests(unittest.TestCase):
    def reload_with(self, value):
        env = {} if value is None else {"ENABLED_PLUGINS": value}
        with patch.dict(os.environ, env, clear=value is None):
            if value is None:
                os.environ.pop("ENABLED_PLUGINS", None)
            return site_store._load_plugin_sites()

    def test_campus_enabled_by_default(self):
        sites = self.reload_with(None)
        self.assertEqual(sites[0]["site_key"], "nsu-smart-education")

    def test_empty_setting_disables_all_plugin_sites(self):
        self.assertEqual(self.reload_with(""), ())

    def test_unknown_or_unsafe_plugin_names_are_skipped(self):
        self.assertEqual(self.reload_with("../etc,missing"), ())


if __name__ == "__main__":
    unittest.main()
