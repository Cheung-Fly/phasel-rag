import unittest

from pydantic import ValidationError

from app.config import Settings


class PasswordModeConfigTests(unittest.TestCase):
    def test_requires_long_password(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, auth_mode="password", access_password="short")

    def test_accepts_long_password(self):
        s = Settings(_env_file=None, auth_mode="password", access_password="x" * 12)
        self.assertEqual(s.auth_mode, "password")

    def test_local_needs_no_password(self):
        self.assertEqual(Settings(_env_file=None, auth_mode="local").access_password, "")


if __name__ == "__main__":
    unittest.main()
