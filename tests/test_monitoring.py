import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from questguard.monitoring import SavedProgressMonitor, load_snapshots, saved_paths


def capture_text(step=42, captured_at=1_800_000_000, character="Guard"):
    return (
        f'RXPQuestGuardSnapshot = {{schemaVersion=1, character="{character}", realm="Test", '
        f'currentGuide="43-44 Later", currentStep={step}, capturedAt={captured_at}, '
        'active={95261}, completed={95260}}'
    )


class MonitorTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.client = Path(temp.name) / "_forever_"
        self.path = self.client / "WTF" / "Account" / "fixture" / "Test" / "Guard" / "SavedVariables" / "RXPQuestGuard.lua"
        self.path.parent.mkdir(parents=True)
        self.monitor = SavedProgressMonitor(self.client)

    def save(self, *args, **kwargs):
        self.path.write_text(capture_text(*args, **kwargs), encoding="utf-8")

    def next_stable(self):
        self.assertFalse(self.monitor.poll().saves)
        return self.monitor.poll()

    def test_primary_path_only_and_atomic_replacement_with_same_timestamp_and_length(self):
        self.save()
        for name in ("RXPQuestGuard.lua.bak", "RXPQuestGuard.lua.tmp", "RXPGuides.lua"):
            self.path.with_name(name).write_text("", encoding="utf-8")
        account_file = self.path.parents[3] / "SavedVariables" / self.path.name
        account_file.parent.mkdir()
        account_file.write_text(capture_text(), encoding="utf-8")
        self.assertEqual(saved_paths(self.client), (self.path,))

        self.assertEqual(self.next_stable().saves[0][1].current_step, 42)
        self.assertFalse(self.monitor.poll().saves)
        before = self.path.stat()
        staging = self.path.with_suffix(".tmp")
        staging.write_text(capture_text(62, 1_800_000_001), encoding="utf-8")
        os.utime(staging, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(staging, self.path)
        self.assertEqual(self.path.stat().st_size, before.st_size)
        self.assertEqual(self.next_stable().saves[0][1].current_step, 62)

    def test_corrupt_and_older_saves_preserve_the_latest_good_snapshot(self):
        self.save()
        self.next_stable()
        self.path.write_text("RXPQuestGuardSnapshot = {", encoding="utf-8")
        self.assertIn("incomplete", self.next_stable().problems[0])
        self.assertEqual(self.monitor.accepted[self.path].current_step, 42)
        self.save(62, 1_800_000_002)
        self.assertEqual(self.next_stable().saves[0][1].current_step, 62)
        self.save(21, 1_800_000_001)
        self.assertIn("older", self.next_stable().problems[0])
        self.assertEqual(self.monitor.accepted[self.path].current_step, 62)

    def test_rejects_capture_if_saved_file_changes_during_the_read(self):
        self.save()
        self.assertFalse(self.monitor.poll().saves)

        def read_then_write(path):
            snapshots = load_snapshots(path)
            self.save(62, 1_800_000_002)
            return snapshots

        with patch("questguard.monitoring.load_snapshots", side_effect=read_then_write):
            self.assertFalse(self.monitor.poll().saves)
        self.assertEqual(self.next_stable().saves[0][1].current_step, 62)

    def test_transient_read_errors_retry_the_same_file_version(self):
        self.save()
        self.assertFalse(self.monitor.poll().saves)
        with patch("questguard.monitoring.load_snapshots", side_effect=PermissionError("temporarily locked")):
            retry = self.monitor.poll()
        self.assertIn("temporarily unreadable", retry.problems[0])
        self.assertFalse(retry.saves)
        self.assertEqual(self.monitor.poll().saves[0][1].current_step, 42)

    def test_unverified_character_never_imports(self):
        self.save(character="Different")
        self.assertIn("ownership", self.next_stable().problems[0])
        self.assertNotIn(self.path, self.monitor.accepted)


if __name__ == "__main__":
    unittest.main()
