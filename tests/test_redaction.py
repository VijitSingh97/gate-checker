"""Tests for `_redact_token` — the bot-token scrubber that protects
journal logs from leaking the Telegram credential when a
`requests.RequestException` stringifies the offending URL.

The regression to defend against: an exception like
`ConnectionError(...api.telegram.org/bot123456:ABCDEFG.../sendMessage...)`
gets logged verbatim, the operator's `journalctl -u base-station`
output contains the token, and anyone with read access to that
journal — including a support bundle pasted in a forum — sees it.
"""

from __future__ import annotations

import unittest

from tests import _helpers


class RedactTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bs = _helpers.import_base_station()

    def test_redacts_token_inside_url(self):
        token = "111111111:AAAA-BBBB-CCCC-DDDDEEEE_FFFFGGGGHHHH"
        message = (
            f"ConnectionError: HTTPSConnectionPool(host='api.telegram.org', "
            f"port=443): URL: /bot{token}/sendMessage"
        )
        out = self.bs._redact_token(message)
        self.assertNotIn(token, out)
        self.assertIn("<TELEGRAM_TOKEN_REDACTED>", out)
        # Surrounding context preserved so log lines stay useful.
        self.assertIn("api.telegram.org", out)
        self.assertIn("/sendMessage", out)

    def test_redacts_bare_token(self):
        token = "999999999:_-abcdefghijklmnopqrstuvwxyz0123456789"
        out = self.bs._redact_token(f"got {token} then more")
        self.assertNotIn(token, out)

    def test_preserves_non_token_text(self):
        clean = "the system clock is 2026-05-22 12:34:56 UTC"
        self.assertEqual(self.bs._redact_token(clean), clean)

    def test_does_not_redact_too_short(self):
        """Real Telegram tokens are well over 25 chars after the colon.
        The regex requires 20+; a fake "123:abc" should NOT be redacted.
        """
        out = self.bs._redact_token("ID 123:abc and 12345:short")
        self.assertIn("123:abc", out)
        self.assertIn("12345:short", out)
        self.assertNotIn("<TELEGRAM_TOKEN_REDACTED>", out)

    def test_redacts_multiple_occurrences(self):
        t = "222222222:" + "x" * 30
        out = self.bs._redact_token(f"first {t} second {t} third")
        self.assertEqual(out.count("<TELEGRAM_TOKEN_REDACTED>"), 2)


if __name__ == "__main__":
    unittest.main()
