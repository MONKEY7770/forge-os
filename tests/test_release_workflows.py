"""Release builder: workflow structure (read as YAML, in the style of test_supply_chain.py).

build.yml: version number from the verified base image, written into the image before the build;
stable "latest" only from main; a "testing" tag only from the testing branch (re-pointed by digest
after the signature check); an SBOM per image with one pinned syft release, kept as an artifact and
attached as a keyless signed attestation. screenshot-test.yml: separate, manual or after a
successful build, read-only, never part of the release."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github/workflows")
try:
    import yaml
except ImportError:
    yaml = None
NEEDS_YAML = unittest.skipUnless(yaml is not None or os.environ.get("GITHUB_ACTIONS"), "PyYAML not installed")
SHA_PIN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
UNTRUSTED_IN_SHELL = re.compile(r"\$\{\{[^}]*\b(inputs\.|github\.event\.|github\.head_ref|needs\.)")


def load(name):
    if yaml is None:
        raise AssertionError("PyYAML is needed (python3-pyyaml)")
    with open(os.path.join(WF, name)) as f:
        wf = yaml.safe_load(f)
    if True in wf:
        wf["on"] = wf.pop(True)
    return wf


def steps(job):
    return job.get("steps", [])


def index_of(job, pred):
    for i, s in enumerate(steps(job)):
        if pred(s):
            return i
    return -1


def runs(job):
    return "\n".join(s.get("run", "") for s in steps(job))


@NEEDS_YAML
class BuildWorkflowReleaseTests(unittest.TestCase):
    def setUp(self):
        self.wf = load("build.yml")
        self.jobs = self.wf["jobs"]

    def test_no_untrusted_or_job_output_text_pasted_into_shell(self):
        for wf_name in ("build.yml", "screenshot-test.yml"):
            for name, job in load(wf_name)["jobs"].items():
                for s in steps(job):
                    self.assertNotRegex(s.get("run", ""), UNTRUSTED_IN_SHELL, f"{wf_name} {name} {s.get('name')}")

    def test_fedora_comes_from_the_verified_base_image(self):
        base = self.jobs["base-images"]
        verify = index_of(base, lambda s: "--certificate-identity https://github.com/ublue-os/bazzite/.github/workflows/build.yml@refs/heads/main" in s.get("run", ""))
        plan = index_of(base, lambda s: "forge_release.py plan" in s.get("run", ""))
        self.assertTrue(0 <= verify < plan)
        self.assertIn("skopeo inspect", steps(base)[plan]["run"])
        step_id = steps(base)[plan]["id"]
        self.assertEqual(base["outputs"], {k: "${{ steps.%s.outputs.%s }}" % (step_id, k) for k in ("fedora", "built_tag")})
        self.assertEqual(base["permissions"], {"contents": "read"})

    def test_version_is_made_in_the_build_job_so_every_rerun_gets_its_own(self):
        # rvRel round 2, item 3: "Re-run failed jobs" reruns this job with a new GITHUB_RUN_ATTEMPT.
        bb = self.jobs["bluebuild"]
        self.assertEqual(bb["needs"], ["checks", "base-images"])
        checkout = index_of(bb, lambda s: s.get("uses", "").startswith("actions/checkout@"))
        plan = index_of(bb, lambda s: "forge_release.py plan" in s.get("run", ""))
        write = index_of(bb, lambda s: "forge_release.py write" in s.get("run", ""))
        action = index_of(bb, lambda s: s.get("uses", "").startswith("blue-build/github-action@"))
        self.assertTrue(0 <= checkout < plan < write < action, (checkout, plan, write, action))
        self.assertIs(steps(bb)[checkout]["with"]["persist-credentials"], False)
        p = steps(bb)[plan]
        self.assertIn('--fedora "$FEDORA"', p["run"])
        self.assertEqual(p["env"]["FEDORA"], "${{ needs.base-images.outputs.fedora }}")
        w = steps(bb)[write]
        self.assertIn("--out files/system/usr/share/forge/version.json", w["run"])
        self.assertIn('--github-env "$GITHUB_ENV"', w["run"])
        self.assertEqual(w["env"]["FORGE_VERSION_JSON"], "${{ steps.%s.outputs.version_json }}" % p["id"])
        a = steps(bb)[action]["with"]
        self.assertIs(a["skip_checkout"], True)
        self.assertIs(a["verify_install"], True)
        self.assertRegex(a["cli_version"], r"^v\d+\.\d+\.\d+$")
        for st in (p, w):
            for k in ("if", "continue-on-error"):
                self.assertNotIn(k, st)

    def test_recipes_label_the_version_and_bluebuild_never_pushes_latest(self):
        for name in ("recipe.yml", "recipe-universal.yml"):
            with open(os.path.join(ROOT, "recipes", name)) as f:
                r = yaml.safe_load(f)
            self.assertEqual(r["labels"], {"org.forgeos.version": "${FORGE_VERSION}", "org.forgeos.commit": "${GITHUB_SHA}"},
                             name)
            # rvRel round 2, item 1: with alt-tags BlueBuild pushes candidate tags only, never "latest".
            self.assertEqual(r["alt-tags"], ["candidate"], name)

    def test_this_runs_image_is_checked_before_anything_points_at_it(self):
        bb = self.jobs["bluebuild"]
        self.assertEqual(bb["concurrency"], {"group": "forge-push-${{ github.sha }}-${{ matrix.recipe }}",
                                             "cancel-in-progress": False})
        action = index_of(bb, lambda s: s.get("uses", "").startswith("blue-build/github-action@"))
        check = index_of(bb, lambda s: "forge_release.py check-label" in s.get("run", ""))
        up = index_of(bb, lambda s: s.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertTrue(0 <= action < check < up)
        st = steps(bb)[check]
        self.assertEqual(st["if"], "github.event_name != 'pull_request'")
        self.assertNotIn("continue-on-error", st)
        run = st["run"]
        order = [run.find(x) for x in ("docker login ghcr.io", "skopeo inspect", 'cosign verify --key cosign.pub "$IMAGE@$DIGEST"',
                                       "forge_release.py check-label", "> digest/digest.txt")]
        self.assertTrue(all(i >= 0 for i in order) and order == sorted(order), order)
        u = steps(bb)[up]
        self.assertEqual(u["if"], "github.event_name != 'pull_request'")
        self.assertEqual(u["with"]["path"], "digest/")
        self.assertIn("digest-", u["with"]["name"])

    def test_latest_only_from_main_by_this_runs_checked_digest(self):
        job = self.jobs["stable-tag"]
        self.assertEqual(job["if"], "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'")
        self.assertEqual(job["needs"], ["bluebuild"])
        self.assertEqual(job["permissions"], {"contents": "read", "packages": "write"})
        self.assert_uses_own_digest(job)
        text = runs(job)
        copy = text.find("skopeo copy --preserve-digests --multi-arch all")
        self.assertTrue(0 <= text.find('cosign verify --key cosign.pub "$IMAGE@$DIGEST"') < copy)
        self.assertIn('"docker://$IMAGE@$DIGEST" "docker://$IMAGE:latest"', text)
        self.assertGreater(text.rfind('cosign verify --key cosign.pub "$IMAGE:latest"'), copy)
        # No other job writes latest.
        for name, other in self.jobs.items():
            if name not in ("stable-tag", "verify"):
                self.assertNotIn(":latest", runs(other), name)
        self.assertEqual(self.jobs["verify"]["needs"], ["bluebuild", "stable-tag"])
        self.assertEqual(self.jobs["verify"]["if"], job["if"])

    def assert_uses_own_digest(self, job):
        # rvRel round 2, item 2: never a tag another run could have moved; this run's digest only.
        dl = [s for s in steps(job) if s.get("uses", "").startswith("actions/download-artifact@")]
        self.assertEqual(len(dl), 1)
        self.assertEqual(dl[0]["with"], {"name": "digest-${{ matrix.image }}", "path": "digest"})
        text = runs(job)
        self.assertIn("forge_release.py read-digest digest/digest.txt", text)
        self.assertNotIn("BUILT_TAG", str(job))
        self.assertTrue(0 <= text.find("docker login ghcr.io") < text.find("cosign verify"), "private packages")

    def test_testing_tag_only_from_the_testing_branch_and_only_after_the_signature_check(self):
        job = self.jobs["channel-tag"]
        self.assertEqual(job["if"], "github.event_name != 'pull_request' && github.ref == 'refs/heads/testing'")
        self.assertEqual(job["needs"], ["bluebuild"])
        self.assert_uses_own_digest(job)
        self.assertEqual(job["permissions"], {"contents": "read", "packages": "write"})
        self.assertEqual(job["strategy"]["matrix"]["image"], ["forge-os", "forge-os-universal"])
        text = runs(job)
        first_verify = text.find("cosign verify --key cosign.pub")
        copy = text.find("skopeo copy --preserve-digests")
        self.assertTrue(0 <= first_verify < copy, "verify before re-tagging")
        self.assertIn('"docker://$IMAGE@$DIGEST" "docker://$IMAGE:testing"', text)
        self.assertIn("skopeo copy --preserve-digests --multi-arch all", text)
        self.assertGreater(text.rfind('cosign verify --key cosign.pub "$IMAGE:testing"'), copy, "verify after")
        self.assertNotIn(":latest", text)
        self.assertNotIn("secrets.", str(job))

    def test_sbom_with_pinned_syft_artifact_and_keyless_attestation(self):
        job = self.jobs["sbom"]
        self.assertEqual(job["needs"], ["bluebuild"])
        self.assert_uses_own_digest(job)
        self.assertEqual(job["if"], "github.event_name != 'pull_request'")
        self.assertEqual(job["permissions"], {"contents": "read", "packages": "write", "id-token": "write"})
        self.assertNotIn("secrets.", str(job), "no key in a new place: keyless only")
        text = runs(job)
        lines = [l.strip() for l in text.replace("\\\n", " ").splitlines() if l.strip() and not l.strip().startswith("#")]
        dl = [i for i, l in enumerate(lines) if l.startswith(("curl", "wget"))]
        self.assertEqual(len(dl), 1, "exactly one download")
        m = re.fullmatch(r"curl -fsSL -o /tmp/syft\.tar\.gz https://github\.com/anchore/syft/releases/download/"
                         r"v(\d+\.\d+\.\d+)/syft_\1_linux_amd64\.tar\.gz", lines[dl[0]])
        self.assertTrue(m, lines[dl[0]])
        self.assertRegex(lines[dl[0] + 1], r'^echo "[0-9a-f]{64}  /tmp/syft\.tar\.gz" \| sha256sum -c -$')
        self.assertTrue(lines[dl[0] + 2].startswith("tar -xzf /tmp/syft.tar.gz"))
        self.assertLess(text.find("cosign verify --key cosign.pub"), text.find("syft scan"))
        self.assertIn('"registry:$IMAGE@$DIGEST"', text)
        upload = index_of(job, lambda s: s.get("uses", "").startswith("actions/upload-artifact@"))
        attest = index_of(job, lambda s: "cosign attest" in s.get("run", ""))
        self.assertTrue(0 <= upload < attest, "the SBOM is kept even if attaching it fails")
        self.assertIn('--type spdxjson', steps(job)[attest]["run"])
        # rvRel 6: a refused attestation (size limit) warns but does not fail the run.
        self.assertIs(steps(job)[attest]["continue-on-error"], True)
        warn = [s for s in steps(job) if "::warning::" in s.get("run", "")]
        self.assertEqual(len(warn), 1)
        self.assertEqual(warn[0]["if"], "steps.%s.outcome == 'failure'" % steps(job)[attest]["id"])
        self.assertIn('"$IMAGE@$DIGEST"', steps(job)[attest]["run"])
        self.assertIn("cosign verify-attestation --type spdxjson", text)
        self.assertIn("--certificate-oidc-issuer https://token.actions.githubusercontent.com", text)

    def test_only_build_jobs_may_write_packages(self):
        for name, job in self.jobs.items():
            if (job.get("permissions") or {}).get("packages") == "write":
                self.assertIn(name, ("bluebuild", "stable-tag", "channel-tag", "sbom"), name)

    def test_existing_gate_still_comes_first(self):
        self.assertIn("bluebuild", self.jobs["verify"]["needs"])
        for name in ("channel-tag", "sbom"):
            self.assertIn("bluebuild", self.jobs[name]["needs"], name)


@NEEDS_YAML
class IsoImageTagTests(unittest.TestCase):
    """build-iso.yml: an ISO from any image tag our build makes (latest, testing, a branch or commit
    candidate), signature checked on exactly that tag, and the installer built from its digest."""

    def setUp(self):
        self.wf = load("build-iso.yml")
        self.job = self.wf["jobs"]["iso"]

    def test_input_is_optional_text_with_latest_as_default(self):
        inp = self.wf["on"]["workflow_dispatch"]["inputs"]["image_tag"]
        self.assertEqual((inp["type"], inp["default"], inp["required"]), ("string", "latest", True))
        self.assertIn("schedule", self.wf["on"])

    def test_one_verify_of_the_chosen_tag_then_build_from_its_digest(self):
        text = runs(self.job)
        self.assertEqual(text.count("cosign verify"), 1, "a single signature check")
        pick = [s for s in steps(self.job) if "cosign verify" in s.get("run", "")]
        self.assertEqual(len(pick), 1)
        st = pick[0]
        self.assertEqual(st["env"]["TAG_INPUT"], "${{ inputs.image_tag }}")
        self.assertEqual(st["env"]["IMAGE"], "ghcr.io/monkey7770/${{ matrix.image }}")
        lines = [l.strip() for l in st["run"].splitlines() if l.strip() and not l.strip().startswith("#")]
        self.assertEqual(lines, [
            'TAG="$(python3 .github/scripts/forge_release.py iso-tag "$TAG_INPUT")"',
            'printf \'%s\' "$REGISTRY_TOKEN" | docker login ghcr.io --username "$GITHUB_ACTOR" --password-stdin',
            'cosign verify --key cosign.pub "$IMAGE:$TAG" > /tmp/verify.json',
            'DIGEST="$(python3 .github/scripts/forge_release.py digest < /tmp/verify.json)"',
            'FOLLOW="$(python3 .github/scripts/forge_release.py iso-follow-tag "$TAG")"',
            'echo "src=docker://$IMAGE@$DIGEST" >> "$GITHUB_OUTPUT"',
            'echo "tag=$FOLLOW" >> "$GITHUB_OUTPUT"',
            'echo "ISO from $IMAGE:$TAG = $DIGEST (signature checked); the installed system follows $FOLLOW"',
        ])
        for k in ("if", "continue-on-error", "shell"):
            self.assertNotIn(k, st)
        build = [s for s in steps(self.job) if "build-container-installer" in s.get("uses", "")]
        self.assertEqual(len(build), 1)
        w = build[0]["with"]
        self.assertEqual(w["image_src"], "${{ steps.%s.outputs.src }}" % st["id"])
        self.assertEqual(w["image_tag"], "${{ steps.%s.outputs.tag }}" % st["id"])
        self.assertEqual(w["enable_cache_skopeo"], "false", "never an older cached copy instead of this digest")
        self.assertLess(steps(self.job).index(st), steps(self.job).index(build[0]))
        self.assertNotIn("inputs.image_tag", str(build[0]))

    def test_the_boot_test_still_uses_this_runs_iso(self):
        boot = self.wf["jobs"]["boot-test"]
        dl = [s for s in steps(boot) if s.get("uses", "").startswith("actions/download-artifact@")]
        self.assertEqual(dl[0]["with"], {"name": "${{ matrix.image }}-iso", "path": "iso"})

    def test_readme_says_how(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            text = f.read()
        self.assertIn("-candidate-45", text)
        self.assertIn("Image tag", text)


@NEEDS_YAML
class ScreenshotWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.wf = load("screenshot-test.yml")
        self.job = self.wf["jobs"]["screenshots"]

    def test_runs_by_hand_or_after_a_successful_build_only(self):
        on = self.wf["on"]
        self.assertEqual(set(on), {"workflow_dispatch", "workflow_run"})
        self.assertEqual(on["workflow_run"]["workflows"], ["bluebuild"])
        self.assertEqual(on["workflow_run"]["types"], ["completed"])
        ch = on["workflow_dispatch"]["inputs"]["channel"]
        self.assertEqual((ch["type"], ch["options"], ch["default"]), ("choice", ["stable", "testing"], "stable"))
        cond = self.job["if"]
        for needle in ("github.event.workflow_run.conclusion == 'success'",
                       "github.event.workflow_run.event != 'pull_request'"):
            self.assertIn(needle, cond)

    def test_read_only_no_secrets_time_limit(self):
        self.assertEqual(self.job["permissions"], {"contents": "read", "packages": "read"})
        self.assertNotIn("permissions", self.wf)
        self.assertNotIn("secrets.", str(self.wf))
        t = self.job.get("timeout-minutes")
        self.assertTrue(isinstance(t, int) and 0 < t <= 180, t)

    def test_never_blocks_the_release(self):
        build = load("build.yml")
        for job in build["jobs"].values():
            needs = job.get("needs") or []
            self.assertNotIn("screenshots", needs if isinstance(needs, list) else [needs])
        self.assertNotIn("screenshot", str(build["on"]))

    def test_image_signature_is_checked_before_it_is_used(self):
        text = runs(self.job)
        self.assertTrue(0 <= text.find("docker login ghcr.io") < text.find("cosign verify"), "a private package needs a login")
        self.assertTrue(0 <= text.find("sudo podman login ghcr.io") < text.find("podman build"))
        verify = text.find("cosign verify --key cosign.pub")
        self.assertGreaterEqual(verify, 0)
        self.assertLess(verify, text.find("podman build"))
        self.assertIn('--build-arg "BASE=$IMAGE@$DIGEST"', text)
        for sub in ("pick-tag", "install", "boot"):
            self.assertIn(f"forge_screenshot.py {sub}", text)

    def test_untrusted_values_only_through_env(self):
        pick = [s for s in steps(self.job) if "pick-tag" in s.get("run", "")][0]
        self.assertEqual(pick["env"]["RUN_BRANCH"], "${{ github.event.workflow_run.head_branch }}")
        self.assertEqual(pick["env"]["CHANNEL_INPUT"], "${{ inputs.channel }}")

    def test_screens_uploaded_pass_or_fail(self):
        up = [s for s in steps(self.job) if s.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(len(up), 1)
        self.assertEqual(up[0]["if"], "${{ always() }}")
        self.assertEqual(up[0]["with"]["path"], "screenshots/")

    def test_every_action_pinned(self):
        for s in steps(self.job):
            if "uses" in s:
                self.assertRegex(s["uses"], SHA_PIN)


if __name__ == "__main__":
    unittest.main()
