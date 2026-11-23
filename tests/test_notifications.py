"""Forge's notices look and behave the same everywhere: one app name ("Forge OS"), an icon, and
critical urgency (which never times out in Plasma) only where data could be lost."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = ["files/system/usr/bin/forge-backup", "files/system/usr/bin/forge-shared-drive",
         "files/system/usr/bin/forge-game", "files/system/usr/bin/forge-settings-sync"]


def calls(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        text = f.read()
    return [m.group(0) for m in re.finditer(r'\["notify-send"[^\]]*\]', text)]


class NoticeStyleTests(unittest.TestCase):
    def test_every_notice_has_the_name_and_an_icon(self):
        for path in TOOLS:
            found = calls(path)
            self.assertTrue(found, path)
            for c in found:
                self.assertIn('"-a", "Forge OS"', c, path)
                self.assertIn('"-i"', c, path)

    def test_backup_reminders_are_not_critical(self):
        with open(os.path.join(ROOT, "files/system/usr/bin/forge-backup"), encoding="utf-8") as f:
            text = f.read()
        self.assertRegex(text, r'notify\("No backup for " \+ when,[^)]*urgent=False')
        self.assertRegex(text, r'notify\(f"Backup restore test not done[^)]*urgent=False')
        self.assertNotRegex(text, r'notify\("Backup failed", f\.message, urgent=False')

    def test_shared_drive_problems_are_normal(self):
        for c in calls("files/system/usr/bin/forge-shared-drive"):
            self.assertNotIn('"critical"', c)


if __name__ == "__main__":
    unittest.main()
