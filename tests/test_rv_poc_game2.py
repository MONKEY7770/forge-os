"""rvGame PoC round 2: a user the root service always refuses (not in wheel, or not the seat0 user)
gets gaming mode on game start, and never gets out of it: power profile and mode stay 'gaming'."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_game import Base, load_tool  # noqa: E402


class PocNeverBack(Base):
    def test_refused_user_stays_in_gaming_forever(self):
        self.set_mode("agents")
        os.environ["FAKE_SYSTEMCTL_RC"] = "1"      # service answers "error: only administrators ..." every time
        game = load_tool()
        w = game.Watcher(proc=self.proc)
        self.steam_game()
        w.tick()
        self.end_game()
        for _ in range(game.EXIT_POLLS * 40):      # 10 minutes of looks
            w.tick()
        game.restore(force=True, proc=self.proc)   # logout
        with open(self.power) as f:
            power = f.read().strip()
        print("mode:", self.mode(), "power:", power, "switch attempts:",
              sum(c.startswith("mode-switch ") for c in self.calls()), file=sys.stderr)
        self.assertNotEqual(self.mode(), "gaming", "the agent limits were never changed, yet it stays gaming")
        self.assertEqual(power, "balanced")


if __name__ == "__main__":
    unittest.main()
