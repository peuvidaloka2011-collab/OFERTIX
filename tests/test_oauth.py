"""Testes OAuth com respostas simuladas. Nao faz chamadas reais de rede."""
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import oauth_ml


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.env = patch.dict(os.environ, {
            "ML_CLIENT_ID": "123456",
            "ML_CLIENT_SECRET": "secret-test-only",
            "ML_REDIRECT_URI": "https://exemplo.onrender.com/oauth/callback",
            "OFERTIX_TOKEN_KEY": "random-test-key-of-more-than-32-characters-123",
        })
        self.env.start()
        oauth_ml.init(self.path)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_pkce_and_one_time_state(self):
        url = oauth_ml.start(self.path)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertIn("state", query)
        self.assertTrue(query["code_challenge"][0])
        with patch.object(oauth_ml, "_post_token", return_value={
            "access_token": "fake-access", "refresh_token": "fake-refresh",
            "expires_in": 3600, "scope": "read offline_access",
        }) as post:
            result = oauth_ml.exchange(self.path, query["state"][0], "mock-code")
        self.assertEqual(result["access_token"], "fake-access")
        self.assertTrue(post.call_args.args[0]["code_verifier"])
        self.assertNotEqual(query["code_challenge"][0], post.call_args.args[0]["code_verifier"])
        self.assertEqual(oauth_ml.access_token(self.path), "fake-access")
        with self.assertRaises(oauth_ml.OAuthFlowError):
            oauth_ml.exchange(self.path, query["state"][0], "mock-code")

    def test_rejects_unknown_state(self):
        with self.assertRaises(oauth_ml.OAuthFlowError):
            oauth_ml.exchange(self.path, "forged-state", "fake-code")

    def test_refresh_rotates_token(self):
        url = oauth_ml.start(self.path)
        state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
        with patch.object(oauth_ml, "_post_token", return_value={
            "access_token": "old", "refresh_token": "refresh-old", "expires_in": 60,
        }):
            oauth_ml.exchange(self.path, state, "mock-code")
        with patch.object(oauth_ml, "_post_token", return_value={
            "access_token": "new", "refresh_token": "refresh-new", "expires_in": 3600,
        }) as call:
            self.assertEqual(oauth_ml.access_token(self.path), "new")
        self.assertEqual(call.call_args.args[0]["grant_type"], "refresh_token")
        self.assertEqual(call.call_args.args[0]["refresh_token"], "refresh-old")
        self.assertEqual(oauth_ml._load_token(self.path)["refresh_token"], "refresh-new")


if __name__ == "__main__":
    unittest.main()
