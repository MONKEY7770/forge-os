"""Secrets in free text (logs, command lines, agent output) that policy.clean_details must hide.

Found by the rvGame review of the Dashboard log viewer. The fix is in forgecore/policy.py itself, so the
audit log, approvals, the timeline and the log viewer all get it. Ordinary text must stay as it is."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "files/system/usr/lib/forge"))
from forgecore import policy  # noqa: E402

S = "Zq8vFAKEs3cr3t"   # obvious fake secret

LEAKS = [
    # A secret pair inside the value of an ordinary pair ("failed" = "password=...")
    f"login failed: password={S}", f"error: password: {S}", f'error: password="{S}"', f"failed: password='{S}'",
    f'failed: password="{S} with space"', f"a=b=password={S}", f"x: y: token: {S}", f"note=password:{S}",
    # Short and other names
    f"pw={S}", f"PW: {S}", f"psk={S}", f"PSK: {S}", f"wpa_psk={S}", f"pin={S}", f"PIN: {S}",
    # Command lines
    f"curl -u user:{S} https://example.org", f"curl --user alice:{S} -s https://x", f"mysql -u root -p{S} db",
    f"mysqldump -p{S} --all-databases", f"sshpass -p {S} ssh host", f"sshpass -p{S} ssh host",
    # Separators and brackets
    f"password\t{S}", f"password=<{S}>", f"password=({S})", f"password：{S}", f"password＝{S}",
    f"ｐａｓｓｗｏｒｄ={S}",
    # Names in other languages
    f"passwort={S}", f"Kennwort: {S}", f"contraseña={S}", f"mot_de_passe={S}", f"pässword={S}",
    f"senha={S}", f"wachtwoord={S}", f"пароль={S}", f"密码={S}",
]

KEEP = [
    "Backup saved, but some files could not be read.",
    "llama runner: offloading 33/33 layers to GPU, VRAM 7.1 GiB",
    "time: 10:30:00, status: ok, mode=gaming",
    "pinned 3 items; spinning up 2 workers; pinentry started",
    "keyboard: us, max_tokens=500, token_count=12",
    "curl -s https://example.org/x -o out.txt",
    "mysql -u root -h localhost db",
    "ssh -p 2222 host",
    "Opened: /home/u/Documents/password-manager-notes.txt",
    "Disk / is 91% full",
    "GPU pin_memory=true",
    # rvGame round 2: over-redaction of ordinary text
    "GPIO pin: 17 set high", "cpu pin: 3", "PW: 120W", "Session\t42 started", "secret\tnot set",
    "Tests: pass\t12", "pw_hash_rounds=12", "Пароль: изменён", "pin_count=4", "SIM PIN: required",
]


def hide(text):
    return policy.clean_details({"msg": text})["msg"]


class FormatsTest(unittest.TestCase):
    def test_every_format_is_hidden(self):
        for text in LEAKS:
            with self.subTest(text=text):
                out = hide(text)
                self.assertNotIn(S, out)
                self.assertNotIn(S[:6], out)
                self.assertIn(policy.HIDDEN, out)

    def test_ordinary_text_is_kept(self):
        for text in KEEP:
            with self.subTest(text=text):
                self.assertEqual(hide(text), text)

    def test_the_name_is_kept_so_you_still_see_what_was_hidden(self):
        self.assertEqual(hide(f"login failed: password={S} for bob"), "login failed: password=[hidden] for bob")
        self.assertEqual(hide(f"curl -u user:{S} https://x"), "curl -u user:[hidden] https://x")
        self.assertEqual(hide(f"mysql -u root -p{S} db"), "mysql -u root -p[hidden] db")

    def test_dict_keys_in_other_languages_and_short_names(self):
        out = policy.clean_details({"passwort": S, "pin": S, "psk": S, "pw": S, "Kennwort": S})
        self.assertNotIn(S, str(out))

    def test_a_long_value_is_hidden_to_its_end(self):
        # rvGame round 2: a value longer than 4096 characters showed its tail.
        for n in (4090, 4096, 4200, 9000):
            for text in ("password=" + "A" * n + "TAILSECRET", f'error: password="{"A" * n}TAILSECRET"',
                         "x: token: " + "A" * n + "TAILSECRET", "Пароль=" + "A1" * (n // 2) + "TAILSECRET"):
                with self.subTest(n=n, text=text[:12]):
                    out = policy.clean_details({"msg": text}, max_str=20000)["msg"]
                    self.assertNotIn("CRET", out)
                    self.assertIn(policy.HIDDEN, out)

    def test_pin_and_pw_need_a_secret_looking_value(self):
        for text in ("PIN: 1234", "pin=12345678", f"pin: {S}", f"pw: {S}", "pw=hunter2fake"):
            with self.subTest(text=text):
                self.assertIn(policy.HIDDEN, hide(text))

    def test_long_input_takes_linear_time(self):
        # rvGame round 4: with no length limit, some inputs took quadratic time (77 s for 256 KB).
        import time

        def took(text):
            best = None
            for _ in range(2):
                started = time.monotonic()
                policy._hide_text_secrets(text)
                spent = time.monotonic() - started
                best = spent if best is None else min(best, spent)
            return best
        for g in ("pin:(", "pin:<", "pin:[", "pin: (", "pw:(", "Kennwort: (", "eyJ-", "eyJaaaaaaaa.",
                  "a=b:", "curl -u ", 'a="', "x: ", "x=[", "?a=", "a://", 'x:"a=', "token: <", "sk-a-"):
            with self.subTest(g=g):
                big = (g * (1024 * 1024 // len(g) + 1))[:1024 * 1024]
                quarter = big[:256 * 1024]
                t_big, t_quarter = took(big), took(quarter)
                self.assertLess(t_big, 2.0, f"1 MB took {t_big:.2f} s")
                self.assertLess(t_big, 6 * max(t_quarter, 0.02), f"{t_quarter:.3f} s -> {t_big:.3f} s")

    def test_hidden_to_the_end_still_with_long_weak_and_jwt_values(self):
        jwt = "eyJ" + "h" * 20 + "." + "p" * 6000 + "." + "s" * 50 + "TAILSECRET"
        for text in (f"x {jwt} y", f"a-{jwt}", "Kennwort=" + "a1" * 3000 + "TAILSECRET",
                     "pw: " + "z" * 5000 + "TAILSECRET", "PIN: (" + "1a" * 3000 + "TAILSECRET)"):
            with self.subTest(text=text[:15]):
                out = policy.clean_details({"msg": text}, max_str=20000)["msg"]
                self.assertNotIn("CRET", out)
                self.assertIn(policy.HIDDEN, out)

    def test_a_secret_inside_a_weak_value_that_is_kept(self):
        self.assertEqual(hide("pin: (password=abcdefgh)"), "pin: (password=[hidden]")

    def test_many_pairs_stay_fast(self):
        import time
        text = "password=a=" * 100000
        started = time.monotonic()
        hide(text)
        self.assertLess(time.monotonic() - started, 10)


if __name__ == "__main__":
    unittest.main()
