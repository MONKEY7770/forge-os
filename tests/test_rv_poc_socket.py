"""rvGame PoC round 2: silent connections queue up in the single-threaded service (2 s each, not rate
limited), so a legitimate request waits 2 s per held connection."""
import os, socket, sys, time, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_game as G  # noqa: E402


class PocHold(G.ModeSwitchServerTest):
    def test_many_silent_clients_delay_a_real_request(self):
        self.serve()
        held = []
        for _ in range(12):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(self.path)
            held.append(s)
        t = time.monotonic()
        reply = self.ask("gaming\n")
        took = time.monotonic() - t
        print(f"reply={reply!r} after {took:.1f}s with {len(held)} silent connections", file=sys.stderr)
        for s in held:
            s.close()
        self.assertLess(took, 10)


if __name__ == "__main__":
    unittest.main()
