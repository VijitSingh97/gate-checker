"""Tests for `factory_sticker.print_sticker`.

The helper is small but lives in the assembly-line path — every
operator-facing sticker is rendered through it, and a regression that
silently misaligns the layout would only be caught when someone holds
two stickers next to each other.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from tests import _helpers


class PrintStickerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fs = _helpers.import_factory_sticker()

    def _render(self, *pairs) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.fs.print_sticker(*pairs)
        return buf.getvalue()

    def test_rejects_empty_input(self):
        """Empty kv_pairs is almost certainly a caller bug — fail loud."""
        with self.assertRaises(ValueError):
            self.fs.print_sticker()

    def test_renders_divider_title_and_pairs(self):
        out = self._render(("Device ID", "BASE-AB12"))
        self.assertIn("-" * self.fs.STICKER_WIDTH, out)
        self.assertIn(self.fs.STICKER_TITLE, out)
        self.assertIn("Device ID:", out)
        self.assertIn("BASE-AB12", out)

    def test_column_alignment_with_uneven_labels(self):
        """All values must start in the same column regardless of label width.

        Longest label drives the column. Compute the column from the
        rendered output and assert every value-bearing line shares it.
        """
        out = self._render(
            ("Device ID", "BASE-AB12"),
            ("Setup Wi-Fi", "BaseStation_Setup"),
            ("Portal Login", "admin / xxxxxxxxxxxxx"),
        )
        value_lines = [
            line for line in out.splitlines()
            if line.startswith("  ") and ":" in line
        ]
        self.assertEqual(len(value_lines), 3, "expected three value rows")
        # Index of the first non-space char *after* the colon.
        starts = []
        for line in value_lines:
            colon_idx = line.index(":")
            after = colon_idx + 1
            while after < len(line) and line[after] == " ":
                after += 1
            starts.append(after)
        self.assertEqual(
            len(set(starts)), 1,
            f"value column drifted across rows: {starts}",
        )

    def test_single_pair_renders_cleanly(self):
        """One-field sticker still gets divider/title/divider scaffolding."""
        out = self._render(("Key", "Value"))
        lines = [l for l in out.splitlines() if l.strip()]
        # Expect 3 non-empty lines: top divider, title, one value, bottom divider.
        self.assertGreaterEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("-"))
        self.assertEqual(lines[1], self.fs.STICKER_TITLE)
        self.assertIn("Key:", lines[2])
        self.assertIn("Value", lines[2])
        self.assertTrue(lines[3].startswith("-"))


if __name__ == "__main__":
    unittest.main()
