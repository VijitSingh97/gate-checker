"""Tests for the adaptive /open and /close grace period.

Two layers:

  - `_compute_actuation_threshold_seconds` — the pure math. Warmup,
    normal-mode mean+kσ, MIN_SLACK floor, ABSOLUTE_CEILING cap.

  - `GateRegistry.record_actuation` / `recent_actuation_durations_ms` —
    the SQLite plumbing. Per-bucket isolation, ordering, retention.

The end-to-end "lora_command records on success but not on noop /
timeout" path is in test_lora_commands.py against a mocked transport.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from tests import _helpers


class ComputeThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bs = _helpers.import_base_station()

    def test_empty_buffer_returns_ceiling(self):
        """No samples at all → warmup → full ceiling."""
        self.assertEqual(
            self.bs._compute_actuation_threshold_seconds([]),
            self.bs.ACTUATION_ABSOLUTE_CEILING_SECONDS,
        )

    def test_below_min_samples_returns_ceiling(self):
        """A handful of samples isn't enough to fit a distribution
        against — keep the safe wide window until we have enough
        history."""
        few = [12000, 13000, 11500]  # 3 samples, MIN_SAMPLES = 5
        self.assertEqual(
            self.bs._compute_actuation_threshold_seconds(few),
            self.bs.ACTUATION_ABSOLUTE_CEILING_SECONDS,
        )

    def test_tight_distribution_uses_slack_floor(self):
        """A perfectly steady gate (σ ≈ 0) would otherwise return
        threshold ≈ μ — too tight against real-world jitter. The
        MIN_SLACK floor keeps a sensible margin above the mean."""
        # Five identical samples at 12s → mean=12, stdev=0 →
        # μ+kσ = 12. MIN_SLACK floor lifts it to 12 + 3 = 15.
        samples = [12000] * 5
        result = self.bs._compute_actuation_threshold_seconds(samples)
        expected = 12.0 + self.bs.ACTUATION_MIN_SLACK_SECONDS
        self.assertAlmostEqual(result, expected, places=3)

    def test_normal_distribution_uses_mean_plus_k_sigma(self):
        """When σ is large enough that μ+kσ exceeds MIN_SLACK, the
        z-score term wins. Use a synthetic sample with known μ and σ."""
        # 5 samples: 10s, 11s, 12s, 13s, 14s. Mean = 12, pop variance
        # = ((4+1+0+1+4)/5) = 2, stdev = sqrt(2) ≈ 1.4142.
        # μ + 3σ ≈ 16.24. Floor μ+slack = 15. Max picks 16.24.
        samples = [10000, 11000, 12000, 13000, 14000]
        result = self.bs._compute_actuation_threshold_seconds(samples)
        expected = 12.0 + self.bs.ACTUATION_K_MULTIPLIER * (2.0 ** 0.5)
        self.assertAlmostEqual(result, expected, places=3)

    def test_absolute_ceiling_caps_extreme_variance(self):
        """If a gate's history is wildly inconsistent (e.g. one stuck
        cycle that took 60s among normal 12s ones), μ+kσ could blow
        past anything reasonable. The ceiling stops the threshold
        from growing unboundedly."""
        # 4 normal samples + one outlier. Mean and stdev both inflated.
        samples = [12000, 12000, 12000, 12000, 120000]
        result = self.bs._compute_actuation_threshold_seconds(samples)
        self.assertEqual(
            result, self.bs.ACTUATION_ABSOLUTE_CEILING_SECONDS
        )

    def test_threshold_never_exceeds_ceiling(self):
        """Property: result is ALWAYS <= ABSOLUTE_CEILING_SECONDS."""
        for samples in (
            [],
            [5000],
            [1000] * 5,
            [50000] * 20,
            list(range(1000, 30000, 500)),
        ):
            result = self.bs._compute_actuation_threshold_seconds(samples)
            self.assertLessEqual(
                result,
                self.bs.ACTUATION_ABSOLUTE_CEILING_SECONDS,
                f"threshold for {samples!r} exceeded ceiling",
            )

    def test_threshold_never_below_mean_for_post_warmup(self):
        """Property: once warmed, threshold is never below the mean —
        otherwise we'd time out a perfectly normal cycle. The MIN_SLACK
        floor exists exactly to guarantee this."""
        samples = [8000, 8000, 8000, 8000, 8000]  # mean = 8s
        result = self.bs._compute_actuation_threshold_seconds(samples)
        self.assertGreater(result, 8.0)


class GateRegistryActuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bs = _helpers.import_base_station()
        self.db_path = tempfile.mktemp(suffix=".db")
        self.registry = self.bs.GateRegistry(self.db_path)
        self.key = _helpers.fresh_fernet_key()
        self.registry.register_gate("GATE-PASTUR", self.key, "Pasture")
        self.registry.register_gate("GATE-DRIVE1", self.key, None)

    def tearDown(self) -> None:
        self.registry.close()
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    def test_schema_includes_actuation_cycles_table(self):
        with sqlite3.connect(self.db_path) as conn:
            cols = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(actuation_cycles)"
                )
            }
        self.assertEqual(
            cols,
            {"id", "gate_id", "action", "duration_ms", "recorded_at"},
        )

    def test_record_and_read_back(self):
        self.registry.record_actuation("GATE-PASTUR", "open", 12000)
        self.registry.record_actuation("GATE-PASTUR", "open", 13500)
        got = self.registry.recent_actuation_durations_ms(
            "GATE-PASTUR", "open"
        )
        # Newest first per the docstring contract.
        self.assertEqual(got, [13500, 12000])

    def test_recent_durations_respects_limit(self):
        for d in (11000, 12000, 13000, 14000, 15000):
            self.registry.record_actuation("GATE-PASTUR", "open", d)
        got = self.registry.recent_actuation_durations_ms(
            "GATE-PASTUR", "open", limit=3
        )
        self.assertEqual(got, [15000, 14000, 13000])

    def test_per_gate_isolation(self):
        """Recording for GATE-PASTUR must NOT pollute GATE-DRIVE1's
        buffer — open and close stats are per-gate."""
        self.registry.record_actuation("GATE-PASTUR", "open", 12000)
        self.registry.record_actuation("GATE-DRIVE1", "open", 22000)
        self.assertEqual(
            self.registry.recent_actuation_durations_ms(
                "GATE-PASTUR", "open"
            ),
            [12000],
        )
        self.assertEqual(
            self.registry.recent_actuation_durations_ms(
                "GATE-DRIVE1", "open"
            ),
            [22000],
        )

    def test_per_action_isolation(self):
        """Open and close get separate buffers — different physical
        loads on a hinged gate (gravity, sag) make them asymmetric."""
        self.registry.record_actuation("GATE-PASTUR", "open", 10000)
        self.registry.record_actuation("GATE-PASTUR", "close", 16000)
        self.assertEqual(
            self.registry.recent_actuation_durations_ms(
                "GATE-PASTUR", "open"
            ),
            [10000],
        )
        self.assertEqual(
            self.registry.recent_actuation_durations_ms(
                "GATE-PASTUR", "close"
            ),
            [16000],
        )

    def test_retention_prunes_oldest_per_bucket(self):
        """Insert past the retention cap; only the most recent
        ACTUATION_RETENTION_PER_BUCKET rows survive."""
        cap = self.bs.ACTUATION_RETENTION_PER_BUCKET
        total = cap + 25
        for i in range(total):
            self.registry.record_actuation("GATE-PASTUR", "open", i)
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM actuation_cycles "
                "WHERE gate_id = ? AND action = ?",
                ("GATE-PASTUR", "open"),
            ).fetchone()[0]
        self.assertEqual(count, cap)
        # Oldest values (0..total-cap-1) were trimmed; newest survive.
        got = self.registry.recent_actuation_durations_ms(
            "GATE-PASTUR", "open", limit=cap
        )
        self.assertEqual(got[0], total - 1)
        self.assertEqual(got[-1], total - cap)

    def test_retention_isolated_per_bucket(self):
        """Pruning open's history must not touch close's history.
        Otherwise a hot /open gate would erase its own /close samples."""
        cap = self.bs.ACTUATION_RETENTION_PER_BUCKET
        for i in range(cap + 5):
            self.registry.record_actuation("GATE-PASTUR", "open", i)
        # close has 3 samples; none should be pruned by the open inserts.
        self.registry.record_actuation("GATE-PASTUR", "close", 17000)
        self.registry.record_actuation("GATE-PASTUR", "close", 18000)
        self.registry.record_actuation("GATE-PASTUR", "close", 19000)
        got = self.registry.recent_actuation_durations_ms(
            "GATE-PASTUR", "close"
        )
        self.assertEqual(got, [19000, 18000, 17000])

    def test_empty_buffer_returns_empty_list(self):
        """No history yet — the result is an empty list, not None.
        Callers feed this directly into the threshold helper, which
        handles the empty case explicitly."""
        self.assertEqual(
            self.registry.recent_actuation_durations_ms(
                "GATE-PASTUR", "open"
            ),
            [],
        )


class GateEventsRetentionTests(unittest.TestCase):
    """Existing gate_events table also has a retention cap so the DB
    can't grow indefinitely over years of operation."""

    def setUp(self) -> None:
        self.bs = _helpers.import_base_station()
        self.db_path = tempfile.mktemp(suffix=".db")
        self.registry = self.bs.GateRegistry(self.db_path)
        self.key = _helpers.fresh_fernet_key()
        self.registry.register_gate("GATE-PASTUR", self.key, None)
        self.registry.register_gate("GATE-DRIVE1", self.key, None)

    def tearDown(self) -> None:
        self.registry.close()
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    def test_log_event_prunes_per_gate(self):
        cap = self.bs.EVENT_LOG_RETENTION_PER_GATE
        total = cap + 25
        for i in range(total):
            self.registry.log_event(
                "GATE-PASTUR", self.bs.EVENT_GATE_STATE, f"alert:open:{i}"
            )
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM gate_events WHERE gate_id = ?",
                ("GATE-PASTUR",),
            ).fetchone()[0]
        self.assertEqual(count, cap)

    def test_retention_isolated_per_gate(self):
        """A hot gate's prune must not erase a quiet gate's history."""
        cap = self.bs.EVENT_LOG_RETENTION_PER_GATE
        for i in range(cap + 5):
            self.registry.log_event(
                "GATE-PASTUR", self.bs.EVENT_GATE_STATE, f"alert:open:{i}"
            )
        self.registry.log_event(
            "GATE-DRIVE1", self.bs.EVENT_GATE_STATE, "alert:open"
        )
        with sqlite3.connect(self.db_path) as conn:
            quiet_count = conn.execute(
                "SELECT COUNT(*) FROM gate_events WHERE gate_id = ?",
                ("GATE-DRIVE1",),
            ).fetchone()[0]
        self.assertEqual(quiet_count, 1)

    def test_last_recorded_state_survives_pruning(self):
        """The most recent state event for each gate must always be
        kept — otherwise the notify-on-transition dedup in `_dispatch`
        would silently break after enough events accumulated."""
        cap = self.bs.EVENT_LOG_RETENTION_PER_GATE
        for _ in range(cap):
            self.registry.log_event(
                "GATE-PASTUR", self.bs.EVENT_GATE_STATE, "alert:open"
            )
        self.registry.log_event(
            "GATE-PASTUR", self.bs.EVENT_GATE_STATE, "status:closed"
        )
        self.assertEqual(
            self.registry.last_recorded_state("GATE-PASTUR"),
            "closed",
        )


if __name__ == "__main__":
    unittest.main()
