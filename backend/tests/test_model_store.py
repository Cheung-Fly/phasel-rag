import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app import model_store
from app.main import ModelProfileRequest


class ModelProfileTests(unittest.TestCase):
    def test_rejects_non_https_or_credentialed_endpoint(self):
        for url in ("http://api.example.com/v1", "https://user:pw@api.example.com/v1"):
            with self.assertRaises(ValidationError):
                ModelProfileRequest(name="a", model="m", base_url=url, api_key="k")

    def test_accepts_https_endpoint_and_trims_slash(self):
        req = ModelProfileRequest(name="a", model="m", base_url="https://api.example.com/v1/", api_key="k")
        self.assertEqual(req.base_url, "https://api.example.com/v1")

    def test_no_default_owner_means_env_fallback(self):
        model_store.set_default_model_owner(None)
        self.assertIsNone(model_store.active_profile())

    def test_lookup_failure_falls_back_to_env(self):
        model_store.set_default_model_owner("u")
        try:
            with patch.object(model_store, "get_model_store", side_effect=RuntimeError("down")):
                self.assertIsNone(model_store.active_profile())
        finally:
            model_store.set_default_model_owner(None)


if __name__ == "__main__":
    unittest.main()
