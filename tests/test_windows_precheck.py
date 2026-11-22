"""tools/windows/forge-precheck.ps1: the read-only Windows checker the owner runs before installing.

The real Windows probes can't run here. The PowerShell tests replace every probe with fake data
and need pwsh (set FORGE_PWSH or put pwsh on PATH); without it they are skipped. The static
checks below always run."""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools/windows/forge-precheck.ps1")
TOOL_README = os.path.join(ROOT, "tools/windows/README.md")
PS_TESTS = os.path.join(ROOT, "tests/pwsh/forge-precheck.tests.ps1")
MAIN_README = os.path.join(ROOT, "README.md")

PWSH = os.environ.get("FORGE_PWSH") or shutil.which("pwsh")

# Verbs that change something. A command like Set-ItemProperty or Remove-Item must never appear.
BANNED_VERBS = (
    "Set", "New", "Remove", "Stop", "Start", "Restart", "Disable", "Enable", "Install", "Uninstall",
    "Update", "Clear", "Add", "Rename", "Move", "Copy", "Mount", "Dismount", "Suspend", "Resume",
    "Register", "Unregister", "Export", "Initialize", "Format", "Resize", "Repair", "Reset", "Optimize",
    "Lock", "Unlock", "Grant", "Revoke", "Protect", "Unprotect", "Backup", "Restore", "Push", "Publish",
    "Save", "Send", "Write-EventLog",
)
ALLOWED_VERB_NOUN = {
    # Save-ForgeReport is the one report writer (checked separately), Format-ForgeReport only builds text.
    "Save-ForgeReport", "Format-ForgeReport", "Format-ForgeSize",
}
BANNED_ANYWHERE = [
    r"Invoke-WebRequest", r"Invoke-RestMethod", r"\biwr\b", r"\birm\b", r"Invoke-Expression", r"\biex\b",
    r"Invoke-CimMethod", r"Invoke-WmiMethod", r"Set-CimInstance", r"Invoke-Command", r"Start-Process",
    r"Out-File", r"Set-Content", r"Add-Content", r"Tee-Object", r"Start-BitsTransfer",
    r"Net\.WebClient", r"Net\.Http", r"Net\.Sockets", r"WebRequest\]", r"DownloadString", r"DownloadFile",
    r"\.SetValue\(", r"\.DeleteValue\(", r"CreateSubKey", r"DeleteSubKey", r"-ComObject",
    r"\bbcdedit", r"\bdiskpart", r"\bmbr2gpt\.exe", r"\breg\.exe", r"\bnetsh", r"\bsc\.exe", r"\bschtasks",
    r"-ExecutionPolicy\s+\w+\s+-Scope", r">\s*\$", r"\s>>?\s",
]
# File writing APIs: only allowed inside Save-ForgeReport.
WRITE_APIS = [r"IO\.File\]::", r"FileStream", r"StreamWriter", r"FileMode", r"IO\.Directory\]::"]


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def strip_comments(text):
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join(line.split("#", 1)[0] if line.lstrip().startswith("#") else line
                     for line in text.splitlines())


def function_body(text, name):
    m = re.search(r"^function\s+" + re.escape(name) + r"\b.*?^\}", text, flags=re.S | re.M)
    return m.group(0) if m else ""


class StaticTest(unittest.TestCase):
    """Read-only by construction: these run everywhere, no PowerShell needed."""

    def setUp(self):
        self.text = read(SCRIPT)
        self.code = strip_comments(self.text)

    def test_ascii_only_for_windows_powershell_5_1(self):
        # 5.1 reads a file without a BOM in the local code page, so anything non-ASCII can break it.
        bad = [(i + 1, line) for i, line in enumerate(self.text.splitlines()) if not line.isascii()]
        self.assertEqual(bad, [])

    def test_requires_5_1_and_has_switches(self):
        self.assertRegex(self.text, r"(?m)^#Requires -Version 5\.1\s*$")
        for sw in ("Json", "SaveReport"):
            self.assertRegex(self.text, r"\[switch\]\s*\$" + sw)

    def test_no_write_cmdlets(self):
        found = []
        for m in re.finditer(r"\b([A-Z][a-z]+)-([A-Z][A-Za-z]+)\b", self.code):
            verb, full = m.group(1), m.group(0)
            if full in ALLOWED_VERB_NOUN:
                continue
            if verb in BANNED_VERBS or full in BANNED_VERBS:
                found.append(full)
        self.assertEqual(sorted(set(found)), [])

    def test_no_network_registry_writes_or_risky_tools(self):
        hits = [p for p in BANNED_ANYWHERE if re.search(p, self.code, flags=re.I)]
        self.assertEqual(hits, [])

    def test_file_writes_only_in_report_function(self):
        body = function_body(self.code, "Save-ForgeReport")
        self.assertTrue(body, "Save-ForgeReport not found")
        rest = self.code.replace(body, "")
        self.assertEqual([p for p in WRITE_APIS if re.search(p, rest)], [])
        # The report never overwrites: a new file only.
        self.assertIn("CreateNew", body)
        self.assertNotRegex(body, r"FileMode\]::(Create|OpenOrCreate|Truncate|Append)\b")

    def test_only_known_native_programs(self):
        # The only outside program it may run is manage-bde -status, by full path.
        exes = set(re.findall(r"([A-Za-z0-9_-]+)\.exe", self.code, flags=re.I))
        self.assertLessEqual({e.lower() for e in exes}, {"manage-bde", "powershell"})
        for m in re.finditer(r"manage-bde\.exe['\"]?\)?[ \t]*(.*)", self.code):
            if m.group(1).strip():
                self.assertRegex(m.group(1), r"^-status\b")
        # $exe is only ever set to manage-bde, and only run with -status.
        for m in re.finditer(r"\$exe\s*=\s*(.*)", self.code):
            self.assertIn("manage-bde.exe", m.group(1))
        for m in re.finditer(r"&\s*\$exe\b(.*)", self.code):
            self.assertRegex(m.group(1), r"^\s+-status\b")

    def test_tool_readme(self):
        text = read(TOOL_README)
        self.assertIn("powershell -ExecutionPolicy Bypass -File .\\forge-precheck.ps1", text)
        self.assertIn("-Json", text)
        self.assertIn("-SaveReport", text)
        self.assertNotIn("\u2014", text)
        prose = re.sub(r"(?m)^\|[-|: ]+\|$", "", text)  # markdown table rulers are fine
        self.assertNotIn("--", prose)

    def test_main_readme_step4_points_at_checker(self):
        text = read(MAIN_README)
        step4 = text.split("## Step 4: Prepare Windows", 1)[1].split("\n## ", 1)[0]
        self.assertIn("tools/windows/forge-precheck.ps1", step4)
        self.assertIn("powershell -ExecutionPolicy Bypass -File .\\forge-precheck.ps1", step4)


@unittest.skipUnless(PWSH, "pwsh not installed (set FORGE_PWSH to run the PowerShell tests)")
class PowerShellTest(unittest.TestCase):
    """Runs tests/pwsh/forge-precheck.tests.ps1: every check with fake probe data (Tested simulated)."""

    def test_pwsh_suite(self):
        env = dict(os.environ, POWERSHELL_TELEMETRY_OPTOUT="1", POWERSHELL_UPDATECHECK="Off")
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", PS_TESTS, SCRIPT],
                           capture_output=True, text=True, timeout=600, env=env)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("ALL PASSED", out)


if __name__ == "__main__":
    unittest.main()
