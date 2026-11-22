"""forge-hub: connects Forge OS to the owner's Creative MCP hub clone without changing the hub.
Everything here uses fake node, npm, git, flatpak and xdg-open, plus a fake hub clone."""
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "files/system/usr/bin/forge-hub")
TOKEN = "T" * 40  # obvious fake dashboard token

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
fake = os.environ["FAKE"]
name, args = os.path.basename(sys.argv[0]), sys.argv[1:]
with open(os.path.join(fake, "calls"), "a") as f:
    f.write(json.dumps([name] + args + ["@" + os.getcwd()]) + "\n")
if name == "node":
    if args == ["--version"]:
        print(os.environ.get("FAKE_NODE_VERSION", "v22.12.0")); sys.exit(0)
    json.loads(sys.stdin.read())  # config validation: must at least be JSON
    sys.exit(1 if os.path.exists(os.path.join(fake, "invalid")) else 0)
if name == "npm":
    if os.path.exists(os.path.join(fake, "npm-fails")):
        sys.exit(1)
    if args[0] == "ci":
        os.makedirs("node_modules", exist_ok=True)
    if args[:2] == ["run", "build"]:
        for p in ["packages/hub/dist/main.js", "packages/hub/dist/config/load.js"] + [
                "servers/%s/dist/index.js" % s for s in ("blender", "studio", "aseprite", "blockbench")]:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("// built\n")
if name == "git":
    print("0123456789abcdef0123456789abcdef01234567")
if name == "xdg-open" and args and os.path.isfile(args[0]):
    st, dst = os.stat(args[0]), os.stat(os.path.dirname(args[0]))
    with open(os.path.join(fake, "opened.json"), "w") as f:
        json.dump({"text": open(args[0]).read(), "mode": st.st_mode & 0o777,
                   "dirmode": dst.st_mode & 0o777}, f)
if name == "flatpak" and args[0] == "info":
    sys.exit(0 if os.path.exists(os.path.join(fake, "flatpaks", args[-1])) else 1)
'''

POLICY = {"default": "ask", "rules": [{"match": {"tool": "get_*"}, "decision": "allow", "reason": "read-only inspection"}]}


def own_app(name, label, env):
    return {"kind": "stdio", "label": label, "origin": "own", "command": "{{NODE}}",
            "args": ["{{REPO}}\\servers\\%s\\dist\\index.js" % name], "envAllowlist": env,
            "lifecycle": {"class": "persistent-owner", "start": "eager", "idleStopMs": None},
            "queue": {"concurrency": 1}, "timeouts": {"callMs": 120000}, "policy": POLICY}


# Same shape as the hub's hub.config.example.json (trimmed policies).
EXAMPLE = {
    "version": 1,
    "dataDir": "%LOCALAPPDATA%\\creative-mcp-hub",
    "approval": {"mode": "native-dialog", "timeoutMs": 120000},
    "apps": {
        "roblox-studio": {"kind": "stdio", "label": "Roblox Studio (built-in StudioMCP)", "origin": "upstream",
                          "command": "cmd.exe", "args": ["/c", "%LOCALAPPDATA%\\Roblox\\mcp.bat"],
                          "cwd": "%LOCALAPPDATA%\\Roblox", "envAllowlist": [],
                          "lifecycle": {"class": "relay", "start": "lazy", "idleStopMs": None}, "policy": POLICY},
        "blender-hub": own_app("blender", "Blender (own)", ["HUB_BLENDER_PORT"]),
        "studio-hub": own_app("studio", "Roblox Studio (own)", ["HUB_STUDIO_PORT"]),
        "aseprite": own_app("aseprite", "Aseprite (own)", ["HUB_ASEPRITE_PORT", "HUB_ASEPRITE_EXE"]),
        "blockbench": own_app("blockbench", "Blockbench (own)", ["HUB_BLOCKBENCH_PORT"]),
    },
    "proposals": {"repoDir": "{{REPO}}"},
}


def tree_hash(path, skip=()):
    h = hashlib.sha256()
    for d, dirs, files in os.walk(path):
        dirs[:] = sorted(x for x in dirs if os.path.relpath(os.path.join(d, x), path) not in skip)
        for f in sorted(files):
            p = os.path.join(d, f)
            h.update(os.path.relpath(p, path).encode() + b"\0")
            with open(p, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


class FakeDashboard(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        FakeDashboard.seen.append((self.path, self.headers.get("X-Hub-Token")))
        if self.path != "/api/state" or self.headers.get("X-Hub-Token") != TOKEN:
            self.send_response(401); self.end_headers(); return
        body = json.dumps({"apps": [
            {"id": "studio-hub", "label": "Roblox Studio (own)", "status": "connected", "lastError": None},
            {"id": "blender-hub", "label": "Blender (own)", "status": "stopped", "lastError": None}],
            "pending": [{"id": "x"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class HubTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp(prefix="forge hub ")  # a space in every path
        self.addCleanup(shutil.rmtree, self.t, True)
        self.home = os.path.join(self.t, "home dir")
        self.bin = os.path.join(self.t, "bin")
        self.fake = os.path.join(self.t, "fake")
        for d in (self.home, self.bin, os.path.join(self.fake, "flatpaks")):
            os.makedirs(d)
        for name in ("node", "npm", "git", "flatpak", "xdg-open"):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(FAKE)
            os.chmod(p, 0o755)
        self.clone = os.path.join(self.t, "my hub clone")
        self.make_clone()
        self.data = os.path.join(self.home, ".local/share/creative-mcp-hub")
        self.claude = os.path.join(self.home, ".config/Claude/claude_desktop_config.json")
        self.state = os.path.join(self.home, ".local/state/forge/hub.json")

    def make_clone(self):
        files = {
            "package.json": json.dumps({"name": "creative-mcp-hub-workspace", "private": True}),
            "hub.config.example.json": json.dumps(EXAMPLE, indent=2),
            "servers/blender/plugin/__init__.py": "# add-on\n",
            "servers/blender/plugin/commands/scene.py": "# cmd\n",
            "servers/blender/plugin/blender_manifest.toml": 'id = "creative_hub"\nversion = "0.1.0"\n',
            "servers/blender/plugin/README.md": "readme\n",
            "servers/blender/plugin/tests/test_bridge.py": "# test\n",
            "servers/blender/plugin/__pycache__/x.cpython-313.pyc": "pyc",
            "servers/aseprite/plugin/package.json": "{}\n",
            "servers/aseprite/plugin/main.lua": "-- main\n",
            "servers/aseprite/plugin/hub/json.lua": "-- json\n",
            "servers/aseprite/plugin/README.md": "readme\n",
            "servers/blockbench/plugin/build/creative_hub.js": "// plugin\n",
            "servers/studio/plugin/build/CreativeHub.lua": "-- plugin\n",
        }
        for rel, text in files.items():
            p = os.path.join(self.clone, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)

    def run_tool(self, *args, stdin="", env=None):
        e = {"HOME": self.home, "PATH": self.bin + ":/usr/bin:/bin", "FAKE": self.fake, "LANG": "C.UTF-8"}
        e.update(env or {})
        return subprocess.run([sys.executable, TOOL, *args], env=e, input=stdin, capture_output=True,
                              text=True, timeout=60)

    def calls(self, name=None):
        p = os.path.join(self.fake, "calls")
        calls = [json.loads(x) for x in open(p).read().splitlines()] if os.path.exists(p) else []
        return [c for c in calls if name is None or c[0] == name]

    def write_claude(self, data, raw=None):
        os.makedirs(os.path.dirname(self.claude), exist_ok=True)
        with open(self.claude, "w") as f:
            f.write(raw if raw is not None else json.dumps(data))

    def setup_ok(self, *extra, stdin="n\n"):
        r = self.run_tool("setup", self.clone, *extra, stdin=stdin)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def backups(self):
        d = os.path.dirname(self.claude)
        return sorted(f for f in os.listdir(d) if ".forge-bak-" in f) if os.path.isdir(d) else []

    # ---- setup ----

    def test_refuses_old_node(self):
        r = self.run_tool("setup", self.clone, env={"FAKE_NODE_VERSION": "v20.11.0"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("22", r.stdout + r.stderr)
        self.assertEqual(self.calls("npm"), [])

    def test_refuses_folder_that_is_not_the_hub(self):
        other = os.path.join(self.t, "other")
        os.makedirs(other)
        r = self.run_tool("setup", other)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.calls("npm"), [])

    def test_builds_with_the_hubs_own_npm_commands(self):
        self.setup_ok()
        npm = self.calls("npm")
        self.assertEqual([c[1:3] for c in npm], [["ci", "--ignore-scripts"], ["run", "build"]])
        self.assertTrue(all(c[-1] == "@" + self.clone for c in npm))

    def test_stops_if_build_fails(self):
        open(os.path.join(self.fake, "npm-fails"), "w").close()
        r = self.run_tool("setup", self.clone)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.claude))

    def test_writes_linux_config_filling_only_paths(self):
        self.setup_ok()
        path = os.path.join(self.data, "hub.config.json")
        cfg = json.load(open(path))
        node = os.path.join(self.bin, "node")
        self.assertEqual(cfg["dataDir"], self.data)
        self.assertNotIn("roblox-studio", cfg["apps"])  # cmd.exe and mcp.bat only exist on Windows
        self.assertEqual(cfg["approval"]["mode"], "dashboard-then-dialog")
        # The hub's "merge a proposal" feature would change the clone (and break its build on
        # Linux), so Forge turns it off: Forge never changes the hub.
        self.assertIsNone(cfg["proposals"]["repoDir"])
        for app_id in ("blender-hub", "studio-hub", "aseprite", "blockbench"):
            a, ex = cfg["apps"][app_id], EXAMPLE["apps"][app_id]
            self.assertEqual(a["command"], node)
            server = app_id.replace("-hub", "")
            self.assertEqual(a["args"], [os.path.join(self.clone, "servers", server, "dist", "index.js")])
            self.assertEqual({k: v for k, v in a.items() if k not in ("command", "args")},
                             {k: v for k, v in ex.items() if k not in ("command", "args")})
        self.assertNotIn("{{", json.dumps(cfg))
        self.assertNotIn("\\\\", json.dumps(cfg))
        self.assertEqual(os.stat(self.data).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        # validated with the hub's own parseConfig
        self.assertTrue(any("--input-type=module" in c for c in self.calls("node")))

    def test_invalid_generated_config_is_not_written(self):
        open(os.path.join(self.fake, "invalid"), "w").close()
        r = self.run_tool("setup", self.clone)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.data, "hub.config.json")))

    def test_existing_hub_config_kept_unless_force(self):
        self.setup_ok()
        path = os.path.join(self.data, "hub.config.json")
        with open(path, "w") as f:
            f.write('{"mine": true}')
        self.setup_ok()
        self.assertEqual(open(path).read(), '{"mine": true}')
        self.setup_ok("--force")
        self.assertIn("apps", json.load(open(path)))
        baks = [f for f in os.listdir(self.data) if f.startswith("hub.config.json.bak-")]
        self.assertEqual(len(baks), 1)
        self.assertEqual(open(os.path.join(self.data, baks[0])).read(), '{"mine": true}')

    def test_plugin_files_built_like_hub_setup(self):
        self.setup_ok()
        inst = os.path.join(self.data, "install")
        with zipfile.ZipFile(os.path.join(inst, "creative_hub-0.1.0.zip")) as z:
            self.assertEqual(sorted(z.namelist()), ["__init__.py", "blender_manifest.toml", "commands/scene.py"])
        with zipfile.ZipFile(os.path.join(inst, "creative-hub-bridge.aseprite-extension")) as z:
            self.assertEqual(sorted(z.namelist()), ["hub/json.lua", "main.lua", "package.json"])
        self.assertEqual(open(os.path.join(inst, "creative_hub.js")).read(), "// plugin\n")
        self.assertEqual(open(os.path.join(inst, "CreativeHub.lua")).read(), "-- plugin\n")

    # ---- Claude Desktop config ----

    def test_claude_entry_merged_keeping_everything_else(self):
        before = {"mcpServers": {"other": {"command": "x"}}, "globalShortcut": "Ctrl+Space"}
        self.write_claude(before)
        self.setup_ok()
        cfg = json.load(open(self.claude))
        self.assertEqual(cfg["globalShortcut"], "Ctrl+Space")
        self.assertEqual(cfg["mcpServers"]["other"], {"command": "x"})
        entry = cfg["mcpServers"]["creative-mcp-hub"]
        self.assertEqual(entry["command"], os.path.join(self.bin, "node"))
        self.assertEqual(entry["args"], [os.path.join(self.clone, "packages/hub/dist/main.js")])
        self.assertEqual(entry["env"], {"LOCALAPPDATA": os.path.join(self.home, ".local/share"),
                                        "HUB_CONFIG": os.path.join(self.data, "hub.config.json")})
        baks = self.backups()
        self.assertEqual(len(baks), 1)
        self.assertEqual(json.load(open(os.path.join(os.path.dirname(self.claude), baks[0]))), before)

    def test_claude_config_created_when_missing(self):
        self.setup_ok()
        self.assertEqual(list(json.load(open(self.claude))["mcpServers"]), ["creative-mcp-hub"])

    def test_invalid_claude_config_refused(self):
        self.write_claude(None, raw="{ not json")
        r = self.run_tool("setup", self.clone)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not valid JSON", r.stdout + r.stderr)
        self.assertEqual(open(self.claude).read(), "{ not json")
        self.assertEqual(self.backups(), [])

    def test_symlinked_claude_config_refused(self):
        target = os.path.join(self.t, "elsewhere.json")
        with open(target, "w") as f:
            f.write("{}")
        os.makedirs(os.path.dirname(self.claude))
        os.symlink(target, self.claude)
        r = self.run_tool("setup", self.clone)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("symlink", r.stdout + r.stderr)
        self.assertEqual(open(target).read(), "{}")
        self.assertTrue(os.path.islink(self.claude))

    # ---- apps and Flatpak overrides ----

    def install_flatpaks(self, *ids):
        for i in ids:
            open(os.path.join(self.fake, "flatpaks", i), "w").close()

    def overrides(self):
        return [c[:-1] for c in self.calls("flatpak") if c[1] == "override"]

    def test_overrides_need_confirmation(self):
        self.install_flatpaks("org.blender.Blender", "net.blockbench.Blockbench")
        r = self.setup_ok(stdin="n\n")
        self.assertEqual(self.overrides(), [])
        self.assertIn("flatpak override --user", r.stdout)

    def test_overrides_are_user_level_and_narrow(self):
        self.install_flatpaks("org.blender.Blender", "net.blockbench.Blockbench")
        self.setup_ok(stdin="y\n")
        bridges = os.path.join(self.data, "bridges")
        self.assertEqual(self.overrides(), [
            ["flatpak", "override", "--user", "--env=HUB_BRIDGE_DIR=" + bridges, "org.blender.Blender"],
            ["flatpak", "override", "--user", "--filesystem=" + bridges + ":ro", "net.blockbench.Blockbench"],
        ])

    def test_no_overrides_for_apps_not_installed(self):
        r = self.setup_ok(stdin="y\n")
        self.assertEqual(self.overrides(), [])
        self.assertIn("Aseprite", r.stdout)
        self.assertIn("not installed", r.stdout)

    def test_studio_plugin_folder_inside_vinegar_prefix(self):
        prefix = os.path.join(self.home, ".var/app/org.vinegarhq.Vinegar/data/vinegar/prefixes/studio")
        os.makedirs(os.path.join(prefix, "drive_c/users/alice/AppData/Local/Roblox"))
        os.makedirs(os.path.join(prefix, "drive_c/users/Public"))
        r = self.setup_ok()
        self.assertIn(os.path.join(prefix, "drive_c/users/alice/AppData/Local/Roblox/Plugins"), r.stdout)

    # ---- status ----

    def start_dashboard(self):
        srv = http.server.HTTPServer(("127.0.0.1", 0), FakeDashboard)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with open(os.path.join(self.data, "dashboard.url"), "w") as f:
            f.write("http://127.0.0.1:%d/?t=%s\n" % (srv.server_port, TOKEN))
        return srv

    def test_status_json(self):
        self.setup_ok()
        self.start_dashboard()
        r = self.run_tool("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        s = json.loads(r.stdout)
        self.assertTrue(s["hub_built"])
        self.assertEqual(s["commit"], "0123456789abcdef0123456789abcdef01234567")
        self.assertTrue(s["hub_config_present"])
        self.assertTrue(s["claude_entry_present"])
        self.assertTrue(s["hub_running"])
        self.assertEqual(s["pending_approvals"], 1)
        self.assertEqual([(a["id"], a["status"]) for a in s["apps"]],
                         [("studio-hub", "connected"), ("blender-hub", "stopped")])
        self.assertEqual([p for p, _ in FakeDashboard.seen if p.startswith("/api")][-1], "/api/state")
        r = self.run_tool("status")
        self.assertEqual(r.returncode, 0)
        self.assertIn("studio-hub", r.stdout)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)

    def test_status_when_hub_not_running(self):
        self.setup_ok()
        with open(os.path.join(self.data, "dashboard.url"), "w") as f:
            f.write("http://127.0.0.1:1/?t=%s\n" % TOKEN)
        s = json.loads(self.run_tool("status", "--json").stdout)
        self.assertFalse(s["hub_running"])
        self.assertIsNone(s["apps"])

    def test_status_before_setup(self):
        r = self.run_tool("status", "--json")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(json.loads(r.stdout)["set_up"])

    # ---- dashboard ----

    def test_dashboard_opens_without_printing_token(self):
        self.setup_ok()
        self.start_dashboard()
        r = self.run_tool("dashboard")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        opened = self.calls("xdg-open")
        self.assertEqual(len(opened), 1)
        # The token never goes on a command line (other users can read /proc/PID/cmdline).
        self.assertNotIn(TOKEN, json.dumps(opened))
        self.assertTrue(os.path.isabs(opened[0][1]))
        with open(os.path.join(self.fake, "opened.json")) as f:
            seen = json.load(f)
        # xdg-open got a private redirect page that sends the browser to the dashboard.
        self.assertIn("http://127.0.0.1:", seen["text"])
        self.assertIn("/?t=" + TOKEN, seen["text"])
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(seen["dirmode"], 0o700)

    def test_dashboard_redirect_page_in_runtime_dir_and_deleted(self):
        self.setup_ok()
        self.start_dashboard()
        run = os.path.join(self.t, "run")
        os.makedirs(run, mode=0o700)
        r = self.run_tool("dashboard", env={"XDG_RUNTIME_DIR": run})
        self.assertEqual(r.returncode, 0, r.stderr)
        page = self.calls("xdg-open")[0][1]
        self.assertTrue(page.startswith(run + os.sep), page)
        # The page is removed shortly after (here: the helper with a tiny delay).
        import importlib.machinery, importlib.util, time
        loader = importlib.machinery.SourceFileLoader("forge_hub_mod", TOOL)
        spec = importlib.util.spec_from_loader("forge_hub_mod", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        p = os.path.join(run, "gone.html")
        open(p, "w").close()
        mod.delete_later(p, 0.2)
        for _ in range(50):
            if not os.path.exists(p):
                break
            time.sleep(0.1)
        self.assertFalse(os.path.exists(p))

    def test_dashboard_refuses_non_loopback_link(self):
        self.setup_ok()
        with open(os.path.join(self.data, "dashboard.url"), "w") as f:
            f.write("http://evil.example:47821/?t=%s\n" % TOKEN)
        r = self.run_tool("dashboard")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.calls("xdg-open"), [])
        self.assertNotIn(TOKEN, r.stdout + r.stderr)

    def test_dashboard_before_hub_ran(self):
        self.setup_ok()
        r = self.run_tool("dashboard")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Claude Desktop", r.stdout + r.stderr)

    # ---- remove ----

    def test_remove_undoes_entry_and_overrides_only(self):
        self.install_flatpaks("org.blender.Blender", "net.blockbench.Blockbench")
        self.write_claude({"mcpServers": {"other": {"command": "x"}}})
        self.setup_ok(stdin="y\n")
        r = self.run_tool("remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.load(open(self.claude)), {"mcpServers": {"other": {"command": "x"}}})
        self.assertEqual(len(self.backups()), 2)
        bridges = os.path.join(self.data, "bridges")
        self.assertEqual(self.overrides()[2:], [
            ["flatpak", "override", "--user", "--unset-env=HUB_BRIDGE_DIR", "org.blender.Blender"],
            ["flatpak", "override", "--user", "--nofilesystem=" + bridges, "net.blockbench.Blockbench"],
        ])
        self.assertFalse(os.path.exists(self.state))
        self.assertTrue(os.path.exists(os.path.join(self.data, "hub.config.json")))  # user data kept

    def test_remove_restores_an_entry_that_was_there_before(self):
        self.write_claude({"mcpServers": {"creative-mcp-hub": {"command": "old"}}})
        self.setup_ok()
        self.run_tool("remove")
        self.assertEqual(json.load(open(self.claude)), {"mcpServers": {"creative-mcp-hub": {"command": "old"}}})

    def test_remove_refuses_symlinked_claude_config(self):
        self.setup_ok()
        target = os.path.join(self.t, "elsewhere.json")
        shutil.move(self.claude, target)
        os.symlink(target, self.claude)
        r = self.run_tool("remove")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("creative-mcp-hub", json.load(open(target))["mcpServers"])

    # ---- the hub clone is never changed ----

    def test_no_hub_files_modified(self):
        self.install_flatpaks("org.blender.Blender", "net.blockbench.Blockbench")
        before = tree_hash(self.clone)
        self.setup_ok(stdin="y\n")
        # Only the hub's own build output appears (node_modules and dist, both gitignored by the hub).
        build_out = {"node_modules", "packages/hub/dist"} | {"servers/%s/dist" % s for s in
                                                             ("blender", "studio", "aseprite", "blockbench")}
        self.assertEqual(tree_hash(self.clone, skip=build_out), before)
        built = tree_hash(self.clone)
        self.start_dashboard()
        for args in (["status"], ["status", "--json"], ["dashboard"], ["remove"]):
            self.run_tool(*args)
            self.assertEqual(tree_hash(self.clone), built, args)


if __name__ == "__main__":
    unittest.main()
