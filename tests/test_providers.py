import os
import pathlib
import json
import types

import unittest
from unittest import mock

# Ensure the repository's providers module is imported, not the global one.
import sys
sys.path.insert(0, '/data/git/hermes-plugin-quota-governor')
import providers

class TestRetryHttp(unittest.TestCase):
    def test_successful_retry(self):
        """_retry_http should retry on transient HTTP errors until success."""
        from urllib.error import HTTPError
        responses = [
            HTTPError(url='http://example', code=502, msg='Bad', hdrs=None, fp=None),
            HTTPError(url='http://example', code=503, msg='Server', hdrs=None, fp=None),
            {'foo': 'bar'},
        ]
        dummy_fn = mock.Mock(side_effect=responses)
        result = providers._retry_http(dummy_fn, retries=2)
        self.assertEqual(result, {'foo': 'bar'})
        self.assertEqual(dummy_fn.call_count, 3)

    def test_permanent_failure(self):
        """If all retries fail with a non-transient error, the exception propagates."""
        from urllib.error import HTTPError
        dummy_fn = mock.Mock(side_effect=[
            HTTPError(url='http://example', code=404, msg='Not', hdrs=None, fp=None),
            HTTPError(url='http://example', code=404, msg='Not', hdrs=None, fp=None),
        ])
        with self.assertRaises(HTTPError):
            providers._retry_http(dummy_fn, retries=1)

class TestOpencodeParse(unittest.TestCase):
    def test_parse_normal(self):
        data = {
            'usage': {
                'rolling': {'percent': 12.5},
                'weekly': {'percent': 25.0},
                'monthly': {'percent': 50.0},
            }
        }
        result = providers._opencode_parse_response(data)
        self.assertEqual(result, {'rolling_pct': 12.5, 'weekly_pct': 25.0, 'monthly_pct': 50.0})

    def test_parse_missing(self):
        data = {}
        result = providers._opencode_parse_response(data)
        self.assertEqual(result, {'rolling_pct': None, 'weekly_pct': None, 'monthly_pct': None})

class TestGetEnv(unittest.TestCase):
    def setUp(self):
        self.env_path = pathlib.Path('temp.env')
        self.env_path.write_text('TEST_VAR=hello\n# comment\n')

    def tearDown(self):
        self.env_path.unlink(missing_ok=True)

    def test_from_env(self):
        os.environ['TEST_VAR'] = 'env_value'
        val = providers._get_env('TEST_VAR')
        self.assertEqual(val, 'env_value')

    def test_from_file(self):
        if 'TEST_VAR' in os.environ:
            del os.environ['TEST_VAR']
        val = providers._get_env('TEST_VAR', env_file='temp.env')
        self.assertEqual(val, 'hello')

class TestQuotaSnapshot(unittest.TestCase):
    def test_properties(self):
        qs = providers.QuotaSnapshot(
            ollama_session_pct=10.0,
            ollama_weekly_pct=20.0,
            ollama_session_requests=100,
            ollama_weekly_requests=200,
            ollama_activity_cost=5.5,
            nanogpt_daily_pct=30.0,
        )
        self.assertAlmostEqual(qs.session_pct, 10.0)
        self.assertAlmostEqual(qs.weekly_pct, 20.0)
        self.assertFalse(qs.has_errors())

    def test_errors(self):
        qs = providers.QuotaSnapshot(errors=['e1'])
        self.assertTrue(qs.has_errors())

if __name__ == '__main__':
    unittest.main()

