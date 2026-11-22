"""rvGame PoC: a read-only folder in the backup (like Go's module cache, dirs 0555) makes the
restore test leave its restored copy behind in the runtime folder, run after run."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_restore_test import RealResticRestoreTest  # noqa: E402


class PocReadOnlyDir(RealResticRestoreTest):
    def test_readonly_dir_leaves_leftovers(self):
        d = os.path.join(self.home, "go/pkg/mod/example.com/m@v1")
        os.makedirs(d)
        for i in range(30):
            with open(os.path.join(d, f"f{i}.go"), "w") as f:
                f.write("package m\n" * (i + 1))
            os.chmod(os.path.join(d, f"f{i}.go"), 0o444)
        for p in (d, os.path.dirname(d)):
            os.chmod(p, 0o555)
        self.backed_up()
        for n in range(3):
            r = self.run_tool("restore-test", timeout=180)
            print("run", n, r.returncode, self.rt().get("result"), "leftovers:", self.leftovers(), file=sys.stderr)
        for p in (d, os.path.dirname(d)):
            os.chmod(p, 0o755)
        self.assertEqual(self.leftovers(), [])

    def tearDown(self):
        for root, dirs, _ in os.walk(self.t):
            for x in dirs:
                try:
                    os.chmod(os.path.join(root, x), 0o755)
                except OSError:
                    pass
        super().tearDown()


if __name__ == "__main__":
    unittest.main()
