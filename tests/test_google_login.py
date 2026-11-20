"""Проверка входа в панель через Google: какие адреса пускаем, а какие нет."""

from __future__ import annotations

import unittest

from app.main import verify_google_identity

CLIENT = "123-abc.apps.googleusercontent.com"
ALLOWED = ("owner@example.com",)


def info(**overrides):
    base = {"aud": CLIENT, "email": "owner@example.com", "email_verified": "true"}
    base.update(overrides)
    return base


class VerifyGoogleIdentityTests(unittest.TestCase):
    def test_allowed_verified_address_passes(self):
        self.assertEqual(verify_google_identity(info(), CLIENT, ALLOWED), "owner@example.com")

    def test_email_is_case_insensitive(self):
        self.assertEqual(verify_google_identity(info(email="Owner@Example.com"), CLIENT, ALLOWED),
                         "owner@example.com")

    def test_other_address_is_refused(self):
        self.assertIsNone(verify_google_identity(info(email="stranger@example.com"), CLIENT, ALLOWED))

    def test_unverified_address_is_refused(self):
        self.assertIsNone(verify_google_identity(info(email_verified="false"), CLIENT, ALLOWED))

    def test_token_for_another_app_is_refused(self):
        self.assertIsNone(verify_google_identity(info(aud="someone-else"), CLIENT, ALLOWED))

    def test_empty_allowlist_refuses_everyone(self):
        self.assertIsNone(verify_google_identity(info(), CLIENT, ()))

    def test_missing_client_id_refuses(self):
        self.assertIsNone(verify_google_identity(info(aud=""), "", ALLOWED))


if __name__ == "__main__":
    unittest.main()
