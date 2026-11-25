#!/usr/bin/env python3
"""Tests for `tartci owners install|uninstall` (scripts/owners_service.py) and the LaunchAgent
template. git is real (a throwaway repository); launchctl, gh, tart, plutil and ps are faked, so
nothing is loaded, exported outside a temp dir, or sent to GitHub."""
from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import owners_dispatch as od  # noqa: E402
import owners_service as svc  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / svc.TEMPLATE_RELATIVE
VALUES = {"HOME": "/Users/example", "APP": "/Users/example/.tartci/owners/app/" + "a" * 40, "CONFIG": "/Users/example/.config/tartci/owners.toml",
          "ROOT": "/Users/example/.tartci/owners", "PATHDIRS": "/opt/homebrew/bin", "PYTHON": "/opt/homebrew/bin/python3.12"}


def proc(code: int = 0, out: Any = "", err: str = "") -> Any:
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


class TemplateTests(unittest.TestCase):
    def render(self, **over: str) -> Dict[str, Any]:
        return plistlib.loads(svc.render_plist(TEMPLATE.read_bytes(), {**VALUES, **over}))

    def test_label_and_lifecycle_keys(self) -> None:
        data = self.render()
        self.assertEqual(data["Label"], "com.danielraffel.tartci.owners")
        self.assertIs(data["RunAtLoad"], True)
        self.assertIs(data["KeepAlive"], True)
        self.assertEqual(data["ProcessType"], "Standard")
        self.assertGreaterEqual(data["ThrottleInterval"], 10)
        self.assertEqual(data["WorkingDirectory"], VALUES["HOME"])

    def test_exit_timeout_outlasts_the_dispatchers_child_grace(self) -> None:
        timeout = self.render()["ExitTimeOut"]
        self.assertIsInstance(timeout, int)
        self.assertGreaterEqual(timeout, od.CHILD_TERM_GRACE_SECONDS + 30)
        self.assertLess(timeout, svc.UNLOAD_WAIT_SECONDS)  # uninstall waits longer than launchd does

    def test_program_arguments_run_the_pinned_export_under_caffeinate(self) -> None:
        argv = self.render()["ProgramArguments"]
        self.assertEqual(argv[:3], ["/usr/bin/caffeinate", "-i", "/bin/bash"])
        self.assertEqual(argv[3], VALUES["APP"] + "/tartci")  # the full-sha path, never `current`
        self.assertNotIn("current", argv[3])
        self.assertEqual(argv[4:], ["owners", "--config", VALUES["CONFIG"], "--root", VALUES["ROOT"], "run"])
        for item in argv:
            self.assertNotIn("$", item)

    def test_environment_is_pinned_and_holds_no_credentials(self) -> None:
        env = self.render()["EnvironmentVariables"]
        self.assertEqual(env["HOME"], VALUES["HOME"])
        self.assertEqual(env["TART_HOME"], "/Users/example/.tart")
        self.assertEqual(env["TARTCI_VM_SSH_KEY"], "/Users/example/.config/tartci/owners-vm-ed25519")
        self.assertEqual(env["TARTCI_PYTHON"], VALUES["PYTHON"])
        self.assertEqual(env["PATH"], "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        for entry in env["PATH"].split(":"):
            self.assertTrue(entry.startswith("/"), entry)
        for name in env:
            self.assertNotRegex(name.upper(), r"TOKEN|SECRET|PASSWORD|KEY_DATA|JIT|CREDENTIAL")
        for name, value in env.items():
            self.assertNotRegex(value, r"gh[pousr]_[A-Za-z0-9]{20,}|encoded_jit_config", name)

    def test_logs_land_in_the_users_library_logs(self) -> None:
        data = self.render()
        self.assertEqual(data["StandardOutPath"], "/Users/example/Library/Logs/tartci/owners.out.log")
        self.assertEqual(data["StandardErrorPath"], "/Users/example/Library/Logs/tartci/owners.err.log")

    def test_every_path_in_the_plist_is_absolute(self) -> None:
        data = self.render()
        for key in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
            self.assertTrue(data[key].startswith("/"), key)
        for item in (data["ProgramArguments"][0], data["ProgramArguments"][3], data["ProgramArguments"][6],
                     data["ProgramArguments"][8], data["EnvironmentVariables"]["TARTCI_PYTHON"]):
            self.assertTrue(item.startswith("/"), item)

    def test_a_substituted_value_is_never_substituted_again(self) -> None:
        for bad in ("/tmp/$HOME", "/tmp/a\nb", "", "/tmp/a\0b"):
            with self.assertRaises(svc.ServiceError):
                svc.render_plist(TEMPLATE.read_bytes(), {**VALUES, "ROOT": bad})

    def test_a_template_with_an_unfilled_placeholder_is_refused(self) -> None:
        values = {k: v for k, v in VALUES.items() if k != "PYTHON"}
        with self.assertRaises(svc.ServiceError) as caught:
            svc.render_plist(TEMPLATE.read_bytes(), values)
        self.assertIn("$PYTHON", str(caught.exception))

    def test_the_template_parses_without_rendering_and_matches_the_repos_launchd_lints(self) -> None:
        text = TEMPLATE.read_text()
        self.assertEqual(len(re.findall(r"<integer>\d+</integer>", text)), 2)  # literal numbers: parseable as-is
        body = re.sub(rb"<!--.*?-->", b"", TEMPLATE.read_bytes(), flags=re.DOTALL)  # the header comment only explains
        self.assertNotIn(b"TOKEN", body.upper())
        data = plistlib.loads(body)
        self.assertEqual(data["Label"], svc.LABEL)

    def test_path_dirs_lists_tool_dirs_once_and_drops_system_dirs(self) -> None:
        table = {"tart": "/opt/homebrew/bin/tart", "gh": "/opt/homebrew/bin/gh", "jq": "/usr/bin/jq",
                 "sshpass": "/usr/local/bin/sshpass"}
        self.assertEqual(svc.path_dirs("gh", table.get), ["/opt/homebrew/bin", "/usr/local/bin"])

    def test_path_dirs_needs_tart_and_gh(self) -> None:
        with self.assertRaises(svc.ServiceError) as caught:
            svc.path_dirs("gh", {"gh": "/opt/homebrew/bin/gh"}.get)
        self.assertIn("tart", str(caught.exception))
        with self.assertRaises(svc.ServiceError):
            svc.path_dirs("ghx", {"tart": "/opt/homebrew/bin/tart", "gh": "/opt/homebrew/bin/gh"}.get)

    def test_path_dirs_never_yields_an_empty_path(self) -> None:
        self.assertEqual(svc.path_dirs("gh", {"tart": "/usr/bin/tart", "gh": "/bin/gh"}.get), ["/usr/local/bin"])


class FakeSystem:
    """launchctl / gh / plutil / tart / ps, plus real git."""

    def __init__(self) -> None:
        self.loaded = False
        self.running = True
        self.calls: List[List[str]] = []
        self.gh_ok = True
        self.vms: List[str] = ["owners-macos", "owners-linux", "ghcr.io/cirruslabs/macos-tahoe-base:latest"]
        self.ps = ""
        self.stuck_prints = 0  # `launchctl print` keeps finding the job this many times after bootout
        self.lint_ok = True
        self.on_bootout: Optional[Any] = None

    def __call__(self, argv: List[str], **kw: Any) -> Any:
        self.calls.append(list(argv))
        tool = argv[0]
        if tool == "git":
            return subprocess.run(argv, **{k: v for k, v in kw.items() if k in ("capture_output", "text", "timeout")})
        if tool == "launchctl":
            return self.launchctl(argv[1:])
        if tool == "gh":
            return proc(0 if self.gh_ok else 1, "", "" if self.gh_ok else "You are not logged into any GitHub hosts")
        if tool == "plutil":
            return proc(0 if self.lint_ok else 1, "", "")
        if tool == "tart":
            return proc(0, json.dumps([{"Name": name, "Source": "local"} for name in self.vms]))
        if tool == "ps":
            return proc(0, self.ps)
        raise AssertionError(f"unexpected command {argv}")

    def launchctl(self, args: List[str]) -> Any:
        verb = args[0]
        if verb == "print":
            if not self.loaded:
                return proc(113, "", "Could not find service")
            if self.stuck_prints:
                self.stuck_prints -= 1
            state = "running" if self.running else "not running"
            return proc(0, f"{svc.LABEL} = {{\n\tactive count = 1\n\tstate = {state}\n\tpid = 4242\n\truns = 1\n\t"
                           f"last exit code = (never exited)\n}}\n")
        if verb == "bootout":
            if self.on_bootout:
                self.on_bootout()
            if self.stuck_prints:
                return proc(0)
            self.loaded = False
            return proc(0)
        if verb == "bootstrap":
            if self.loaded:
                return proc(5, "", "Bootstrap failed: 5: Input/output error")
            self.loaded = True
            return proc(0)
        if verb == "kickstart":
            return proc(0 if self.loaded else 113, "", "" if self.loaded else "Could not find service")
        raise AssertionError(args)

    def launchctl_calls(self) -> List[List[str]]:
        return [c[1:] for c in self.calls if c[0] == "launchctl" and c[1] != "print"]


class ServiceCase(unittest.TestCase):
    CONFIG = {"owners": ["org1"], "repos": ["org2/x"]}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="owners-svc-")).resolve()
        self.addCleanup(self.cleanup)
        self.home = str(self.tmp / "home")
        os.makedirs(self.home)
        self.layout = svc.Layout(home=self.home, root=os.path.join(self.home, ".tartci", "owners"))
        self.checkout = str(self.tmp / "checkout")
        self.git("init", "-q", self.checkout, cwd=str(self.tmp))
        self.system = FakeSystem()
        self.config = str(self.tmp / "owners.toml")
        Path(self.config).write_text("# parsed by the fake loader\n")
        self.lines: List[str] = []
        self.now = 1000.0
        self.tools = {"tart": "/opt/homebrew/bin/tart", "gh": "/opt/homebrew/bin/gh"}
        self.commit_files({"tartci": "#!/bin/bash\necho v1\n", "scripts/owners_dispatch.py": "# v1\n",
                           svc.TEMPLATE_RELATIVE: TEMPLATE.read_text(), "README.md": "one\n"}, "first")

    def cleanup(self) -> None:
        svc.remove_tree(str(self.tmp))

    def git(self, *args: str, cwd: Optional[str] = None) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": "/dev/null"}
        return subprocess.run(["git", *args], cwd=cwd or self.checkout, env=env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit_files(self, files: Dict[str, str], message: str) -> str:
        for name, text in files.items():
            path = Path(self.checkout, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            if name == "tartci":
                path.chmod(0o755)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def load_config(self, path: str) -> od.Config:
        if not os.path.isfile(path):
            raise od.ConfigError(f"{path}: no such file")
        return od.parse_config(self.CONFIG)

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def install(self, rev: str = "HEAD", dry_run: bool = False, config: Optional[str] = None) -> int:
        return svc.install(self.layout, self.checkout, config or self.config, rev, dry_run, run=self.system,
                           which=self.tools.get, python="/opt/homebrew/bin/python3.12", uid=501, sleep=self.sleep,
                           clock=lambda: self.now, log=self.lines.append, load_config=self.load_config,
                           forwarder_login=self.forwarder_login)

    def forwarder_login(self, config_dir: str) -> str:
        return "gho_FakeForwarderLOGIN_Value9"

    def uninstall(self, purge: bool = False) -> int:
        return svc.uninstall(self.layout, purge, run=self.system, uid=501, sleep=self.sleep, clock=lambda: self.now,
                             log=self.lines.append, alive=lambda *a, **k: False)

    def out(self) -> str:
        return "\n".join(self.lines)


class InstallTests(ServiceCase):
    def test_install_exports_the_head_commit_pins_it_and_loads_the_agent(self) -> None:
        sha = self.git("rev-parse", "HEAD")
        self.assertEqual(self.install(), 0)
        app = self.layout.export_dir(sha)
        self.assertEqual(Path(app, "COMMIT").read_text().strip(), sha)
        self.assertEqual(os.readlink(self.layout.current), sha)
        self.assertEqual(svc.installed_commit(self.layout), sha)
        self.assertEqual(svc.plist_commit(self.layout), sha)
        self.assertEqual(stat.S_IMODE(os.stat(self.layout.plist).st_mode), 0o644)
        data = plistlib.loads(Path(self.layout.plist).read_bytes())
        self.assertEqual(data["ProgramArguments"][3], app + "/tartci")
        self.assertEqual(data["ProgramArguments"][5:9], ["--config", self.config, "--root", self.layout.root])
        self.assertEqual(data["EnvironmentVariables"]["PATH"], "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(self.system.launchctl_calls(),
                         [["bootstrap", "gui/501", self.layout.plist], ["kickstart", f"gui/501/{svc.LABEL}"]])
        self.assertIn(f"installed commit {sha[:12]}", self.out())

    def test_the_export_is_private_read_only_and_without_git(self) -> None:
        sha = self.git("rev-parse", "HEAD")
        self.install()
        app = self.layout.export_dir(sha)
        self.assertEqual(stat.S_IMODE(os.stat(app).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.layout.app_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(app, "scripts")).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(app, "README.md")).st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(app, "tartci")).st_mode), 0o555)
        self.assertFalse(os.path.exists(os.path.join(app, ".git")))
        self.assertEqual(od.recorded_commit(app), sha)
        self.assertEqual(od.git_commit(app), sha[:12])  # the pinned tree has no .git: COMMIT answers

    def test_only_the_committed_tree_is_exported_and_a_dirty_copy_is_warned_about(self) -> None:
        sha = self.git("rev-parse", "HEAD")
        Path(self.checkout, "README.md").write_text("edited, uncommitted\n")
        Path(self.checkout, "untracked.txt").write_text("x\n")
        self.install()
        self.assertEqual(Path(self.layout.export_dir(sha), "README.md").read_text(), "one\n")
        self.assertFalse(os.path.exists(os.path.join(self.layout.export_dir(sha), "untracked.txt")))
        self.assertIn("uncommitted change", self.out())

    def test_rev_selects_another_commit_and_a_clean_pin_has_no_warning(self) -> None:
        first = self.git("rev-parse", "HEAD")
        self.commit_files({"README.md": "two\n"}, "second")
        Path(self.checkout, "README.md").write_text("dirty\n")
        self.install(rev=first)
        self.assertEqual(svc.installed_commit(self.layout), first)
        self.assertNotIn("uncommitted", self.out())

    def test_an_unknown_or_malformed_rev_is_refused_before_anything_is_written(self) -> None:
        for rev in ("no-such-branch", "--output=/tmp/x", "a b"):
            with self.assertRaises(svc.ServiceError):
                self.install(rev=rev)
        self.assertFalse(os.path.exists(self.layout.root))
        self.assertEqual(self.system.launchctl_calls(), [])

    def test_reinstalling_the_same_commit_is_idempotent(self) -> None:
        sha = self.git("rev-parse", "HEAD")
        self.install()
        mtime = os.stat(os.path.join(self.layout.export_dir(sha), "COMMIT")).st_mtime_ns
        self.install()
        self.assertEqual(sorted(os.listdir(self.layout.app_dir)), sorted([sha, "current"]))
        self.assertEqual(os.stat(os.path.join(self.layout.export_dir(sha), "COMMIT")).st_mtime_ns, mtime)
        self.assertFalse(os.path.lexists(self.layout.previous))
        self.assertEqual(self.system.launchctl_calls().count(["bootout", "gui/501/" + svc.LABEL]), 1)  # replaced once

    def test_a_new_commit_adds_a_directory_flips_current_and_keeps_the_old_one_as_previous(self) -> None:
        first = self.git("rev-parse", "HEAD")
        self.install()
        second = self.commit_files({"README.md": "two\n"}, "second")
        self.install()
        self.assertEqual(os.readlink(self.layout.current), second)
        self.assertEqual(os.readlink(self.layout.previous), first)
        self.assertEqual(sorted(os.listdir(self.layout.app_dir)), sorted([first, second, "current", "previous"]))
        self.assertEqual(svc.plist_commit(self.layout), second)
        self.assertEqual(["bootout", "gui/501/" + svc.LABEL] in self.system.launchctl_calls(), True)

    def test_older_exports_are_pruned_and_never_a_foreign_directory(self) -> None:
        shas = [self.git("rev-parse", "HEAD")]
        self.install()
        Path(self.layout.app_dir, "not-an-export").mkdir()
        for n in range(3):
            shas.append(self.commit_files({"README.md": f"v{n}\n"}, f"c{n}"))
            self.install()
        kept = sorted(os.listdir(self.layout.app_dir))
        self.assertEqual(kept, sorted([shas[-1], shas[-2], "current", "previous", "not-an-export"]))
        self.assertEqual(os.readlink(self.layout.previous), shas[-2])
        self.assertIn("pruned", self.out())

    def exports(self) -> List[str]:
        return sorted(name for name in os.listdir(self.layout.app_dir) if re.match(r"^[0-9a-f]{40}$", name))

    def test_a_failed_install_followed_by_another_never_prunes_the_export_the_agent_runs(self) -> None:
        a = self.git("rev-parse", "HEAD")
        self.install()
        self.system.lint_ok = False  # B and C are refused after their export, before the plist is replaced
        b = self.commit_files({"README.md": "B\n"}, "B")
        with self.assertRaises(svc.ServiceError):
            self.install()
        c = self.commit_files({"README.md": "C\n"}, "C")
        with self.assertRaises(svc.ServiceError):
            self.install()
        self.assertEqual(svc.plist_commit(self.layout), a)  # the loaded agent still runs A
        self.assertEqual(os.readlink(self.layout.current), a)  # and the links still say so
        self.assertIn(a, self.exports())
        self.assertEqual(self.exports(), sorted([a, b, c]))  # nothing was pruned by the failures
        self.system.lint_ok = True
        d = self.commit_files({"README.md": "D\n"}, "D")
        self.install()
        self.assertEqual(self.exports(), sorted([d, a]))  # D now runs, A is its `previous`; B and C go
        self.assertEqual(os.readlink(self.layout.previous), a)

    def test_prune_keeps_the_commit_the_plist_runs_whatever_the_links_say(self) -> None:
        a = self.git("rev-parse", "HEAD")
        self.install()
        b = self.commit_files({"README.md": "B\n"}, "B")
        c = self.commit_files({"README.md": "C\n"}, "C")
        for sha in (b, c):  # what the old order produced: the links moved on, the plist still runs A
            svc.export_commit(self.layout, self.checkout, sha, self.system)
            svc.point_current(self.layout, sha)
        self.assertEqual(svc.plist_commit(self.layout), a)
        self.assertEqual(svc.prune_exports(self.layout), [])
        self.assertEqual(self.exports(), sorted([a, b, c]))

    def test_point_current_only_flips_links_and_pruning_waits_for_a_successful_bootstrap(self) -> None:
        a = self.git("rev-parse", "HEAD")
        self.install()
        b = self.commit_files({"README.md": "B\n"}, "B")
        self.install()
        c = self.commit_files({"README.md": "C\n"}, "C")
        orig = self.system.launchctl
        seen: Dict[str, Any] = {}

        def launchctl(args: List[str]) -> Any:
            if args[0] == "bootstrap":
                seen["at_bootstrap"] = self.exports()
            return orig(args)

        self.system.launchctl = launchctl  # type: ignore[assignment]
        self.install()
        self.assertEqual(seen["at_bootstrap"], sorted([a, b, c]))  # nothing pruned before the bootstrap
        self.assertEqual(self.exports(), sorted([b, c]))

    def test_a_failed_bootstrap_prunes_nothing(self) -> None:
        a = self.git("rev-parse", "HEAD")
        self.install()
        b = self.commit_files({"README.md": "B\n"}, "B")
        orig = self.system.launchctl
        self.system.launchctl = lambda args: proc(5, "", "Bootstrap failed") if args[0] == "bootstrap" else orig(args)  # type: ignore[assignment]
        with self.assertRaises(svc.ServiceError):
            self.install()
        self.assertEqual(self.exports(), sorted([a, b]))
        self.assertEqual(svc.plist_commit(self.layout), b)  # the plist was replaced, so B must stay
        self.assertEqual(os.readlink(self.layout.previous), a)

    def test_a_kickstart_that_raises_still_prunes_and_reports(self) -> None:
        a = self.git("rev-parse", "HEAD")
        self.install()
        b = self.commit_files({"README.md": "B\n"}, "B")
        self.install()
        c = self.commit_files({"README.md": "C\n"}, "C")
        orig = self.system.launchctl

        def hung_kickstart(args: List[str]) -> Any:
            if args[0] == "kickstart":
                raise subprocess.TimeoutExpired(["launchctl", *args], 60)
            return orig(args)
        self.system.launchctl = hung_kickstart  # type: ignore[assignment]
        self.assertEqual(self.install(), 0)
        self.assertEqual(self.exports(), sorted([b, c]))
        self.assertNotIn(a, self.exports())
        out = self.out()
        self.assertIn("launchctl kickstart failed (TimeoutExpired)", out)
        self.assertIn(f"installed commit {c[:12]}", out)

    def test_rolling_back_to_the_previous_commit_works(self) -> None:
        first = self.git("rev-parse", "HEAD")
        self.install()
        second = self.commit_files({"README.md": "two\n"}, "second")
        self.install()
        self.install(rev=first)
        self.assertEqual(os.readlink(self.layout.current), first)
        self.assertEqual(os.readlink(self.layout.previous), second)
        self.assertEqual(sorted(os.listdir(self.layout.app_dir)), sorted([first, second, "current", "previous"]))

    def test_a_half_written_export_is_never_left_as_an_export(self) -> None:
        self.git("rm", "-q", "scripts/owners_dispatch.py")
        self.git("commit", "-q", "-m", "drop the dispatcher")
        with self.assertRaises(svc.ServiceError) as caught:
            self.install()
        self.assertIn("no tartci owners dispatcher", str(caught.exception))
        self.assertEqual(os.listdir(self.layout.app_dir), [])  # staging removed, nothing named by a sha
        self.assertFalse(os.path.lexists(self.layout.plist))

    def test_a_commit_without_the_template_is_refused(self) -> None:
        self.git("rm", "-q", svc.TEMPLATE_RELATIVE)
        self.git("commit", "-q", "-m", "no template")
        with self.assertRaises(svc.ServiceError) as caught:
            self.install()
        self.assertIn("predates", str(caught.exception))
        self.assertFalse(os.path.exists(self.layout.root))

    def test_a_bad_or_missing_config_is_refused_with_exit_code_2_before_any_change(self) -> None:
        with self.assertRaises(svc.ServiceError) as caught:
            self.install(config=str(self.tmp / "missing.toml"))
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("refusing to install", str(caught.exception))

        def broken(path: str) -> Any:
            return od.parse_config({"owners": []})

        with self.assertRaises(svc.ServiceError) as caught:
            svc.install(self.layout, self.checkout, self.config, "HEAD", False, run=self.system, which=self.tools.get,
                        uid=501, log=self.lines.append, load_config=broken)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.system.calls, [])
        self.assertFalse(os.path.exists(self.layout.root))

    def test_without_the_forwarder_login_install_is_refused_with_exit_code_2_before_any_change(self) -> None:
        import owners_push  # noqa: PLC0415

        seen: List[str] = []

        def missing(config_dir: str) -> str:
            seen.append(config_dir)
            raise owners_push.PushError("cannot read the forwarder login /x/hosts.yml: No such file")

        self.forwarder_login = missing  # type: ignore[assignment]
        with self.assertRaises(svc.ServiceError) as caught:
            self.install()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("the pool needs the forwarder login", str(caught.exception))
        self.assertIn("/x/hosts.yml", str(caught.exception))
        self.assertEqual(seen, ["~/.config/tartci/gh-webhooks"])
        self.assertEqual(self.system.calls, [])
        self.assertFalse(os.path.exists(self.layout.root))

    def test_a_config_with_a_removed_polling_key_is_refused_with_exit_code_2(self) -> None:
        for extra in ({"push": False}, {"push": True}, {"reconcile_seconds": 3600}):
            with self.subTest(extra=extra):
                self.CONFIG = dict(type(self).CONFIG, **extra)
                with self.assertRaises(svc.ServiceError) as caught:
                    self.install()
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("was removed", str(caught.exception))
                self.assertEqual(self.system.calls, [])
                self.assertFalse(os.path.exists(self.layout.root))

    def test_a_failing_gh_login_refuses_under_the_scrubbed_environment(self) -> None:
        self.system.gh_ok = False
        seen: Dict[str, Any] = {}
        real = self.system

        def spy(argv: List[str], **kw: Any) -> Any:
            if argv[0] == "gh":
                seen["env"] = kw.get("env")
            return real(argv, **kw)

        with self.assertRaises(svc.ServiceError) as caught:
            svc.install(self.layout, self.checkout, self.config, "HEAD", False, run=spy, which=self.tools.get, uid=501,
                        log=self.lines.append, load_config=self.load_config)
        self.assertIn("auth status", str(caught.exception))
        self.assertNotIn("GH_TOKEN", seen["env"])
        self.assertNotIn("GITHUB_TOKEN", seen["env"])
        self.assertFalse(os.path.exists(self.layout.root))

    def test_missing_tart_or_gh_is_refused(self) -> None:
        del self.tools["tart"]
        with self.assertRaises(svc.ServiceError):
            self.install()

    def test_install_as_root_is_refused(self) -> None:
        with self.assertRaises(svc.ServiceError):
            svc.install(self.layout, self.checkout, self.config, "HEAD", False, run=self.system, which=self.tools.get,
                        uid=0, log=self.lines.append, load_config=self.load_config)

    def test_not_a_git_checkout_is_refused(self) -> None:
        with self.assertRaises(svc.ServiceError):
            svc.install(self.layout, str(self.tmp / "home"), self.config, "HEAD", False, run=self.system,
                        which=self.tools.get, uid=501, log=self.lines.append, load_config=self.load_config)

    def test_dry_run_prints_the_plist_and_actions_and_changes_nothing(self) -> None:
        self.assertEqual(self.install(dry_run=True), 0)
        text = self.out()
        self.assertIn("dry-run", text)
        self.assertIn("com.danielraffel.tartci.owners", text)
        self.assertIn("<key>ExitTimeOut</key>", text)
        self.assertIn(f"launchctl bootout {svc.LABEL}", text)
        self.assertFalse(os.path.exists(self.layout.root))
        self.assertFalse(os.path.exists(self.layout.plist))
        self.assertEqual(self.system.launchctl_calls(), [])

    def test_an_old_agent_is_booted_out_and_waited_for_before_the_plist_is_replaced(self) -> None:
        self.install()
        orig_launchctl = self.system.launchctl
        # launchctl bootout returns at once; the job leaves launchd a few polls later
        state = {"booted_out": False, "polls": 0, "bootstrapped_while_loaded": False}

        def launchctl(args: List[str]) -> Any:
            if args[0] == "bootout":
                state["booted_out"] = True
                return proc(0)
            if args[0] == "print" and state["booted_out"] and self.system.loaded:
                state["polls"] += 1
                if state["polls"] > 3:
                    self.system.loaded = False
                    state["booted_out"] = False
                    return proc(113)
            if args[0] == "bootstrap" and state["booted_out"]:
                state["bootstrapped_while_loaded"] = True
            return orig_launchctl(args)

        self.system.launchctl = launchctl  # type: ignore[assignment]
        self.commit_files({"README.md": "two\n"}, "second")
        start = self.now
        self.install()
        self.assertGreater(self.now, start)  # it slept while waiting
        self.assertFalse(state["bootstrapped_while_loaded"])
        self.assertTrue(self.system.loaded)  # re-bootstrapped after it was gone

    def test_an_old_agent_that_will_not_leave_blocks_the_replacement(self) -> None:
        self.install()
        before = Path(self.layout.plist).read_bytes()
        self.commit_files({"README.md": "two\n"}, "second")
        orig = self.system.launchctl
        self.system.launchctl = lambda args: proc(0) if args[0] == "bootout" else orig(args)  # type: ignore[assignment]
        with self.assertRaises(svc.ServiceError) as caught:
            self.install()
        self.assertIn("still loaded", str(caught.exception))
        self.assertEqual(Path(self.layout.plist).read_bytes(), before)  # the running agent keeps its plist
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(os.path.dirname(self.layout.plist))))

    def test_a_plist_plutil_rejects_stops_before_the_old_agent_is_touched(self) -> None:
        self.install()
        self.system.lint_ok = False
        self.commit_files({"README.md": "two\n"}, "second")
        with self.assertRaises(svc.ServiceError):
            self.install()
        self.assertEqual(self.system.launchctl_calls(),
                         [["bootstrap", "gui/501", self.layout.plist], ["kickstart", f"gui/501/{svc.LABEL}"]])  # no 2nd bootout
        self.assertTrue(self.system.loaded)

    def test_bootstrap_failure_and_a_job_that_is_not_running_are_reported(self) -> None:
        self.system.loaded = False
        orig = self.system.launchctl
        self.system.launchctl = lambda args: proc(5, "", "Bootstrap failed: 5") if args[0] == "bootstrap" else orig(args)  # type: ignore[assignment]
        with self.assertRaises(svc.ServiceError) as caught:
            self.install()
        self.assertIn("bootstrap", str(caught.exception))

    def test_a_loaded_but_not_running_job_is_an_error_after_waiting(self) -> None:
        self.system.running = False
        with self.assertRaises(svc.ServiceError) as caught:
            self.install()
        self.assertIn("not running", str(caught.exception))
        self.assertGreaterEqual(self.now, 1000.0 + svc.LOADED_WAIT_SECONDS)

    def test_no_secret_reaches_the_output_or_the_plist(self) -> None:
        self.install()
        blob = self.out() + Path(self.layout.plist).read_text()
        self.assertNotRegex(blob, r"gh[pousr]_[A-Za-z0-9]{20,}|encoded_jit_config|GH_TOKEN|GITHUB_TOKEN")


class ExportSafetyTests(unittest.TestCase):
    def archive(self, tmp: Path, entries: List[Any]) -> str:
        import io
        import tarfile

        path = str(tmp / "a.tar")
        with tarfile.open(path, "w") as tar:
            for info, data in entries:
                tar.addfile(info, io.BytesIO(data) if data is not None else None)
        return path

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="owners-tar-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.dest = self.tmp / "out"
        self.dest.mkdir()

    def member(self, name: str, kind: bytes = b"0", data: bytes = b"x", link: str = "") -> Any:
        import tarfile

        info = tarfile.TarInfo(name)
        info.type = kind
        info.mode = 0o644
        info.linkname = link
        info.size = len(data) if kind == b"0" else 0
        return info, (data if kind == b"0" else None)

    def test_traversal_links_and_special_files_are_refused(self) -> None:
        for entry in (self.member("../evil"), self.member("a/../../evil"), self.member("/etc/evil"),
                      self.member("lnk", kind=b"2", link="/etc/passwd"), self.member("hard", kind=b"1", link="x"),
                      self.member("fifo", kind=b"6")):
            with self.assertRaises(svc.ServiceError, msg=entry[0].name):
                svc._safe_extract(self.archive(self.tmp, [entry]), str(self.dest))
        self.assertEqual(os.listdir(self.dest), [])
        self.assertFalse((self.tmp / "evil").exists())

    def test_regular_files_and_directories_extract_with_the_fixed_modes(self) -> None:
        svc._safe_extract(self.archive(self.tmp, [self.member("d", kind=b"5"), self.member("d/f.txt")]), str(self.dest))
        self.assertEqual(stat.S_IMODE(os.stat(self.dest / "d").st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.dest / "d" / "f.txt").st_mode), 0o444)
        svc.remove_tree(str(self.dest))  # a read-only tree is removable
        self.assertFalse(self.dest.exists())

    def test_remove_tree_unlinks_a_symlink_without_following_it(self) -> None:
        target = self.tmp / "keep"
        target.mkdir()
        (target / "f").write_text("x")
        link = self.tmp / "ln"
        os.symlink(target, link)
        svc.remove_tree(str(link))
        self.assertFalse(os.path.lexists(link))
        self.assertTrue((target / "f").exists())


class UninstallTests(ServiceCase):
    def record_child(self, name: str, pid: int, ended: bool = False) -> None:
        state = os.path.join(self.layout.root, "state", name)
        os.makedirs(state, exist_ok=True)
        meta: Dict[str, Any] = {"name": name, "pid": pid, "token": "t"}
        if ended:
            meta["ended"] = 123
        Path(state, od.CHILD_META).write_text(json.dumps(meta))

    def test_another_lanes_linux_vms_are_not_the_pools_leftovers(self) -> None:
        self.install()
        self.record_child("tartci-owners-x1-1-1", pid=4343, ended=True)
        self.system.vms += ["linux-ephr-9999-1", "linux-ephr-4343-2", "pulp-linux-vm", "owners-linux"]
        self.assertEqual(svc.leftover_vms(self.layout, self.system), ["linux-ephr-4343-2"])  # 9999 is a native lane's
        self.system.vms.remove("linux-ephr-4343-2")
        self.assertEqual(self.uninstall(), 0)  # a native lane's VM sharing TART_HOME does not fail the uninstall
        self.assertNotIn("linux-ephr-9999-1", self.out())

    def test_a_pool_macos_clone_is_a_leftover_without_any_recorded_child(self) -> None:
        self.install()
        self.system.vms += ["tartci-owners-m123451-4242-1"]
        self.assertEqual(svc.leftover_vms(self.layout, self.system), ["tartci-owners-m123451-4242-1"])

    def test_uninstall_boots_out_removes_the_plist_and_reports_a_clean_host(self) -> None:
        self.install()
        self.assertEqual(self.uninstall(), 0)
        self.assertFalse(self.system.loaded)
        self.assertFalse(os.path.lexists(self.layout.plist))
        self.assertIn("no tartci-owners-* process or VM is left", self.out())
        self.assertTrue(os.path.isdir(self.layout.app_dir))  # state and exports stay without --purge
        self.assertIn("--purge", self.out())

    def test_uninstall_stops_forwarders_a_crashed_dispatcher_left_behind(self) -> None:
        import owners_push  # noqa: PLC0415
        self.install()
        calls: List[str] = []

        def stop_recorded(root: str, log: Any = None) -> List[str]:
            calls.append(root)
            return ["repo me/a (pid 4001)"]

        with mock.patch.object(owners_push, "stop_recorded", stop_recorded):
            self.assertEqual(self.uninstall(), 0)
        self.assertEqual(calls, [self.layout.root])
        self.assertIn("forwarder stopped a leftover: repo me/a (pid 4001)", self.out())

    def test_uninstall_when_nothing_is_installed_is_a_clean_no_op(self) -> None:
        self.assertEqual(self.uninstall(), 0)
        self.assertIn("was not loaded", self.out())

    def test_it_waits_for_the_dispatcher_to_leave_launchd(self) -> None:
        self.install()
        polls = {"n": 0}
        orig = self.system.launchctl

        def launchctl(args: List[str]) -> Any:
            if args[0] == "bootout":
                return proc(0)  # asynchronous: returns before the job is gone
            if args[0] == "print" and self.system.loaded:
                polls["n"] += 1
                if polls["n"] > 4:
                    self.system.loaded = False
                    return proc(113)
            return orig(args)

        self.system.launchctl = launchctl  # type: ignore[assignment]
        start = self.now
        self.assertEqual(self.uninstall(), 0)
        self.assertGreater(self.now, start)
        self.assertIn("waiting for the dispatcher", self.out())

    def test_a_job_that_never_leaves_keeps_its_plist_and_fails(self) -> None:
        self.install()
        orig = self.system.launchctl
        self.system.launchctl = lambda args: proc(0) if args[0] == "bootout" else orig(args)  # type: ignore[assignment]
        with self.assertRaises(svc.ServiceError):
            self.uninstall()
        self.assertTrue(os.path.lexists(self.layout.plist))

    def test_leftover_processes_and_vms_are_reported_not_deleted_and_fail_the_exit_code(self) -> None:
        self.install()
        self.record_child("tartci-owners-x1-1-1", pid=4343, ended=True)  # the pool's Linux child 4343
        self.system.vms += ["tartci-owners-m123451-4242-1", "linux-ephr-4343-2"]
        self.system.ps = ("  501 /usr/bin/python3 tart run tartci-owners-m123451-4242-1 --no-graphics\n"
                          "  77712 /bin/zsh -c unrelated\n")
        self.assertEqual(self.uninstall(), 1)
        text = self.out()
        self.assertIn("LEFTOVER VM tartci-owners-m123451-4242-1", text)
        self.assertIn("LEFTOVER VM linux-ephr-4343-2", text)
        self.assertIn("LEFTOVER process pid 501", text)
        self.assertNotIn("unrelated", text)
        self.assertNotIn("owners-macos", text)  # goldens and base images are never listed as leftovers
        self.assertNotIn("ghcr.io", text)
        self.assertFalse([c for c in self.system.calls if c[:2] == ["tart", "delete"] or c[:2] == ["tart", "stop"]])

    def test_children_that_finish_cleaning_up_during_the_wait_are_not_leftovers(self) -> None:
        self.install()
        self.system.vms.append("tartci-owners-m123451-4242-1")
        self.system.ps = "  501 tart run tartci-owners-m123451-4242-1\n"
        start = self.now

        def finish() -> None:  # the child deletes its VM and exits 20 s after the dispatcher was stopped
            if self.now >= start + 20:
                self.system.vms.remove("tartci-owners-m123451-4242-1") if "tartci-owners-m123451-4242-1" in self.system.vms else None
                self.system.ps = ""

        orig_sleep = self.sleep

        def sleep(seconds: float) -> None:
            orig_sleep(seconds)
            finish()

        self.sleep = sleep  # type: ignore[assignment]
        self.assertEqual(self.uninstall(), 0)
        text = self.out()
        self.assertIn("waiting up to 150 s", text)
        self.assertNotIn("LEFTOVER", text)
        self.assertLess(self.now, start + 60)

    def test_leftovers_that_persist_are_reported_after_the_full_grace(self) -> None:
        self.install()
        self.system.vms.append("tartci-owners-m1-2-3")
        start = self.now
        self.assertEqual(self.uninstall(), 1)
        self.assertGreaterEqual(self.now, start + od.CHILD_TERM_GRACE_SECONDS)
        self.assertIn("LEFTOVER VM tartci-owners-m1-2-3", self.out())

    def test_recorded_children_with_a_live_pid_are_leftovers(self) -> None:
        state = os.path.join(self.layout.root, "state", od.CHILD_PREFIX + "m1-1-1")
        os.makedirs(state)
        Path(state, od.CHILD_META).write_text(json.dumps({"pid": 99, "pid_started": "x", "name": "tartci-owners-m1-1-1"}))
        done = os.path.join(self.layout.root, "state", od.CHILD_PREFIX + "m1-1-2")
        os.makedirs(done)
        Path(done, od.CHILD_META).write_text(json.dumps({"pid": 98, "ended": 123, "name": "tartci-owners-m1-1-2"}))
        found = svc.leftover_processes(self.layout, self.system, alive=lambda pid, started="", strict=False: pid == 99)
        self.assertEqual(found, ["pid 99 (tartci-owners-m1-1-1)"])

    def test_purge_removes_pool_state_and_launchd_logs_only(self) -> None:
        self.install()
        keep_config = Path(self.home, ".config", "tartci", "owners.toml")
        keep_key = Path(self.home, ".config", "tartci", "owners-vm-ed25519")
        keep_vm = Path(self.home, ".tart", "vms", "owners-macos", "config.json")
        for path in (keep_config, keep_key, keep_vm):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep")
        for sub in ("state", "logs", "reap", "cache"):
            Path(self.layout.root, sub).mkdir(parents=True, exist_ok=True)
            Path(self.layout.root, sub, "f").write_text("x")
        Path(self.layout.log_dir).mkdir(parents=True, exist_ok=True)
        Path(self.layout.log_dir, "owners.out.log").write_text("x")
        Path(self.layout.log_dir, "other-agent.log").write_text("x")
        self.assertEqual(self.uninstall(purge=True), 0)
        self.assertFalse(os.path.exists(self.layout.root))
        self.assertFalse(Path(self.layout.log_dir, "owners.out.log").exists())
        self.assertTrue(Path(self.layout.log_dir, "other-agent.log").exists())
        for path in (keep_config, keep_key, keep_vm):
            self.assertEqual(path.read_text(), "keep")

    def test_purge_is_skipped_while_leftovers_remain(self) -> None:
        self.install()
        self.system.vms.append("tartci-owners-m1-2-3")
        self.assertEqual(self.uninstall(purge=True), 1)
        self.assertTrue(os.path.isdir(self.layout.app_dir))
        self.assertIn("not purging", self.out())

    def test_purge_refuses_a_root_that_is_not_below_home(self) -> None:
        for root in (self.home, "/", str(self.tmp)):
            with self.assertRaises(svc.ServiceError):
                svc.purge(svc.Layout(home=self.home, root=root), self.lines.append)

    def test_purge_refuses_home_dot_tartci_and_a_root_without_a_pool_marker(self) -> None:
        home = Path(self.home)
        for root in (home / ".tartci", home / "Documents", home / ".tartci" / "owners-elsewhere"):
            root.mkdir(parents=True, exist_ok=True)
            (root / "state").mkdir(exist_ok=True)
            (root / "state" / "mine.txt").write_text("precious")  # a state dir is not a marker by itself
            (root / "app").mkdir(exist_ok=True)
            (root / "app" / "also-mine").write_text("precious")
            with self.assertRaises(svc.ServiceError, msg=str(root)):
                svc.purge(svc.Layout(home=self.home, root=str(root)), self.lines.append)
            self.assertEqual((root / "state" / "mine.txt").read_text(), "precious")
            self.assertEqual((root / "app" / "also-mine").read_text(), "precious")
        with self.assertRaises(svc.ServiceError):
            svc.purge(svc.Layout(home=self.home, root=self.home), self.lines.append)

    def test_a_marked_dot_tartci_is_still_refused_and_a_marked_pool_root_is_purged(self) -> None:
        dot = Path(self.home, ".tartci")
        (dot / "state").mkdir(parents=True)
        (dot / "state" / "dispatcher.json").write_text("{}")
        with self.assertRaises(svc.ServiceError):
            svc.purge(svc.Layout(home=self.home, root=str(dot)), self.lines.append)
        self.assertTrue((dot / "state" / "dispatcher.json").exists())
        for marker in (("state", "dispatcher.json"), ("app", "current")):
            root = Path(self.home, ".tartci", "pool-" + marker[0])
            (root / marker[0]).mkdir(parents=True)
            target = root / marker[0] / marker[1]
            if marker[1] == "current":
                os.symlink("0" * 40, target)
            else:
                target.write_text("{}")
            svc.purge(svc.Layout(home=self.home, root=str(root)), self.lines.append)
            self.assertFalse(root.exists())

    def test_a_case_variant_of_dot_tartci_is_refused_on_darwin(self) -> None:
        dot = Path(self.home, ".TARTCI")
        (dot / "state").mkdir(parents=True)
        (dot / "state" / "dispatcher.json").write_text("{}")  # even a marked one
        with mock.patch.object(svc.sys, "platform", "darwin"):
            with self.assertRaises(svc.ServiceError):
                svc.purge(svc.Layout(home=self.home, root=str(dot)), self.lines.append)
        self.assertTrue((dot / "state" / "dispatcher.json").exists())

    def test_a_root_left_by_a_failed_first_install_is_purgeable(self) -> None:
        root = Path(self.home, ".tartci", "owners")
        export = root / "app" / ("a" * 40)
        export.mkdir(parents=True)
        (export / od.COMMIT_FILE).write_text("a" * 40 + "\n")
        svc.purge(svc.Layout(home=self.home, root=str(root)), self.lines.append)
        self.assertFalse((root / "app").exists())
        not_sha = Path(self.home, ".tartci", "other")
        (not_sha / "app" / "notasha").mkdir(parents=True)
        (not_sha / "app" / "notasha" / od.COMMIT_FILE).write_text("x")
        with self.assertRaises(svc.ServiceError):
            svc.purge(svc.Layout(home=self.home, root=str(not_sha)), self.lines.append)

    def test_purge_through_a_symlinked_root_is_judged_by_where_it_points(self) -> None:
        outside = self.tmp / "outside"
        (outside / "state").mkdir(parents=True)
        (outside / "state" / "dispatcher.json").write_text("{}")
        link = Path(self.home, ".tartci", "owners-link")
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, link)
        with self.assertRaises(svc.ServiceError):
            svc.purge(svc.Layout(home=self.home, root=str(link)), self.lines.append)
        self.assertTrue((outside / "state" / "dispatcher.json").exists())

    def test_uninstall_purge_with_a_bad_root_refuses_before_stopping_anything(self) -> None:
        self.install()
        bad = svc.Layout(home=self.home, root=self.home)
        calls = len(self.system.calls)
        with self.assertRaises(svc.ServiceError):
            svc.uninstall(bad, True, run=self.system, uid=501, sleep=self.sleep, clock=lambda: self.now,
                          log=self.lines.append, alive=lambda *a, **k: False)
        self.assertEqual(len(self.system.calls), calls)  # no launchctl, no tart: nothing was touched
        self.assertTrue(os.path.lexists(self.layout.plist))

    def test_uninstall_purge_of_a_root_that_does_not_exist_is_a_no_op(self) -> None:
        self.assertEqual(self.uninstall(purge=True), 0)
        self.assertIn("nothing to purge", self.out())

    def test_vm_leftover_pattern_never_matches_goldens_or_bases(self) -> None:
        for name in ("owners-macos", "owners-linux", "ghcr.io/cirruslabs/macos-tahoe-base:latest", "pulp-linux-vm",
                     "linux-ephr", "base", "tartci-owner"):
            self.assertIsNone(svc.VM_LEFTOVER.match(name), name)
        for name in ("tartci-owners-m123451-4242-1", "linux-ephr-4343-2"):
            self.assertTrue(svc.VM_LEFTOVER.match(name), name)


class StatusTests(ServiceCase):
    def test_not_installed(self) -> None:
        self.assertEqual(svc.status_lines(self.layout, self.system, uid=501),
                         ["service: not installed (`tartci owners install`)"])

    def test_installed_status_shows_the_pinned_commit_and_launchd_state(self) -> None:
        sha = self.git("rev-parse", "HEAD")
        self.install()
        lines = svc.status_lines(self.layout, self.system, uid=501)
        self.assertIn(f"pinned commit {sha[:12]}", lines[0])
        self.assertIn("state=running", lines[-1])
        self.assertIn("pid=4242", lines[-1])

    def test_a_current_link_that_disagrees_with_the_plist_is_called_out(self) -> None:
        first = self.git("rev-parse", "HEAD")
        self.install()
        second = self.commit_files({"README.md": "two\n"}, "second")
        svc.export_commit(self.layout, self.checkout, second, self.system)
        svc.point_current(self.layout, second)
        lines = svc.status_lines(self.layout, self.system, uid=501)
        self.assertTrue(any("the plist runs " + first[:12] in line for line in lines), lines)

    def test_installed_but_not_loaded(self) -> None:
        self.install()
        self.system.loaded = False
        self.assertIn("not loaded", svc.status_lines(self.layout, self.system, uid=501)[-1])


class CommitFileTests(unittest.TestCase):
    def test_recorded_commit_reads_only_a_full_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(od.recorded_commit(tmp))
            Path(tmp, "COMMIT").write_text("abc123\n")
            self.assertIsNone(od.recorded_commit(tmp))
            Path(tmp, "COMMIT").write_text("c" * 40 + "\n")
            self.assertEqual(od.recorded_commit(tmp), "c" * 40)


if __name__ == "__main__":
    unittest.main()
