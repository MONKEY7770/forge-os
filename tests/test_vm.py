"""forge-vm windows-tune: the standard Windows performance settings for a VM made in Virtual
Machine Manager (the Flatpak, your own user's VMs), with a backup that `forge-vm undo` restores."""
import importlib.machinery
import importlib.util
import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "files/system/usr/bin/forge-vm")

WIN11 = """<domain type="kvm">
  <name>win11</name>
  <metadata>
    <libosinfo:libosinfo xmlns:libosinfo="http://libosinfo.org/xmlns/libvirt/domain/1.0">
      <libosinfo:os id="http://microsoft.com/win/11"/>
    </libosinfo:libosinfo>
  </metadata>
  <memory unit="KiB">8388608</memory>
  <vcpu placement="static">8</vcpu>
  <os firmware="efi"><type arch="x86_64" machine="q35">hvm</type></os>
  <features>
    <acpi/><apic/>
    <hyperv mode="custom"><relaxed state="on"/><vapic state="on"/><spinlocks state="on" retries="8191"/></hyperv>
    <vmport state="off"/>
  </features>
  <cpu mode="host-passthrough" check="none" migratable="on"/>
  <clock offset="localtime">
    <timer name="rtc" tickpolicy="catchup"/><timer name="pit" tickpolicy="delay"/>
    <timer name="hpet" present="no"/><timer name="hypervclock" present="yes"/>
  </clock>
  <devices>
    <disk type="file" device="disk"><driver name="qemu" type="qcow2"/><target dev="sda" bus="sata"/></disk>
    <tpm model="tpm-crb"><backend type="emulator" version="2.0"/></tpm>
    <memballoon model="virtio"/>
  </devices>
</domain>"""

LINUX = WIN11.replace("http://microsoft.com/win/11", "http://fedoraproject.org/fedora/41").replace("win11", "fedora")


def load_tool():
    loader = importlib.machinery.SourceFileLoader("forge_vm_t", TOOL)
    spec = importlib.util.spec_from_loader("forge_vm_t", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TuneXmlTest(unittest.TestCase):
    def setUp(self):
        self.v = load_tool()

    def tuned(self, xml=WIN11, smt=True):
        new, changes = self.v.tune_windows_xml(xml, smt=smt)
        return ET.fromstring(new), changes

    def test_hyper_v_enlightenments_are_all_on(self):
        d, _ = self.tuned()
        hv = d.find("features/hyperv")
        for name in ("relaxed", "vapic", "spinlocks", "vpindex", "runtime", "synic", "stimer", "reset",
                     "frequencies", "reenlightenment", "tlbflush", "ipi"):
            self.assertEqual(hv.find(name).get("state"), "on", name)
        self.assertEqual(hv.find("spinlocks").get("retries"), "8191")
        self.assertEqual(hv.find("stimer/direct").get("state"), "on")
        self.assertEqual(len(hv.findall("relaxed")), 1)      # not added twice

    def test_cpu_is_the_host_cpu_with_a_matching_topology(self):
        d, _ = self.tuned()
        cpu = d.find("cpu")
        self.assertEqual(cpu.get("mode"), "host-passthrough")
        topo = cpu.find("topology")
        self.assertEqual((topo.get("sockets"), topo.get("cores"), topo.get("threads")), ("1", "4", "2"))
        self.assertEqual(cpu.find("cache").get("mode"), "passthrough")
        d, _ = self.tuned(smt=False)
        topo = d.find("cpu/topology")
        self.assertEqual((topo.get("cores"), topo.get("threads")), ("8", "1"))

    def test_odd_cpu_counts_use_one_thread_per_core(self):
        d, _ = self.tuned(WIN11.replace(">8</vcpu>", ">6</vcpu>").replace(">6</vcpu>", ">5</vcpu>"))
        topo = d.find("cpu/topology")
        self.assertEqual((topo.get("cores"), topo.get("threads")), ("5", "1"))

    def test_windows_clocks_and_no_memory_balloon(self):
        d, _ = self.tuned()
        timers = {t.get("name"): t.attrib for t in d.findall("clock/timer")}
        self.assertEqual(timers["hypervclock"]["present"], "yes")
        self.assertEqual(timers["hpet"]["present"], "no")
        self.assertEqual(timers["rtc"]["tickpolicy"], "catchup")
        self.assertEqual(d.find("devices/memballoon").get("model"), "none")

    def test_disks_networks_and_tpm_are_never_changed(self):
        d, _ = self.tuned()
        self.assertEqual(d.find("devices/disk/target").get("bus"), "sata")   # changing it can stop Windows booting
        self.assertIsNotNone(d.find("devices/tpm"))

    def test_changes_are_listed_and_a_tuned_vm_needs_nothing_more(self):
        once, changes = self.v.tune_windows_xml(WIN11, smt=True)
        self.assertTrue(changes)
        again, changes2 = self.v.tune_windows_xml(once, smt=True)
        self.assertEqual(changes2, [])

    def test_xml_with_declarations_is_refused(self):
        bad = '<!DOCTYPE d [<!ENTITY x "y">]>' + WIN11
        with self.assertRaisesRegex(ValueError, "unexpected XML"):
            self.v.tune_windows_xml(bad, smt=True)

    def test_not_windows_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not a Windows VM"):
            self.v.tune_windows_xml(LINUX, smt=True)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.v = load_tool()
        self.v.BACKUP_DIR = os.path.join(self.t, "backups")
        self.v.smt_active = lambda: True
        self.defined = []
        self.xml = WIN11

        self.dumps = []
        self.only_on = "qemu:///session"

        def fake(args, input_text=None, uri=None):
            if uri != self.only_on:
                return 1, "error: failed to get domain"
            if args[:1] == ["dumpxml"]:
                self.dumps.append(args)
                return 0, self.xml
            if args[:1] == ["define"]:
                self.assertEqual(args, ["define", "/dev/stdin"])     # the sandbox can't see host files
                self.defined.append(input_text)
                return 0, ""
            if args[:2] == ["domstate", "win11"]:
                return 0, "shut off\n"
            return 1, "unknown"
        self.v.virsh = fake

    def test_tune_saves_a_backup_then_defines_the_new_vm(self):
        self.assertEqual(self.v.main(["windows-tune", "win11"]), 0)
        self.assertEqual(len(self.defined), 1)
        self.assertIn("synic", self.defined[0])
        backups = os.listdir(self.v.BACKUP_DIR)
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.v.BACKUP_DIR, backups[0])) as f:
            self.assertEqual(f.read(), WIN11)

    def test_saved_settings_with_passwords_are_read(self):
        self.v.main(["windows-tune", "win11"])
        self.assertEqual(self.dumps[0], ["dumpxml", "--inactive", "--security-info", "win11"])

    def test_vms_of_the_system_connection_are_found_too(self):
        self.only_on = "qemu:///system"
        self.assertEqual(self.v.main(["windows-tune", "win11"]), 0)
        self.assertEqual(len(self.defined), 1)

    def test_dry_run_changes_nothing(self):
        self.assertEqual(self.v.main(["windows-tune", "win11", "--dry-run"]), 0)
        self.assertEqual(self.defined, [])
        self.assertFalse(os.path.exists(self.v.BACKUP_DIR))

    def test_undo_puts_back_the_last_backup(self):
        self.v.main(["windows-tune", "win11"])
        self.defined.clear()
        self.assertEqual(self.v.main(["undo", "win11"]), 0)
        self.assertEqual(self.defined, [WIN11])

    def test_odd_names_are_refused(self):
        for bad in ("../x", "-c", "a b", ""):
            self.assertEqual(self.v.main(["windows-tune", bad]), 2, bad)
        self.assertEqual(self.defined, [])

    def test_the_virsh_used_is_the_one_inside_virtual_machine_manager(self):
        self.assertEqual(self.v.VIRSH, ["flatpak", "run", "--command=virsh", "org.virt_manager.virt-manager"])
        self.assertEqual(self.v.URIS, ("qemu:///session", "qemu:///system"))


if __name__ == "__main__":
    unittest.main()
