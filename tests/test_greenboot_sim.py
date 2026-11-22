"""Release builder: simulation of greenboot-rs 0.16.4 (Fedora 45 stable), GRUB's 08_greenboot.cfg
countdown and Forge's trial-start rule (forge-boot-health is_trial). The rollback by the legacy
greenboot_rollback_trigger at the end of a countdown follows 0.16.6 (0.16.4 never reads that trigger and
asks for manual intervention instead; GRUB then starts the other entry, which ends the same way).
Fedora's grub-boot-success.timer (a login of 2 minutes or more sets boot_success=1) is modelled too.

Based on the reviewer rvRel's model (/tmp/ws/rvRel/scratch/sim_greenboot.py). It checks that the end
state is always a green start of the default entry with no countdown left: never GRUB booting the
failed entry forever, never going back and forth between versions, Windows starts in between or not."""
import importlib.machinery
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "files/system/usr/libexec/forge-boot-health")


def load_tool():
    loader = importlib.machinery.SourceFileLoader("forge_boot_health_sim", TOOL)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Machine:
    """deps[0] is GRUB's default entry. env is GRUB's environment block (values as text, like grub2-editenv)."""

    def __init__(self, forge, broken_on, max_attempts=3):
        self.forge, self.broken_on, self.max = forge, broken_on, max_attempts
        self.deps, self.env, self.log = ["NEW", "OLD"], {}, []

    def stage_update(self, legacy=False):
        # greenboot-set-rollback-trigger at shutdown (0.16.4+: next_deployment_id and fallback).
        if legacy:
            self.env["greenboot_rollback_trigger"] = "1"
        else:
            self.env.update(greenboot_next_deployment_id="NEW", fallback="1")

    def grub(self, choice):
        default = 0
        env = self.env
        if "boot_counter" in env and env.get("boot_success") == "0":
            if env["boot_counter"] in ("0", "-1"):
                default, env["boot_counter"] = 1, "-1"
            else:
                env["boot_counter"] = str(int(env["boot_counter"]) - 1)
        env["boot_success"] = "0"
        return None if choice == "windows" else self.deps[default]

    def make_default(self, dep):
        self.deps = [dep] + [d for d in self.deps if d != dep]

    def healthcheck(self, booted):
        env = self.env
        nid = env.get("greenboot_next_deployment_id")
        if nid and nid != booted:                                   # GRUB fell back: made permanent
            self.make_default(booted)
            env.pop("greenboot_next_deployment_id", None)
            env.pop("fallback", None)
        else:
            env.pop("fallback", None)                               # kernel booted: disarm fallback
        trial = self.forge.is_trial(dict(env), {booted})
        if not (trial and booted in self.broken_on):
            env["boot_success"] = "1"
            env.pop("boot_counter", None)
            env.pop("greenboot_next_deployment_id", None)
            return "GREEN"
        c = env.get("boot_counter")
        if c is None:
            env["boot_counter"] = str(self.max)
            return "RED reboot"
        if int(c) > 0:
            return "RED reboot"
        if env.get("greenboot_next_deployment_id") or env.get("greenboot_rollback_trigger") == "1":
            self.make_default([d for d in self.deps if d != self.deps[0]][0])   # rpm-ostree rollback
            for k in ("boot_counter", "greenboot_next_deployment_id", "greenboot_rollback_trigger"):
                env.pop(k, None)
            return "RED rollback"
        return "RED manual"

    def run(self, plan, login=False):
        for choice in plan:
            booted = self.grub(choice)
            if booted is None:
                self.log.append(("windows", None))
                continue
            result = self.healthcheck(booted)
            if login and not result.endswith(("reboot", "rollback")):
                self.env["boot_success"] = "1"     # grub-boot-success.timer after 2 minutes logged in
            self.log.append((booted, result))
        return self.log


class GreenbootSimulationTests(unittest.TestCase):
    def setUp(self):
        self.forge = load_tool()

    def settled(self, m, plan_after=12):
        before = list(m.deps)
        tail = m.run(["linux"] * plan_after)[-plan_after:]
        self.assertTrue(all(r == (before[0], "GREEN") for r in tail), tail)
        self.assertEqual(m.deps, before, "no going back and forth")
        self.assertNotIn("boot_counter", m.env)
        return before[0]

    def test_failure_on_every_version_with_windows_starts_ends_green_on_the_default(self):
        # rvRel's scenario: the check fails on every version (worst case), Windows starts during the countdown.
        m = Machine(self.forge, broken_on={"NEW", "OLD"})
        m.stage_update()
        m.run(["linux", "windows", "windows", "windows", "windows", "linux"])
        self.assertEqual(self.settled(m), "OLD")

    def test_real_failure_of_the_new_version_goes_back_after_four_failed_starts(self):
        m = Machine(self.forge, broken_on={"NEW"})
        m.stage_update()
        log = m.run(["linux"] * 5)
        self.assertEqual([r for d, r in log if d == "NEW"], ["RED reboot"] * 3 + ["RED rollback"])
        self.assertEqual(self.settled(m), "OLD")

    def test_mixed_windows_starts_during_a_real_failure(self):
        for plan in (["linux", "windows", "linux", "windows", "windows", "linux", "linux"],
                     ["linux", "linux", "windows"] * 4, ["linux"] + ["windows"] * 8 + ["linux"]):
            m = Machine(self.forge, broken_on={"NEW"})
            m.stage_update()
            m.run(plan)
            self.assertEqual(self.settled(m), "OLD", plan)

    def test_logins_marking_starts_good_do_not_change_the_end(self):
        for broken in ({"NEW"}, {"NEW", "OLD"}):
            m = Machine(self.forge, broken_on=broken)
            m.stage_update()
            m.run(["linux", "windows", "linux", "windows", "windows", "linux", "linux", "linux"], login=True)
            self.assertEqual(self.settled(m), "OLD", broken)

    def test_healthy_update_stays(self):
        m = Machine(self.forge, broken_on=set())
        m.stage_update()
        self.assertEqual(m.run(["linux"]), [("NEW", "GREEN")])
        self.assertEqual(self.settled(m), "NEW")

    def test_stale_legacy_trigger_never_makes_a_normal_start_red(self):
        m = Machine(self.forge, broken_on={"NEW", "OLD"})
        m.env.update(greenboot_rollback_trigger="1", boot_success="1")
        self.assertEqual(self.settled(m), "NEW")

    def test_legacy_trigger_with_a_countdown_still_rolls_back(self):
        # greenboot 0.16.3 and older: the trigger plus a running countdown is a trial start.
        m = Machine(self.forge, broken_on={"NEW"})
        m.stage_update(legacy=True)
        m.env["boot_counter"] = "3"
        m.run(["linux"] * 5)
        self.assertEqual(self.settled(m), "OLD")


if __name__ == "__main__":
    unittest.main()
