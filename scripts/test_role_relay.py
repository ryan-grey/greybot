"""Relay regression: explicit identity and credential-free diagnostics."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import role_relay


class RelayTests(unittest.TestCase):
    def test_modal_reply_is_preserved(self):
        reply={"type":9,"data":{"custom_id":"greybot:raid:create:2:standard","title":"Create raid"}}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(reply).encode())):
            self.assertEqual(role_relay.forward(b'{}','signature','timestamp'),reply)

    def test_forwards_exact_signed_bytes_with_bot_identity(self):
        raw = b'{"token":"test-only-interaction","type":3}'
        reply = {"type": 4, "data": {"content": "Queued", "flags": 64}}
        def send(request, timeout):
            self.assertEqual(request.data, raw)
            self.assertEqual(request.get_header('X-signature-ed25519'), 'signature')
            self.assertEqual(request.get_header('X-signature-timestamp'), 'timestamp')
            self.assertTrue(request.get_header('User-agent').startswith('greyBot/'))
            self.assertEqual(timeout, 1.7)
            return io.BytesIO(json.dumps(reply).encode())
        with patch('urllib.request.urlopen', side_effect=send):
            self.assertEqual(role_relay.forward(raw, 'signature', 'timestamp'), reply)

    def test_http_failure_logs_only_type_and_status(self):
        error = urllib.error.HTTPError('https://example.test/private', 403,
                                      'private diagnostic', {}, None)
        output = io.StringIO()
        with patch('urllib.request.urlopen', side_effect=error), contextlib.redirect_stdout(output):
            result = role_relay.forward(b'private-body', 'private-signature', 'private-timestamp')
        self.assertEqual(result['data']['flags'], 64)
        self.assertEqual(json.loads(output.getvalue()), {
            'event': 'role_relay_failed', 'error_type': 'HTTPError', 'http_status': 403})
        self.assertNotIn('private', output.getvalue())


if __name__ == '__main__':
    unittest.main()
