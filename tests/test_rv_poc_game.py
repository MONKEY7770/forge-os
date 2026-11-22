"""rvGame PoC F2, kept as a regression test: a failed switch back is retried until it works.
(The PoC "any own process named sober counts as a game" is by design: see NOTES.md.)"""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_game import Base, load_tool  # noqa: E402


class PocStuck(Base):
    def test_failed_switch_back_is_forgotten_and_never_retried(self):
        self.set_mode("agents")
        game = load_tool()
        w = game.Watcher(proc=self.proc)
        self.steam_game()
        w.tick()                                  # root step works: agents.slice now has gaming limits
        self.assertEqual(self.mode(), "gaming")
        os.environ["FAKE_SYSTEMCTL_RC"] = "1"     # e.g. session not active (user switched), polkit says no
        self.end_game()
        for _ in range(game.EXIT_POLLS):
            w.tick()
        os.environ["FAKE_SYSTEMCTL_RC"] = "0"     # session active again
        self.clear_calls()
        for _ in range(5):
            w.tick()
        game.restore(force=True, proc=self.proc)   # ExecStopPost / next login
        print("mode file:", self.mode(), "state kept:",
              os.path.exists(os.path.join(self.state_dir(), "game-mode.json")),
              "retries:", [c for c in self.calls() if "forge-mode-apply@" in c], file=sys.stderr)
        self.assertIn("mode-switch agents", self.calls(),
                      "agents.slice keeps the gaming limits while every file says 'agents'")


if __name__ == "__main__":
    unittest.main()
