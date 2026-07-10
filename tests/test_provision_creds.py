"""Tests for the portal-password migration off FAT32 /boot.

`provision._migrate_provision_creds` mirrors the gate's
ranch-gate-config-migrate.sh: the portal password must not stay on the
FAT32 boot partition (mode bits are advisory there — pulling the SD
card reads it), and the copy → replace → remove ordering must be safe
to interrupt at any point without losing the only copy.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest

from tests import _helpers


class ProvisionCredsMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prov = _helpers.import_provision()
        self.tmpdir = tempfile.mkdtemp()
        # Redirect the module's paths into the sandbox: "boot" plays the
        # FAT32 partition, "state" plays /var/lib/base_station.
        self.boot = os.path.join(self.tmpdir, "boot", "provision_creds.env")
        self.state_dir = os.path.join(self.tmpdir, "state")
        os.makedirs(os.path.dirname(self.boot))
        self.prov.PROVISION_CREDS_BOOT_PATH = self.boot
        self.prov.STATE_DIR = self.state_dir
        self.prov.PROVISION_CREDS_PATH = os.path.join(
            self.state_dir, "provision_creds.env"
        )

    def _write_boot_creds(self, body: str = "PORTAL_PASSWORD=hunter2\n") -> None:
        with open(self.boot, "w", encoding="utf-8") as fh:
            fh.write(body)

    def test_migrates_and_removes_boot_copy(self):
        self._write_boot_creds()
        self.prov._migrate_provision_creds()
        self.assertFalse(os.path.exists(self.boot))
        with open(self.prov.PROVISION_CREDS_PATH, encoding="utf-8") as fh:
            self.assertIn("PORTAL_PASSWORD=hunter2", fh.read())

    def test_migrated_file_is_owner_only(self):
        self._write_boot_creds()
        self.prov._migrate_provision_creds()
        mode = stat.S_IMODE(os.stat(self.prov.PROVISION_CREDS_PATH).st_mode)
        self.assertEqual(mode, 0o600)

    def test_noop_when_boot_copy_absent(self):
        self.prov._migrate_provision_creds()
        self.assertFalse(os.path.exists(self.prov.PROVISION_CREDS_PATH))

    def test_crash_between_copy_and_remove_resolves_next_run(self):
        # Simulate "previous run crashed after the copy": both files
        # exist. The next run must keep the ext4 copy (not re-copy over
        # it) and clean up the boot copy.
        self._write_boot_creds("PORTAL_PASSWORD=stale-on-boot\n")
        os.makedirs(self.state_dir)
        with open(self.prov.PROVISION_CREDS_PATH, "w", encoding="utf-8") as fh:
            fh.write("PORTAL_PASSWORD=already-migrated\n")
        self.prov._migrate_provision_creds()
        self.assertFalse(os.path.exists(self.boot))
        with open(self.prov.PROVISION_CREDS_PATH, encoding="utf-8") as fh:
            self.assertIn("already-migrated", fh.read())

    def test_load_reads_migrated_path(self):
        self._write_boot_creds()
        password = self.prov._load_provision_credentials()
        self.assertEqual(password, "hunter2")
        self.assertFalse(os.path.exists(self.boot))

    def test_load_falls_back_to_boot_when_migration_fails(self):
        # Point STATE_DIR somewhere unwritable-ish: a path under a file.
        blocker = os.path.join(self.tmpdir, "blocker")
        open(blocker, "w").close()
        self.prov.STATE_DIR = os.path.join(blocker, "nope")
        self.prov.PROVISION_CREDS_PATH = os.path.join(
            self.prov.STATE_DIR, "provision_creds.env"
        )
        self._write_boot_creds()
        password = self.prov._load_provision_credentials()
        self.assertEqual(password, "hunter2")
        # Boot copy must survive — it's the only copy we have.
        self.assertTrue(os.path.exists(self.boot))

    def test_load_exits_when_no_copy_exists(self):
        with self.assertRaises(SystemExit):
            self.prov._load_provision_credentials()


if __name__ == "__main__":
    unittest.main()
