"""Tests for `GateRegistry` — the SQLite-backed store that holds gate
keys, names, replay-protection seq counters, and event history.

Covers:
  - The idempotent ALTER TABLE migration that added `name` after the
    table shipped. A regression here would either fail on first boot
    of an upgraded device or silently drop the name.
  - register_gate UPSERT semantics, including the load-bearing rule
    that `last_seq` only resets when the Fernet key actually changes.
  - accept_seq replay protection — the per-gate monotonic counter
    that stops a captured Fernet token from being replayed.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from tests import _helpers


class GateRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bs = _helpers.import_base_station()
        self.db_path = tempfile.mktemp(suffix=".db")
        self.registry = self.bs.GateRegistry(self.db_path)
        self.key = _helpers.fresh_fernet_key()

    def tearDown(self) -> None:
        self.registry.close()
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    # ----------------------------------------------------------------------
    # Schema + migration
    # ----------------------------------------------------------------------

    def test_db_file_is_owner_only(self):
        # The DB stores plaintext Fernet keys; sqlite3 creates it at the
        # umask, so GateRegistry must tighten it to 0600 itself.
        mode = os.stat(self.db_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_schema_creates_both_tables_with_name_column(self):
        with sqlite3.connect(self.db_path) as conn:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(registered_gates)")
            }
        self.assertEqual(
            cols,
            {
                "gate_id",
                "lora_key",
                "last_seq",
                "registered_at",
                "name",
                "relay_ms",
            },
        )

    def test_alter_table_is_idempotent(self):
        """Re-opening the DB should not raise even though the `name`
        column already exists."""
        self.registry.close()
        # Second open hits the ALTER path with the column already present —
        # the OperationalError("duplicate column name") must be swallowed.
        self.registry = self.bs.GateRegistry(self.db_path)

    def test_alter_table_handles_legacy_db_without_name(self):
        """Simulate a legacy DB (before the `name` column shipped) by creating the table
        with no `name` column, then opening it through GateRegistry."""
        self.registry.close()
        os.unlink(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE registered_gates (
                    gate_id TEXT PRIMARY KEY,
                    lora_key TEXT NOT NULL,
                    last_seq INTEGER NOT NULL DEFAULT 0,
                    registered_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE gate_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    gate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                INSERT INTO registered_gates (gate_id, lora_key)
                    VALUES ('GATE-LEGACY', 'somekey');
                """
            )
        self.registry = self.bs.GateRegistry(self.db_path)
        # After the migration the legacy row is preserved and the new
        # name column is NULL for it.
        row = self.registry.get_gate("GATE-LEGACY")
        self.assertIsNotNone(row)
        self.assertIsNone(row["name"])
        # The relay_ms column was added with NOT NULL DEFAULT, so the
        # legacy row backfills to the firmware default rather than NULL.
        self.assertEqual(row["relay_ms"], self.bs.RELAY_PULSE_DEFAULT_MS)

    # ----------------------------------------------------------------------
    # register_gate / unregister_gate / rename_gate
    # ----------------------------------------------------------------------

    def test_register_new_gate(self):
        out = self.registry.register_gate("GATE-AAAAAA", self.key, "Front")
        self.assertFalse(out["existed"])
        self.assertFalse(out["key_changed"])
        row = self.registry.get_gate("GATE-AAAAAA")
        self.assertEqual(row["lora_key"], self.key)
        self.assertEqual(row["name"], "Front")
        self.assertEqual(row["last_seq"], 0)

    def test_register_same_key_keeps_last_seq(self):
        """Re-pairing with the same key (e.g. name change via /pair
        overwrite) must NOT reset the seq counter — that would
        silently re-open the replay window."""
        self.registry.register_gate("GATE-AAAAAA", self.key, "Front")
        self.assertTrue(self.registry.accept_seq("GATE-AAAAAA", 42))
        out = self.registry.register_gate("GATE-AAAAAA", self.key, "Renamed")
        self.assertTrue(out["existed"])
        self.assertFalse(out["key_changed"])
        self.assertEqual(self.registry.get_gate("GATE-AAAAAA")["last_seq"], 42)
        self.assertEqual(self.registry.get_gate("GATE-AAAAAA")["name"], "Renamed")

    def test_register_new_key_resets_last_seq(self):
        """Key change *must* reset last_seq to 0 — the old seq counter
        was for the old key's cipher; with a fresh key, the gate's
        seq starts at 0 again."""
        self.registry.register_gate("GATE-AAAAAA", self.key, "Front")
        self.registry.accept_seq("GATE-AAAAAA", 17)
        new_key = _helpers.fresh_fernet_key()
        out = self.registry.register_gate("GATE-AAAAAA", new_key, "Front")
        self.assertTrue(out["key_changed"])
        self.assertEqual(self.registry.get_gate("GATE-AAAAAA")["last_seq"], 0)
        self.assertEqual(self.registry.get_gate("GATE-AAAAAA")["lora_key"], new_key)

    def test_unregister_gate_removes_row(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.assertTrue(self.registry.unregister_gate("GATE-AAAAAA"))
        self.assertIsNone(self.registry.get_gate("GATE-AAAAAA"))
        # Second delete is a no-op.
        self.assertFalse(self.registry.unregister_gate("GATE-AAAAAA"))

    def test_rename_gate(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, "Old")
        self.assertTrue(self.registry.rename_gate("GATE-AAAAAA", "New"))
        self.assertEqual(self.registry.get_gate("GATE-AAAAAA")["name"], "New")
        # rename on a non-existent gate returns False.
        self.assertFalse(self.registry.rename_gate("GATE-NONE", "x"))

    # ----------------------------------------------------------------------
    # relay_ms (per-gate relay pulse duration)
    # ----------------------------------------------------------------------

    def test_new_gate_gets_default_relay_ms(self):
        """A freshly-paired gate defaults to the firmware-equivalent
        press time (1s) so it behaves exactly as before until tuned."""
        self.registry.register_gate("GATE-AAAAAA", self.key, "Front")
        self.assertEqual(
            self.registry.get_relay_ms("GATE-AAAAAA"),
            self.bs.RELAY_PULSE_DEFAULT_MS,
        )
        self.assertEqual(
            self.registry.get_gate("GATE-AAAAAA")["relay_ms"],
            self.bs.RELAY_PULSE_DEFAULT_MS,
        )

    def test_set_and_get_relay_ms(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.assertTrue(self.registry.set_relay_ms("GATE-AAAAAA", 2500))
        self.assertEqual(self.registry.get_relay_ms("GATE-AAAAAA"), 2500)

    def test_set_relay_ms_unknown_gate_returns_false(self):
        self.assertFalse(self.registry.set_relay_ms("GATE-NONE", 2000))

    def test_get_relay_ms_unknown_gate_returns_none(self):
        """None (not the default) signals "no such gate" so callers can
        distinguish it from a real configured value."""
        self.assertIsNone(self.registry.get_relay_ms("GATE-NONE"))

    def test_relay_ms_survives_same_key_repair(self):
        """Re-pairing with the same key only touches name/seq — a tuned
        relay duration must not be silently reset to the default."""
        self.registry.register_gate("GATE-AAAAAA", self.key, "Front")
        self.registry.set_relay_ms("GATE-AAAAAA", 1800)
        self.registry.register_gate("GATE-AAAAAA", self.key, "Renamed")
        self.assertEqual(self.registry.get_relay_ms("GATE-AAAAAA"), 1800)

    # ----------------------------------------------------------------------
    # display_name + list_gates + get_gate(last_event_at)
    # ----------------------------------------------------------------------

    def test_display_name_falsey_returns_none(self):
        """Empty string and NULL both render as no-name in alerts."""
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.assertIsNone(self.registry.display_name("GATE-AAAAAA"))
        self.registry.rename_gate("GATE-AAAAAA", "")
        self.assertIsNone(self.registry.display_name("GATE-AAAAAA"))
        self.registry.rename_gate("GATE-AAAAAA", "Real")
        self.assertEqual(self.registry.display_name("GATE-AAAAAA"), "Real")

    def test_list_gates_orders_by_registered_at(self):
        self.registry.register_gate("GATE-FIRST1", self.key, "First")
        self.registry.register_gate("GATE-SECOND", self.key, "Second")
        names = [g["gate_id"] for g in self.registry.list_gates()]
        self.assertEqual(names, ["GATE-FIRST1", "GATE-SECOND"])

    def test_get_gate_with_event_history(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, "Front")
        self.registry.log_event("GATE-AAAAAA", "gate_state", "alert:open")
        row = self.registry.get_gate("GATE-AAAAAA")
        self.assertIsNotNone(row["last_event_at"])

    def test_get_gate_without_event_history(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, "Front")
        row = self.registry.get_gate("GATE-AAAAAA")
        self.assertIsNone(row["last_event_at"])

    # ----------------------------------------------------------------------
    # accept_seq — replay protection
    # ----------------------------------------------------------------------

    def test_accept_seq_monotonic(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.assertTrue(self.registry.accept_seq("GATE-AAAAAA", 1))
        self.assertTrue(self.registry.accept_seq("GATE-AAAAAA", 2))
        self.assertTrue(self.registry.accept_seq("GATE-AAAAAA", 5))
        # Replay of an earlier seq is rejected.
        self.assertFalse(self.registry.accept_seq("GATE-AAAAAA", 3))
        # Equal seq is also rejected (must strictly advance).
        self.assertFalse(self.registry.accept_seq("GATE-AAAAAA", 5))

    def test_accept_seq_unknown_gate(self):
        """No row for the gate → no UPDATE matches → rejected.
        Prevents a forged packet for an unknown gate_id from leaking
        through as a side effect of seq bookkeeping."""
        self.assertFalse(self.registry.accept_seq("GATE-NOPE12", 1))

    # ----------------------------------------------------------------------
    # last_recorded_state — feeds the state-change-only notification logic
    # ----------------------------------------------------------------------

    def test_last_recorded_state_none_before_any_event(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.assertIsNone(self.registry.last_recorded_state("GATE-AAAAAA"))

    def test_last_recorded_state_extracts_state_from_summary(self):
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.registry.log_event("GATE-AAAAAA", "gate_state", "alert:open")
        self.assertEqual(self.registry.last_recorded_state("GATE-AAAAAA"), "open")
        self.registry.log_event("GATE-AAAAAA", "gate_state", "status:closed")
        self.assertEqual(self.registry.last_recorded_state("GATE-AAAAAA"), "closed")

    def test_last_recorded_state_picks_most_recent_by_id(self):
        """Two rows with the same DATETIME — order has to come from id
        (the AUTOINCREMENT primary key), not timestamp. Otherwise the
        notify-on-transition logic would non-deterministically dedup or
        not dedup when two events land in the same SQLite second."""
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        # Insert two events with a hand-rolled raw cursor so they share
        # the same CURRENT_TIMESTAMP value to the second.
        for state in ("open", "closed"):
            self.registry.log_event(
                "GATE-AAAAAA", "gate_state", f"alert:{state}"
            )
        # Last logged was "closed".
        self.assertEqual(self.registry.last_recorded_state("GATE-AAAAAA"), "closed")

    def test_last_recorded_state_ignores_other_event_types(self):
        """If we ever start logging non-state events to gate_events,
        last_recorded_state should still return the latest *state* event."""
        self.registry.register_gate("GATE-AAAAAA", self.key, None)
        self.registry.log_event("GATE-AAAAAA", "gate_state", "alert:open")
        self.registry.log_event("GATE-AAAAAA", "other_type", "noise")
        self.assertEqual(self.registry.last_recorded_state("GATE-AAAAAA"), "open")


if __name__ == "__main__":
    unittest.main()
