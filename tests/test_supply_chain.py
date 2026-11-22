"""Supply chain checks (R14): pinned downloads and pinned GitHub Actions."""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLLAMA_SCRIPT = os.path.join(ROOT, "files/scripts/install-ollama.sh")
WORKFLOWS = sorted(glob.glob(os.path.join(ROOT, ".github/workflows/*.yml")))

# Actions that may stay on a tag for now, with the reason. Keep this list short.
# (Empty: the second review found the signing job's blue-build action on a tag, so it is pinned now.)
TAG_ALLOWLIST = {}

SHA_PIN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^'\"\s#]+)", re.M)
ACTIONS_DIR = os.path.join(ROOT, ".github/actions")


def read(path):
    with open(path) as f:
        return f.read()


def local_action_file(ref):
    """The action.yml of a local action ("./.github/actions/<name>"), or None."""
    d = os.path.normpath(os.path.join(ROOT, ref))
    if not d.startswith(ACTIONS_DIR + os.sep):
        return None
    for name in ("action.yml", "action.yaml"):
        if os.path.isfile(os.path.join(d, name)):
            return os.path.join(d, name)
    return None


def unpinned_refs(path, seen=None):
    """Every "uses:" in a workflow or action file, and in the local actions it uses, that is not
    pinned to a commit SHA (or allowlisted). A local action counts as pinned only when everything
    inside it is."""
    seen = set() if seen is None else seen
    if path in seen:
        return []
    seen.add(path)
    bad = []
    for ref in USES.findall(read(path)):
        if ref in TAG_ALLOWLIST:
            continue
        if ref.startswith("./"):
            inner = local_action_file(ref)
            if inner is None:
                bad.append(f"{os.path.relpath(path, ROOT)}: {ref} (no action.yml under .github/actions)")
            else:
                bad += unpinned_refs(inner, seen)
        elif not SHA_PIN.match(ref):
            bad.append(f"{os.path.relpath(path, ROOT)}: {ref}")
    return bad


class OllamaScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = read(OLLAMA_SCRIPT)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_verify(self, script, target, **env):
        return subprocess.run(["bash", script], capture_output=True, text=True,
                              env=dict(os.environ, VERIFY_ONLY=target, **env), timeout=30)

    def fake_file(self):
        path = os.path.join(self.tmp, "ollama-linux-amd64.tar.zst")
        with open(path, "wb") as f:
            f.write(b"not the real ollama")
        return path, hashlib.sha256(b"not the real ollama").hexdigest()

    def test_r14_version_and_checksum_pinned(self):
        self.assertRegex(self.text, r'(?m)^OLLAMA_VERSION="\d+\.\d+\.\d+"$')
        self.assertRegex(self.text, r'(?m)^OLLAMA_SHA256="[0-9a-f]{64}"$')
        self.assertIn("sha256sum -c", self.text)
        self.assertIn("https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/", self.text)

    def test_r14_no_unverified_fallback(self):
        self.assertNotIn("ollama.com/download", self.text)
        self.assertNotIn(".tgz", self.text)
        self.assertEqual(self.text.count("curl "), 1)

    def test_r14_wrong_checksum_fails(self):
        path, real_hash = self.fake_file()
        r = self.run_verify(OLLAMA_SCRIPT, path)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        # The pinned value can't be swapped from outside the script.
        r = self.run_verify(OLLAMA_SCRIPT, path, OLLAMA_SHA256=real_hash)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_r14_right_checksum_passes(self):
        path, real_hash = self.fake_file()
        copy = os.path.join(self.tmp, "install-ollama.sh")
        with open(copy, "w") as f:
            f.write(re.sub(r'(?m)^OLLAMA_SHA256="[0-9a-f]{64}"$', f'OLLAMA_SHA256="{real_hash}"', self.text))
        r = self.run_verify(copy, path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_r14_missing_file_fails(self):
        r = self.run_verify(OLLAMA_SCRIPT, os.path.join(self.tmp, "nothing-here"))
        self.assertNotEqual(r.returncode, 0)


class WorkflowPinTests(unittest.TestCase):
    def test_r14_every_action_pinned_or_allowlisted(self):
        self.assertTrue(WORKFLOWS)
        seen = set()
        for wf in WORKFLOWS:
            seen.update(USES.findall(read(wf)))
            self.assertEqual(unpinned_refs(wf), [], "not pinned to a commit SHA")
        # Every allowlist entry must still be in use, so the list can't go stale.
        for ref in TAG_ALLOWLIST:
            self.assertIn(ref, seen, f"{ref} is allowlisted but no longer used")

    def test_r14_local_actions_pin_everything_inside(self):
        # A local composite action could hide an unpinned action. Every action.yml under
        # .github/actions is checked, used or not.
        files = glob.glob(os.path.join(ACTIONS_DIR, "**", "action.y*ml"), recursive=True)
        for f in files:
            self.assertEqual(unpinned_refs(f), [], f)

    def test_r14_iso_installer_pinned(self):
        text = read(os.path.join(ROOT, ".github/workflows/build-iso.yml"))
        # The builder image is pinned by digest and run directly (the archived action can't be pinned:
        # it pulls its image by the action's own ref).
        self.assertIn("ghcr.io/jasonn3/build-container-installer@sha256:354e6d7fd4cb1850fb5adcfc3535f2c271dcecbc127730a058c0cbedbd9d3f51" , text)
        self.assertNotIn("uses: jasonn3/", text)
        self.assertNotIn("@main", text)


PIN_SCRIPT = os.path.join(ROOT, "files/scripts/pin-repo-keys.sh")
RECIPE_COMMON = os.path.join(ROOT, "recipes/common-modules.yml")
REPO_DIR = os.path.join(ROOT, "files/system/etc/yum.repos.d")
DATA = os.path.join(ROOT, "tests/data")


class RepoKeyPinTests(unittest.TestCase):
    """Extra dnf repos: keys pinned by fingerprint, repo files kept in this repo."""

    def test_recipe_downloads_no_repo_files(self):
        text = open(RECIPE_COMMON).read()
        self.assertNotRegex(text, r"https?://\S+\.repo", "a .repo file is still downloaded at build time")

    def test_key_check_runs_before_any_package_install(self):
        text = open(RECIPE_COMMON).read()
        pin = text.find("pin-repo-keys.sh")
        dnf = text.find("type: dnf")
        self.assertNotEqual(pin, -1, "pin-repo-keys.sh is not in the recipe")
        self.assertLess(pin, dnf, "the key check must run before the dnf module")

    def test_repo_files_use_checked_local_keys(self):
        files = sorted(glob.glob(os.path.join(REPO_DIR, "forge-*.repo")))
        self.assertTrue(files, "no forge-*.repo files")
        script = open(PIN_SCRIPT).read()
        for f in files:
            text = open(f).read()
            name = os.path.basename(f)[len("forge-"):-len(".repo")]
            self.assertIn("gpgcheck=1", text, f)
            self.assertIn("repo_gpgcheck=1", text, f)
            self.assertIn(f"gpgkey=file:///etc/pki/rpm-gpg/forge-{name}.gpg", text, f)
            self.assertNotRegex(text, r"gpgkey=https?://", f)
            self.assertIn(f'"{name}|https://', script, f"{name} has no pinned key")

    def run_pin(self, keyfile, name="claude-desktop"):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        src = os.path.join(tmp, "src")
        os.makedirs(src)
        if keyfile:
            shutil.copy(keyfile, os.path.join(src, name + ".key"))
        env = dict(os.environ, FORGE_KEY_DIR=os.path.join(tmp, "out"), FORGE_KEY_TEST_DIR=src)
        r = subprocess.run(["bash", PIN_SCRIPT, name], env=env, capture_output=True, text=True)
        return r, os.path.join(tmp, "out", f"forge-{name}.gpg")

    def throwaway_key(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        home = os.path.join(tmp, "g")
        os.makedirs(home, mode=0o700)
        env = dict(os.environ, GNUPGHOME=home)
        subprocess.run(["gpg", "--batch", "--passphrase", "", "--quick-gen-key", "Fake <f@example.com>",
                        "rsa2048", "sign", "never"], env=env, check=True, capture_output=True)
        out = os.path.join(tmp, "fake.asc")
        with open(out, "w") as f:
            subprocess.run(["gpg", "--armor", "--export"], env=env, check=True, stdout=f)
        return out

    @unittest.skipUnless(shutil.which("gpg"), "gpg not installed")
    def test_pinned_key_accepted(self):
        r, out = self.run_pin(os.path.join(DATA, "claude-desktop.key"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(out))

    @unittest.skipUnless(shutil.which("gpg"), "gpg not installed")
    def test_other_key_refused(self):
        r, out = self.run_pin(self.throwaway_key())
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(out))

    @unittest.skipUnless(shutil.which("gpg"), "gpg not installed")
    def test_extra_key_next_to_the_pinned_one_refused(self):
        both = os.path.join(tempfile.mkdtemp(), "both.asc")
        with open(both, "w") as f:
            f.write(open(os.path.join(DATA, "claude-desktop.key")).read())
            f.write(open(self.throwaway_key()).read())
        r, out = self.run_pin(both)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(out))

    def test_missing_or_garbage_key_refused(self):
        r, out = self.run_pin(None)
        self.assertNotEqual(r.returncode, 0)
        junk = os.path.join(tempfile.mkdtemp(), "junk")
        with open(junk, "w") as f:
            f.write("<html>not a key</html>")
        r, out = self.run_pin(junk)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(out))


class WorkflowHardeningTests(unittest.TestCase):
    def wf(self, name):
        return open(os.path.join(ROOT, ".github/workflows", name)).read()

    def test_bluebuild_cli_is_verified_and_pinned(self):
        text = self.wf("build.yml")
        self.assertRegex(text, r"verify_install:\s*true")
        self.assertRegex(text, r"cli_version:\s*v\d+\.\d+\.\d+\b")

    def test_both_images_are_built(self):
        text = self.wf("build.yml")
        self.assertIn("- recipe.yml", text)
        self.assertIn("- recipe-universal.yml", text)

    def test_secure_boot_key_url_pinned_to_a_commit(self):
        wf = yaml.safe_load(self.wf("build-iso.yml"))
        build = [s for s in wf["jobs"]["iso"]["steps"] if s.get("id") == "build"]
        self.assertEqual(len(build), 1)
        url = build[0]["env"]["SECURE_BOOT_KEY_URL"]
        self.assertRegex(url, r"^https://github\.com/ublue-os/akmods/raw/[0-9a-f]{40}/certs/public_key\.der$")
        # And the builder really gets it (a dropped argument once left the key out of the ISO).
        self.assertIn('SECURE_BOOT_KEY_URL="$SECURE_BOOT_KEY_URL"', build[0]["run"])
        self.assertIn('ENROLLMENT_PASSWORD="universalblue"', build[0]["run"])


class CiGateTests(unittest.TestCase):
    def test_finalize_chmods_every_tool_in_one_command(self):
        # A lost "\" once turned the rest of the list into a command (running forge-apply as
        # root during the image build). Every logical line must be a real command, and every
        # tool must be made executable.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "files/scripts/finalize.sh")) as f:
            text = f.read().replace("\\\n", " ")
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
        for line in lines:
            self.assertFalse(line.startswith("/"), "a path runs as a command: " + line)
        chmod = [l for l in lines if l.startswith("chmod 0755 ")]
        self.assertEqual(len(chmod), 1)
        listed = set(chmod[0].split()[2:])
        for d in ("usr/bin", "usr/libexec"):
            for name in os.listdir(os.path.join(root, "files/system", d)):
                full = os.path.join(root, "files/system", d, name)
                if name.startswith("forge") and os.path.isfile(full):
                    self.assertIn("/" + d + "/" + name, listed)

    def test_no_merge_conflict_markers_anywhere(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        marks = ("<" * 7 + " ", ">" * 7 + " ")
        bad = []
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in (".git", "__pycache__")]
            for name in files:
                try:
                    with open(os.path.join(d, name), encoding="utf-8") as f:
                        if any(line.startswith(marks) for line in f):
                            bad.append(os.path.relpath(os.path.join(d, name), root))
                except (UnicodeDecodeError, OSError):
                    continue
        self.assertEqual(bad, [])

    def test_tests_do_not_depend_on_one_machine_s_folders(self):
        # GitHub checks the repo out somewhere else, so a fixed path breaks the CI gate there.
        here = os.path.dirname(os.path.abspath(__file__))
        needle = "/home/" + "claude"      # split so this file doesn't match itself
        bad = []
        for name in sorted(os.listdir(here)):
            if name.endswith(".py"):
                with open(os.path.join(here, name)) as f:
                    if needle in f.read():
                        bad.append(name)
        self.assertEqual(bad, [])

    """P0 step 1: nothing is built or published unless the checks pass."""

    def wf(self, name):
        return open(os.path.join(ROOT, ".github/workflows", name)).read()

    def job(self, text, name):
        m = re.search(rf"^  {name}:\n(.*?)(?=^  \S|\Z)", text, re.S | re.M)
        self.assertTrue(m, f"no job {name}")
        return m.group(1)

    def test_checks_job_runs_every_gate(self):
        checks = self.job(self.wf("build.yml"), "checks")
        for needle in ("unittest discover", "bandit", "shellcheck", "detect-secrets", "actionlint .github/workflows/"):
            self.assertIn(needle, checks, needle)
        # actionlint: one exact release, accepted only with its sha256 (no unpinned download).
        url = re.search(r"https://github\.com/rhysd/actionlint/releases/download/v(\d+\.\d+\.\d+)/actionlint_\1_linux_amd64\.tar\.gz", checks)
        self.assertTrue(url, "actionlint must come from one pinned release")
        self.assertRegex(checks, r'echo "[0-9a-f]{64}  /tmp/actionlint\.tar\.gz" \| sha256sum -c -')
        self.assertLess(checks.find("sha256sum -c"), checks.find("tar -xzf /tmp/actionlint"))
        base = self.job(self.wf("build.yml"), "base-images")
        # Bazzite's base images: pinned to its own build workflow's keyless identity and GitHub's issuer.
        self.assertIn("cosign verify --certificate-oidc-issuer https://token.actions.githubusercontent.com", base)
        self.assertIn("--certificate-identity https://github.com/ublue-os/bazzite/.github/workflows/build.yml@refs/heads/main" , base)
        self.assertNotIn("certificate-identity-regexp", base)

    def test_gate_steps_are_exact_and_cannot_be_switched_off(self):
        # Third review: text matching let "actionlint || true", "if: false", "continue-on-error",
        # an echoed command or a second download after the hash check slip through.
        try:
            import yaml
        except ImportError:
            if os.environ.get("GITHUB_ACTIONS"):
                raise
            self.skipTest("PyYAML not installed")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, ".github/workflows/build.yml")) as f:
            wf = yaml.safe_load(f)
        self.assertNotIn("defaults", wf)
        job = wf["jobs"]["checks"]
        for key in ("if", "continue-on-error"):
            self.assertNotIn(key, job)
        self.assertEqual(job.get("defaults"), {"run": {"shell": "bash"}})
        exact = {
            "Unit tests": "python3 -m unittest discover -s tests",
            "Python security lint (fails on High findings)":
                "mapfile -t files < <(grep -rlE '^#!.*python' files/system/usr/bin files/system/usr/libexec; "
                "find files -name '*.py')\nbandit -q -lll \"${files[@]}\"",
            "Shell script checks": "grep -rlE '^#!.*(bash|/sh)' files .github | xargs shellcheck -S warning",
            "Workflow checks (actionlint, with shellcheck on every run script)": "actionlint .github/workflows/*.yml",
            "Secret scan (fails on anything not reviewed in .secrets.baseline)":
                'git config --global --add safe.directory "$PWD"\n'
                "git ls-files -z | xargs -0 detect-secrets-hook --baseline .secrets.baseline",
        }
        seen = set()
        for step in job["steps"]:
            for key in ("if", "continue-on-error", "shell", "working-directory"):
                self.assertNotIn(key, step, step.get("name"))
            self.assertNotIn("PATH", step.get("env") or {}, step.get("name"))
            name = step.get("name")
            if name in exact:
                seen.add(name)
                self.assertEqual(step["run"].strip(), exact[name], name)
        self.assertEqual(seen, set(exact))
        [install] = [st for st in job["steps"] if st.get("name") == "Install test tools"]
        joined = install["run"].replace("\\\n", " ")       # one logical command per line
        lines = [l.strip() for l in joined.splitlines() if l.strip() and not l.strip().startswith("#")]
        self.assertFalse([l for l in lines if "||" in l or l.endswith("; true") or "--ignore-missing" in l], lines)
        curls = [i for i, l in enumerate(lines) if l.split()[0] in ("curl", "wget")]
        self.assertEqual(len(curls), 1, "exactly one download")
        c = curls[0]
        self.assertRegex(lines[c], r"^curl -fsSL -o /tmp/actionlint\.tar\.gz https://github\.com/rhysd/actionlint/"
                                   r"releases/download/v(\d+\.\d+\.\d+)/actionlint_\1_linux_amd64\.tar\.gz$")
        self.assertRegex(lines[c + 1], r'^echo "[0-9a-f]{64}  /tmp/actionlint\.tar\.gz" \| sha256sum -c -$')
        self.assertEqual(lines[c + 2], "tar -xzf /tmp/actionlint.tar.gz -C /usr/local/bin actionlint")

    def test_image_build_waits_for_checks(self):
        build = self.job(self.wf("build.yml"), "bluebuild")
        self.assertRegex(build, r"needs:\s*\[checks, base-images\]")

    def test_published_images_are_verified_with_our_public_key(self):
        verify = self.job(self.wf("build.yml"), "verify")
        self.assertRegex(verify, r"needs:\s*\[?\s*bluebuild")
        self.assertIn("cosign verify --key cosign.pub", verify)
        self.assertNotIn("secrets.", verify, "verifying needs only the public key")

    def test_iso_build_only_from_a_verified_image(self):
        text = self.wf("build-iso.yml")
        self.assertIn("cosign verify --key cosign.pub", text)
        self.assertLess(text.find("cosign verify"), text.find("docker run --privileged"))

    def test_secret_scan_baseline_has_no_unreviewed_entries(self):
        base = json.load(open(os.path.join(ROOT, ".secrets.baseline")))
        for path, hits in base["results"].items():
            for h in hits:
                self.assertIs(h.get("is_secret"), False, f"{path}:{h['line_number']} not reviewed")


class ImageBuildTests(unittest.TestCase):
    def test_every_tool_is_made_executable_in_the_image(self):
        fin = open(os.path.join(ROOT, "files/scripts/finalize.sh")).read()
        for d in ("usr/bin", "usr/libexec"):
            for f in os.listdir(os.path.join(ROOT, "files/system", d)):
                if f != "__pycache__":
                    self.assertIn(f"/{d}/{f}", fin, f"/{d}/{f} is not chmod-ed in finalize.sh")


class WeeklyIsoTests(unittest.TestCase):
    def setUp(self):
        self.text = read(os.path.join(ROOT, ".github/workflows/build-iso.yml"))

    def test_runs_weekly_and_by_hand(self):
        self.assertIn("workflow_dispatch:", self.text)
        m = re.search(r"schedule:\s*\n\s*-\s*cron:\s*\"([^\"]+)\"", self.text)
        self.assertTrue(m, "no weekly schedule")
        fields = m.group(1).split()
        self.assertEqual(len(fields), 5)
        self.assertNotEqual(fields[4], "*", "should be one day a week")


# The workflow checks below read the YAML, so a comment or a moved line can't fool them.
# CI installs python3-pyyaml (build.yml); on GitHub a missing PyYAML is an error, not a skip.
try:
    import yaml
except ImportError:
    yaml = None
NEEDS_YAML = unittest.skipUnless(yaml is not None or os.environ.get("GITHUB_ACTIONS"), "PyYAML not installed")
UNTRUSTED_IN_SHELL = re.compile(r"\$\{\{[^}]*\b(inputs\.|github\.event\.|github\.head_ref)")
EXPECTED_CHECKS = [
    'python3 .github/scripts/iso_boot_check.py --iso "$ISO" --checksum "$ISO-CHECKSUM" --out boot-test/firmware-secure-boot-off',
    'python3 .github/scripts/iso_boot_check.py --iso "$ISO" --secure-boot --out boot-test/firmware-secure-boot-on',
    'python3 .github/scripts/iso_boot_check.py --iso "$ISO" --serial-copy --out boot-test/installer-secure-boot-off',
    'python3 .github/scripts/iso_boot_check.py --iso "$ISO" --serial-copy --secure-boot --out boot-test/installer-secure-boot-on',
]
BOOT_CHECK_RUN = re.compile(r'python3 \.github/scripts/iso_boot_check\.py(?: (?:--[a-z-]+|"\$ISO(?:-CHECKSUM)?"|[\w./-]+))+')


def load_workflow(path):
    if yaml is None:
        raise AssertionError("PyYAML is needed for the workflow checks (python3-pyyaml)")
    with open(path) as f:
        wf = yaml.safe_load(f)
    if True in wf:          # YAML 1.1 reads the key "on" as true
        wf["on"] = wf.pop(True)
    return wf


def all_steps(wf):
    for name, job in wf["jobs"].items():
        for step in job.get("steps", []):
            yield name, step


@NEEDS_YAML
class WorkflowYamlTests(unittest.TestCase):
    """Every workflow: no write-all, no untrusted text inside a shell."""

    def test_no_inputs_or_event_text_inside_run(self):
        # ${{ }} is pasted into the script before the shell runs: an input could add commands.
        for wf_path in WORKFLOWS:
            for job, step in all_steps(load_workflow(wf_path)):
                run = step.get("run", "")
                self.assertNotRegex(run, UNTRUSTED_IN_SHELL, f"{os.path.basename(wf_path)} {job}: {run!r}")

    def test_no_write_all_and_every_job_has_its_own_permissions(self):
        for wf_path in WORKFLOWS:
            wf = load_workflow(wf_path)
            top = wf.get("permissions", {})
            self.assertIsInstance(top, dict, f"{wf_path}: top-level permissions must be a list, not {top!r}")
            for name, job in wf["jobs"].items():
                perms = job.get("permissions")
                self.assertIsInstance(perms, dict, f"{os.path.basename(wf_path)} {name}: permissions {perms!r}")


@NEEDS_YAML
class IsoWorkflowTests(unittest.TestCase):
    """build-iso.yml, read as YAML."""

    def setUp(self):
        self.wf = load_workflow(os.path.join(ROOT, ".github/workflows/build-iso.yml"))
        self.jobs = self.wf["jobs"]
        self.boot = self.jobs["boot-test"]
        self.steps = self.boot["steps"]
        self.checks = [s for s in self.steps if "iso_boot_check.py" in s.get("run", "")]

    def test_fedora_version_is_a_fixed_choice(self):
        # It reaches a shell inside the installer action (with docker --privileged): no free text.
        inp = self.wf["on"]["workflow_dispatch"]["inputs"]["fedora_version"]
        self.assertEqual(inp.get("type"), "choice")
        self.assertTrue(inp.get("options"))
        for o in inp["options"]:
            self.assertRegex(str(o), r"^[0-9]{2}$")
        self.assertIn(str(inp["default"]), [str(o) for o in inp["options"]])

    def test_scheduled_runs_have_a_fedora_version(self):
        # A scheduled run has no inputs, so the version needs a fallback, and it must be a choice.
        build = [s for s in self.jobs["iso"]["steps"] if s.get("id") == "build"]
        self.assertEqual(len(build), 1)
        m = re.fullmatch(r"\$\{\{ inputs\.fedora_version \|\| '([0-9]{2})' \}\}", build[0]["env"]["VERSION"])
        self.assertTrue(m, build[0]["env"]["VERSION"])
        self.assertIn('VERSION="$VERSION"', build[0]["run"])
        options = [str(o) for o in self.wf["on"]["workflow_dispatch"]["inputs"]["fedora_version"].get("options", [])]
        self.assertIn(m.group(1), options)

    def test_only_read_permissions(self):
        self.assertNotIn("permissions", self.wf, "no top-level permissions: each job sets its own")
        self.assertEqual(self.jobs["iso"]["permissions"], {"contents": "read", "packages": "read"})
        self.assertEqual(self.boot["permissions"], {"contents": "read"})

    def test_runs_after_the_iso_build_for_both_images(self):
        self.assertIn(self.boot.get("needs"), ("iso", ["iso"]))
        self.assertEqual(self.boot["strategy"]["matrix"]["image"], ["forge-os", "forge-os-universal"])

    def test_tests_the_iso_from_this_run_only(self):
        dl = [s for s in self.steps if s.get("uses", "").startswith("actions/download-artifact@")]
        self.assertEqual(len(dl), 1)
        # No run-id, repository or github-token: only this run's artifact, with no token.
        self.assertEqual(dl[0]["with"], {"name": "${{ matrix.image }}-iso", "path": "iso"})

    def test_job_has_a_time_limit(self):
        t = self.boot.get("timeout-minutes")
        self.assertIsInstance(t, int)
        self.assertTrue(0 < t <= 360, t)

    def test_check_steps_cannot_be_swallowed(self):
        self.assertNotIn("continue-on-error", self.boot)
        self.assertTrue(self.checks)
        for s in self.checks:
            self.assertNotIn("continue-on-error", s)
            self.assertNotIn("shell", s)
            self.assertRegex(s["run"].strip(), "^" + BOOT_CHECK_RUN.pattern + "$", "one plain command, nothing after it")

    def test_check_commands_are_exactly_the_four_boots(self):
        # No extra flags (--help exits 0, --timeout 1, --accel ...): only these exact commands.
        self.assertEqual([s["run"] for s in self.checks], EXPECTED_CHECKS)
        for s in self.checks:
            self.assertLessEqual(set(s), {"name", "run", "if"}, f"{s['name']}: no env, shell or other keys")

    def test_check_steps_and_jobs_always_run(self):
        # A job-level "if: false" skips the job, and a skipped job is not a failure.
        for name in ("iso", "boot-test"):
            self.assertNotIn("if", self.jobs[name], name)
        # The first boot always runs, the others run even after a failure (not when cancelled).
        self.assertEqual([s.get("if") for s in self.checks], [None] + ["${{ !cancelled() }}"] * 3)

    def test_no_shell_defaults_or_path_changes(self):
        # defaults.run.shell or a PATH change would decide what "python3" runs.
        self.assertNotIn("defaults", self.wf)
        self.assertNotIn("env", self.wf)
        for name, job in self.jobs.items():
            self.assertNotIn("defaults", job, name)
        self.assertNotIn("env", self.jobs["iso"])
        self.assertEqual(self.boot.get("env"), {"ISO": "iso/${{ matrix.image }}.iso"})
        for name, job in self.jobs.items():
            for step in job.get("steps", []):
                keys = {str(k).upper() for k in (step.get("env") or {})}
                self.assertNotIn("PATH", keys, f"{name}: {step.get('name') or step.get('uses')}")

    def test_both_images_in_both_jobs(self):
        # No include or exclude: both ISOs are built and both are boot tested.
        for name in ("iso", "boot-test"):
            self.assertEqual(self.jobs[name]["strategy"]["matrix"], {"image": ["forge-os", "forge-os-universal"]}, name)

    def test_boots_firmware_and_installer_with_secure_boot_off_and_on(self):
        runs = [s["run"] for s in self.checks]
        firmware = [r for r in runs if "--serial-copy" not in r]
        installer = [r for r in runs if "--serial-copy" in r]
        self.assertEqual([("--secure-boot" in r) for r in firmware], [False, True], runs)
        self.assertEqual([("--secure-boot" in r) for r in installer], [False, True], runs)
        self.assertIn('--checksum "$ISO-CHECKSUM"', runs[0])
        # Every boot runs, even if an earlier one failed.
        for s in self.checks[1:]:
            self.assertEqual(s.get("if"), "${{ !cancelled() }}", s["name"])

    def test_screens_and_logs_uploaded_either_way(self):
        up = [s for s in self.steps if s.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(len(up), 1)
        self.assertEqual(up[0].get("if"), "${{ always() }}")
        self.assertEqual(up[0]["with"]["path"], "boot-test/")
        for s in self.checks:
            self.assertRegex(s["run"], r"--out boot-test/[\w-]+$")

    def test_boot_test_frees_disk_space_and_has_the_tools(self):
        # The ISO and its test copy are both on disk, like the iso job's large ISO.
        text = " ".join(s.get("run", "") for s in self.steps)
        self.assertIn("sudo rm -rf /usr/share/dotnet", text)
        for pkg in ("qemu-system-x86", "ovmf", "xorriso", "mtools"):
            self.assertIn(pkg, text)


KREMA_SCRIPT = os.path.join(ROOT, "files/scripts/install-krema.sh")


class KremaPinGateTests(unittest.TestCase):
    """install-krema.sh: the optional Krema dock is pinned by exact version and key fingerprint,
    skipped when the pins are empty, never fails the build, and leaves no repo or key behind."""

    def setUp(self):
        self.text = read(KREMA_SCRIPT)

    def test_pins_are_fixed_in_the_file_and_well_formed(self):
        ver = re.search(r'(?m)^KREMA_VERSION="([^"]*)"$', self.text)
        fpr = re.search(r'(?m)^KREMA_KEY_FPR="([^"]*)"$', self.text)
        self.assertTrue(ver and fpr)
        self.assertEqual(bool(ver.group(1)), bool(fpr.group(1)), "fill in both pins or neither")
        if ver.group(1):
            self.assertRegex(ver.group(1), r"^[0-9][0-9A-Za-z.+~^_]*-[0-9A-Za-z.+~^_]+$")
            self.assertRegex(fpr.group(1), r"^[0-9A-F]{40}$")
        self.assertNotIn("${KREMA_VERSION:-", self.text)
        self.assertNotIn("${KREMA_KEY_FPR:-", self.text)

    def test_fingerprint_is_checked_before_dnf_and_the_exact_version_is_installed(self):
        self.assertIn("gpg --batch --with-colons --show-keys", self.text)
        self.assertLess(self.text.find("--show-keys"), self.text.find("dnf -y"))
        self.assertIn('install "krema-$KREMA_VERSION"', self.text)
        self.assertIn("gpgcheck=1", self.text)
        self.assertIn("enabled=0", self.text)

    def test_never_fails_the_build_and_cleans_up(self):
        self.assertNotRegex(self.text, r"(?m)^set -[a-z]*e")
        self.assertNotRegex(self.text, r"\bexit [1-9]")
        self.assertRegex(self.text, r'trap cleanup EXIT')
        self.assertRegex(self.text, r'rm -f "\$REPO_FILE" "\$KEY_FILE"')
        self.assertIn("rm -f \"$GLOBAL_AUTOSTART\"", self.text)

    def test_empty_pins_skip_with_a_log_line(self):
        if re.search(r'(?m)^KREMA_VERSION=""$', self.text) is None:
            self.skipTest("pins are filled in")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        r = subprocess.run(["bash", KREMA_SCRIPT], env=dict(os.environ, FORGE_KREMA_ROOT=tmp),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("install-krema: skipped", r.stdout)

    def test_recipe_runs_it_after_the_packages(self):
        recipe = read(RECIPE_COMMON)
        self.assertLess(recipe.find("type: dnf"), recipe.find("- install-krema.sh"))


class GitignoreTests(unittest.TestCase):
    def test_python_caches_are_ignored(self):
        lines = read(os.path.join(ROOT, ".gitignore")).split()
        self.assertIn("__pycache__/", lines)
        self.assertIn("*.pyc", lines)


class FourthReviewWorkflowTests(unittest.TestCase):
    def test_iso_installs_enforce_image_signatures(self):
        text = open(os.path.join(ROOT, ".github/workflows/build-iso.yml")).read()
        self.assertIn('IMAGE_SIGNED="true"', text)

    def test_ci_shell_steps_stop_on_pipe_failures(self):
        text = open(os.path.join(ROOT, ".github/workflows/build.yml")).read()
        checks = re.search(r"^  checks:\n(.*?)(?=^  \S)", text, re.S | re.M).group(1)
        self.assertRegex(checks, r"defaults:\s*\n\s*run:\s*\n\s*shell:\s*bash")

    def test_build_scripts_are_executable(self):
        for f in glob.glob(os.path.join(ROOT, "files/scripts/*.sh")):
            self.assertTrue(os.access(f, os.X_OK), f)

if __name__ == "__main__":
    unittest.main()





