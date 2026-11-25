#!/usr/bin/env python3
"""Tests for the dispatcher's periodic runner-registration reap (owners_dispatch.ReapPass) and for
the `vm_reap.py --runners-only` mode it runs. No GitHub call, no VM, no process is started."""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import time
import sys
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import owners_dispatch as od  # noqa: E402
import vm_reap  # noqa: E402
from test_owners_dispatch import FakeProc, SchedulerHarness  # noqa: E402

REPOS = ["org1/a", "org1/b", "org1/c"]


class ReapHarness(SchedulerHarness):
    def reaper(self, base_env: Dict[str, str] = None, **kw: Any) -> od.ReapPass:  # type: ignore[assignment]
        cfg = od.parse_config({"owners": ["org1"], "gh_cli": "ghx"})
        return od.ReapPass(cfg, self.root, "/opt/tartci", spawn=self.spawn, clock=lambda: self.now,
                           base_env=base_env if base_env is not None else {"PATH": "/opt/homebrew/bin:/usr/bin"},
                           home="/home/u", python="/py/bin/python3", **kw)

    def run_pass(self, reaper: od.ReapPass, served: List[str], busy: List[str] = (), code: int = 0,  # type: ignore[assignment]
                 digest: Any = None) -> str:
        """Start one pass (advancing the fake clock to when it is due) and finish it."""
        self.now = max(self.now, reaper.next_at)
        repo = reaper.start(served, busy)
        self.assertIsNotNone(repo)
        proc = self.spawned[-1][3]
        proc.code = code
        if digest is not None:
            Path(reaper.out_path).write_text(json.dumps(digest))
        reaper.poll(lambda message: None)
        return str(repo)


class ReapArgvEnvTests(ReapHarness):
    def test_argv_runs_vm_reap_for_one_repository_in_registrations_only_mode(self) -> None:
        argv = self.reaper().argv("org1/a")
        self.assertEqual(argv[:2], ["/py/bin/python3", "/opt/tartci/scripts/vm_reap.py"])
        for flag in ("--runners-only", "--fix", "--json"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--scopes") + 1], "repository")  # the pool never registers at org scope
        self.assertEqual(argv[argv.index("--repo") + 1], "org1/a")
        self.assertEqual(argv[argv.index("--prefixes") + 1], "tartci-owners-,linux-ephr-")
        self.assertEqual(argv[argv.index("--own-name-prefixes") + 1], "tartci-owners-,linux-ephr-")
        self.assertEqual(argv[argv.index("--min-offline-secs") + 1], "900")
        self.assertEqual(argv[argv.index("--min-offline-passes") + 1], "3")
        self.assertNotIn("--dry-run", argv)

    def test_each_repository_has_its_own_offline_clock_under_the_pool_root(self) -> None:
        reaper = self.reaper()
        roots = {repo: reaper.argv(repo)[reaper.argv(repo).index("--state-root") + 1] for repo in REPOS}
        self.assertEqual(len(set(roots.values())), 3)  # a shared clock would be rewritten by every pass
        for repo, state_root in roots.items():
            self.assertEqual(state_root, os.path.join(self.root, "reap", repo.replace("/", "__")))

    def test_env_is_the_childrens_scrubbed_one_and_names_the_prefixes(self) -> None:
        base = {"PATH": "/opt/homebrew/bin:/usr/bin", "HOME": "/home/u", "GH_TOKEN": "t", "GITHUB_TOKEN": "t",
                "TARTCI_STATE_ROOT": "/elsewhere", "TARTCI_REAP_PREFIXES": "pulp-", "TARTCI_REAP_MIN_OFFLINE_PASSES": "1",
                "SSH_AUTH_SOCK": "/s", "TARTCI_RUNNER_REPO": "x/y"}
        env = self.reaper(base_env=base).env()
        self.assertEqual(env["TARTCI_REAP_OWN_NAME_PREFIXES"], "tartci-owners-,linux-ephr-")
        self.assertEqual(env["TARTCI_GH_CLI"], "ghx")
        self.assertEqual(env["TART_HOME"], "/home/u/.tart")
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "TARTCI_STATE_ROOT", "TARTCI_REAP_PREFIXES",
                     "TARTCI_REAP_MIN_OFFLINE_PASSES", "SSH_AUTH_SOCK", "TARTCI_RUNNER_REPO"):
            self.assertNotIn(name, env)
        self.assertEqual(env["PATH"], base["PATH"])

    def test_argv_parses_with_the_real_vm_reap_parser(self) -> None:
        argv = self.reaper().argv("org1/a")
        args = vm_reap.parse_args(argv[2:])
        self.assertTrue(args.runners_only and args.fix and args.json)
        self.assertEqual(args.scopes, "repository")
        self.assertEqual((args.repo, args.prefixes, args.own_name_prefixes),
                         ("org1/a", od.REAP_PREFIXES, od.REAP_PREFIXES))
        self.assertTrue(vm_reap.scoped_view(args))  # no launchctl lane discovery, no whole-host state


class ReapScheduleTests(ReapHarness):
    def test_round_robin_visits_every_repository_then_wraps(self) -> None:
        reaper = self.reaper()
        order = [self.run_pass(reaper, REPOS) for _ in range(7)]
        self.assertEqual(order, ["org1/a", "org1/b", "org1/c", "org1/a", "org1/b", "org1/c", "org1/a"])

    def test_the_cursor_follows_a_changing_served_set(self) -> None:
        reaper = self.reaper()
        self.assertEqual(self.run_pass(reaper, REPOS), "org1/a")
        self.assertEqual(self.run_pass(reaper, ["org1/b", "org1/c", "org1/d"]), "org1/b")
        self.assertEqual(self.run_pass(reaper, ["org1/a", "org1/d"]), "org1/d")  # b and c gone: next after b
        self.assertEqual(self.run_pass(reaper, ["org1/a", "org1/d"]), "org1/a")  # wraps

    def test_first_pass_waits_and_passes_are_spaced_by_the_interval(self) -> None:
        reaper = self.reaper()
        self.assertIsNone(reaper.start(REPOS, []))  # not before REAP_FIRST_DELAY_SECONDS
        self.now += od.REAP_FIRST_DELAY_SECONDS
        self.assertEqual(reaper.start(REPOS, []), "org1/a")
        self.spawned[-1][3].code = 0
        reaper.poll(lambda message: None)
        self.now += od.REAP_EVERY_SECONDS - 1
        self.assertIsNone(reaper.start(REPOS, []))
        self.now += 1
        self.assertEqual(reaper.start(REPOS, []), "org1/b")
        self.assertEqual(len(self.spawned), 2)

    def test_no_second_pass_while_one_runs(self) -> None:
        reaper = self.reaper()
        self.now += 10_000
        self.assertEqual(reaper.start(REPOS, []), "org1/a")
        self.now += 10_000
        self.assertIsNone(reaper.start(REPOS, []))
        self.assertEqual(len(self.spawned), 1)

    def test_a_repository_with_a_live_child_is_skipped_and_none_eligible_spawns_nothing(self) -> None:
        reaper = self.reaper()
        self.now += 10_000
        self.assertEqual(reaper.start(REPOS, ["org1/a"]), "org1/b")
        self.spawned[-1][3].code = 0
        reaper.poll(lambda message: None)
        self.now += 10_000
        self.assertEqual(reaper.start(REPOS, ["org1/a", "org1/c"]), "org1/b")  # only b is free
        self.spawned[-1][3].code = 0
        reaper.poll(lambda message: None)
        self.now += 10_000
        before = len(self.spawned)
        self.assertIsNone(reaper.start(REPOS, REPOS))
        self.assertEqual(len(self.spawned), before)
        self.assertGreater(reaper.next_at, self.now)  # asks again after an interval, not every tick

    def test_busy_comparison_ignores_case_and_a_malformed_name_is_never_passed_on(self) -> None:
        reaper = self.reaper()
        self.assertEqual(reaper.pick(["Org1/A", "org1/b"], ["org1/a"]), "org1/b")
        self.assertIsNone(reaper.pick(["--repo=x", "a b/c", "../x", ""], []))

    def test_a_pass_that_overruns_is_killed_and_collected(self) -> None:
        reaper = self.reaper()
        self.now += 10_000
        reaper.start(REPOS, [])
        self.now += od.REAP_TIMEOUT_SECONDS + 1
        reaper.poll(lambda message: None)
        self.assertEqual(self.sent, [(self.spawned[0][3].pid, ("group", signal.SIGKILL))])
        self.assertTrue(reaper.running)  # collected on the next poll, once it has really exited
        self.spawned[0][3].code = -9
        reaper.poll(lambda message: None)
        self.assertFalse(reaper.running)
        self.assertEqual(reaper.last_rc, -9)

    def test_stop_terminates_a_pass_in_flight(self) -> None:
        reaper = self.reaper()
        self.now += 10_000
        reaper.start(REPOS, [])
        reaper.stop()
        self.assertEqual(self.sent, [(self.spawned[0][3].pid, ("group", signal.SIGTERM))])
        self.assertFalse(reaper.running)

    def test_finished_pass_logs_what_it_deleted_and_counts_it(self) -> None:
        reaper = self.reaper()
        lines: List[str] = []
        self.now += 10_000
        reaper.start(REPOS, [])
        self.spawned[-1][3].code = 0
        Path(reaper.out_path).write_text(json.dumps({"fixed": ["github_runner_deleted:tartci-owners-m1-2-3:77"],
                                                     "problems": []}))
        reaper.poll(lines.append)
        self.assertEqual(lines, ["reap org1/a: github_runner_deleted:tartci-owners-m1-2-3:77"])
        self.assertEqual((reaper.passes, reaper.fixed_total, reaper.last_repo), (1, 1, "org1/a"))
        self.assertEqual(reaper.summary()["fixed"], 1)

    def test_a_failed_pass_is_logged_and_does_not_stop_the_schedule(self) -> None:
        reaper = self.reaper()
        lines: List[str] = []
        self.now += 10_000
        reaper.start(REPOS, [])
        self.spawned[-1][3].code = 2
        Path(reaper.out_path).write_text(json.dumps({"fixed": [], "problems": ["github_unreadable:boom"]}))
        reaper.poll(lines.append)
        self.assertIn("rc=2", lines[0])
        self.assertEqual(self.run_pass(reaper, REPOS), "org1/b")

    def test_a_spawn_that_raises_waits_a_full_interval_and_moves_to_the_next_repository(self) -> None:
        calls: List[str] = []

        def failing(argv: List[str], env: Dict[str, str], out: str, err: Any = None) -> Any:
            calls.append(argv[argv.index("--repo") + 1])
            raise OSError(24, "Too many open files")

        reaper = self.reaper()
        reaper.spawn = failing
        self.now += 10_000
        with self.assertRaises(OSError):
            reaper.start(REPOS, [])
        self.assertFalse(reaper.running)
        self.assertEqual(reaper.next_at, self.now + reaper.interval)
        self.now += reaper.interval - 1  # a tick a second later retries nothing
        self.assertIsNone(reaper.start(REPOS, []))
        self.assertEqual(calls, ["org1/a"])
        self.now += 1
        with self.assertRaises(OSError):
            reaper.start(REPOS, [])
        self.assertEqual(calls, ["org1/a", "org1/b"])  # and a poison repository does not block the ring

    def test_stderr_goes_to_its_own_file_so_the_json_digest_stays_parseable(self) -> None:
        reaper = self.reaper()
        self.now += 10_000
        reaper.start(REPOS, [])
        out_path, err_path = reaper.out_path, reaper.err_path
        self.assertEqual(self.spawn_err_paths[-1], err_path)
        self.assertNotEqual(out_path, err_path)
        self.assertEqual(os.path.basename(err_path), "last.err")
        self.assertEqual(os.path.dirname(err_path), os.path.dirname(out_path))
        Path(err_path).write_text("Traceback...\nWARNING: something on stderr\n")
        Path(out_path).write_text(json.dumps({"fixed": ["github_runner_deleted:linux-ephr-1-2:9"], "problems": []}))
        self.spawned[-1][3].code = 0
        lines: List[str] = []
        reaper.poll(lines.append)
        self.assertEqual(lines, ["reap org1/a: github_runner_deleted:linux-ephr-1-2:9"])  # parsed despite stderr text
        self.assertEqual(reaper.fixed_total, 1)

    def test_a_failed_pass_logs_the_last_stderr_line(self) -> None:
        reaper = self.reaper()
        self.now += 10_000
        reaper.start(REPOS, [])
        Path(reaper.err_path).write_text("first\nImportError: no module named x\n")
        self.spawned[-1][3].code = 1
        lines: List[str] = []
        reaper.poll(lines.append)
        self.assertIn("(no digest)", lines[0])
        self.assertIn("stderr: ImportError: no module named x", lines[0])

    def test_the_real_spawn_keeps_stdout_and_stderr_apart(self) -> None:
        reaper = self.reaper()
        self.now += 10_000
        out_dir = os.path.join(self.root, "logs", "reap")
        od.ensure_private_dir(out_dir)
        out, err = os.path.join(out_dir, "o.json"), os.path.join(out_dir, "o.err")
        script = "import sys; print('{\"fixed\": []}'); print('noise', file=sys.stderr)"
        proc = od.spawn_process([sys.executable, "-c", script], {"PATH": os.environ.get("PATH", "/usr/bin")}, out, err)
        import time as _time
        for _ in range(500):
            if proc.poll() is not None:
                break
            _time.sleep(0.02)
        self.assertEqual(json.loads(Path(out).read_text()), {"fixed": []})
        self.assertEqual(Path(err).read_text().strip(), "noise")
        self.assertEqual(oct(os.stat(err).st_mode & 0o777), "0o600")
        del reaper

    def test_the_output_file_is_private_and_replaced_for_each_pass(self) -> None:
        reaper = self.reaper()
        self.run_pass(reaper, REPOS, digest={"fixed": ["old"]})
        self.now += 10_000
        reaper.start(REPOS, [])
        self.assertFalse(os.path.exists(reaper.out_path))  # the previous digest is gone before the next pass
        self.assertEqual(oct(os.stat(os.path.dirname(reaper.out_path)).st_mode & 0o777), "0o700")


class DispatcherReapTickTests(ReapHarness):
    def dispatcher(self) -> od.Dispatcher:
        disp = self.make_dispatcher({})
        disp.reaper = self.reaper()
        disp.directory.current.served = {name: None for name in REPOS}  # type: ignore[misc]
        self.now += 10_000
        # a child ended badly in each: only such repositories are visited
        disp._reap_candidates.update({name: self.now for name in REPOS})
        return disp

    def assert_blocked(self, disp: od.Dispatcher, why: str) -> None:
        self.assertEqual(disp.reap_blocker(), why)
        disp.reap_tick()
        self.assertEqual(self.spawned, [], why)

    def test_an_idle_healthy_dispatcher_starts_a_pass(self) -> None:
        disp = self.dispatcher()
        self.assertEqual(disp.reap_blocker(), "")
        disp.reap_tick()
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("org1/a", self.spawned[0][0])

    def test_no_pass_while_rate_limited(self) -> None:
        disp = self.dispatcher()
        disp.rate_block_until = self.now + 60
        self.assert_blocked(disp, "rate limited")

    def test_no_pass_below_the_headroom_floor(self) -> None:
        disp = self.dispatcher()
        disp.client.rate.remaining, disp.client.rate.limit = 1000, 5000  # 20% < 25%
        self.assert_blocked(disp, "low rate headroom")
        disp.client.rate.remaining = 2000  # 40%
        self.assertEqual(disp.reap_blocker(), "")

    def test_no_pass_while_github_is_failing(self) -> None:
        disp = self.dispatcher()
        disp.error_since = self.now - 10
        self.assert_blocked(disp, "GitHub unreachable")

    def test_no_pass_once_shutdown_has_begun(self) -> None:
        disp = self.dispatcher()
        disp.request_stop(signal.SIGTERM)
        self.assert_blocked(disp, "stopping")

    def test_repositories_with_children_are_skipped_through_the_scheduler(self) -> None:
        disp = self.dispatcher()
        disp.sched.children["c1"] = types.SimpleNamespace(repo="org1/a")  # type: ignore[assignment]
        disp.reap_tick()
        self.assertIn("org1/b", self.spawned[0][0])

    def test_the_snapshot_reports_the_reap_pass(self) -> None:
        disp = self.dispatcher()
        disp.reap_tick()
        self.assertEqual(disp.snapshot()["reap"]["running"], "org1/a")
        self.assertIsNone(self.make_dispatcher({}).snapshot()["reap"])

    def test_a_raising_spawn_is_attempted_once_per_interval_not_every_tick(self) -> None:
        disp = self.dispatcher()
        spawn = mock.Mock(side_effect=OSError("EMFILE"))
        disp.reaper.spawn = spawn  # type: ignore[union-attr]
        with self.assertRaises(OSError):
            disp.reap_tick()
        for _ in range(5):  # the next five seconds: no retry, so nothing raised and nothing logged
            self.now += 1
            disp.reap_tick()
        self.assertEqual(spawn.call_count, 1)
        self.now += od.REAP_EVERY_SECONDS
        with self.assertRaises(OSError):
            disp.reap_tick()
        self.assertEqual(spawn.call_count, 2)

    def test_a_failing_state_directory_waits_an_interval_and_moves_on(self) -> None:
        disp = self.dispatcher()
        with mock.patch.object(od, "ensure_private_dir", side_effect=OSError("EACCES")) as ensure:
            with self.assertRaises(OSError):
                disp.reap_tick()
            for _ in range(5):  # no retry inside the interval
                self.now += 1
                disp.reap_tick()
            self.assertEqual(ensure.call_count, 1)
        self.now += od.REAP_EVERY_SECONDS
        disp.reap_tick()  # the next pass goes to the next repository
        self.assertIn("org1/b", self.spawned[0][0])

    def test_a_failing_reap_spawn_is_recorded_as_reap_and_cleared_by_a_working_one(self) -> None:
        disp = self.dispatcher()
        with mock.patch.dict(od.LAST_SPAWN_ERROR, {"child": {"at": 1.0, "error": "child broke"}}, clear=True):
            disp.reaper.spawn = mock.Mock(side_effect=OSError("EMFILE"))  # type: ignore[union-attr]
            with self.assertRaises(OSError):
                disp.reap_tick()
            self.assertIn("EMFILE", od.LAST_SPAWN_ERROR["reap"]["error"])
            disp.reaper.spawn = lambda *a: self.spawned.append(a) or types.SimpleNamespace(pid=1, poll=lambda: None)  # type: ignore
            self.now += od.REAP_EVERY_SECONDS
            disp.reap_tick()
            self.assertNotIn("reap", od.LAST_SPAWN_ERROR)
            self.assertEqual(od.LAST_SPAWN_ERROR["child"]["error"], "child broke")  # untouched by reap

    def test_a_reap_failure_never_escapes_the_tick(self) -> None:
        disp = self.dispatcher()
        with mock.patch.object(disp.reaper, "start", side_effect=OSError("EMFILE")):
            disp.tick()
        self.assertGreaterEqual(disp.tick_errors, 1)

    def test_the_spawn_path_is_logged_at_startup_and_a_fallback_warns(self) -> None:
        for libc, warns in ((object(), False), (None, True)):
            disp = self.dispatcher()
            disp.request_stop(signal.SIGTERM)
            disp.sleep = lambda seconds: None
            lines: List[str] = []
            with mock.patch.object(od, "_spawn_libc", return_value=libc), \
                 mock.patch.object(od, "log_line", side_effect=lines.append):
                disp.run()
            up = [line for line in lines if line.startswith("dispatcher up")]
            self.assertEqual(len(up), 1)
            self.assertIn("spawn=disclaimed posix_spawn" if not warns else "spawn=Popen fallback", up[0])
            self.assertEqual(any("WARNING: children are started with plain Popen" in line for line in lines), warns)

    def test_status_shows_the_spawn_path(self) -> None:
        import contextlib
        import io

        disp = self.dispatcher()
        disp.write_snapshot()
        self.assertEqual(disp.snapshot()["spawn"], od.spawn_mode())
        args = types.SimpleNamespace(root=self.root, json=False)
        for libc, expected in ((object(), "child spawn: disclaimed posix_spawn"), (None, "child spawn: Popen fallback")):
            out = io.StringIO()
            with mock.patch.object(od, "_spawn_libc", return_value=libc), \
                 mock.patch.object(od, "service_lines", return_value=[]), contextlib.redirect_stdout(out):
                disp.write_snapshot()  # status reports what the running dispatcher recorded
                od.cmd_status(args)  # type: ignore[arg-type]
            self.assertIn(expected, out.getvalue())

    def test_status_shows_a_failing_spawn(self) -> None:
        import contextlib
        import io

        disp = self.dispatcher()
        args = types.SimpleNamespace(root=self.root, json=False)
        out = io.StringIO()
        with mock.patch.dict(od.LAST_SPAWN_ERROR, {"child": {"at": time.time() - 5, "error": "[Errno 1] disclaim failed"}},
                             clear=True), \
             mock.patch.object(od, "service_lines", return_value=[]), contextlib.redirect_stdout(out):
            disp.write_snapshot()
            od.cmd_status(args)  # type: ignore[arg-type]
        self.assertIn("LAST CHILD SPAWN FAILED", out.getvalue())
        self.assertIn("disclaim failed", out.getvalue())

    def test_shutdown_stops_the_reap_pass(self) -> None:
        disp = self.dispatcher()
        disp.reap_tick()
        disp.request_stop(signal.SIGTERM)
        disp.sleep = lambda seconds: None
        self.assertEqual(disp.run(), 128 + signal.SIGTERM)
        self.assertIn((self.spawned[0][3].pid, ("group", signal.SIGTERM)), self.sent)


class RunnersOnlyTests(unittest.TestCase):
    """`vm_reap.py --runners-only`, driven with the argv the dispatcher builds: only an offline,
    idle registration of the pool's names is deleted, after 3 passes over 900 s, and no VM or
    state file is read, stopped or deleted."""

    T0 = dt.datetime(2026, 10, 15, 4, 0, 0, tzinfo=dt.timezone.utc)

    def setUp(self) -> None:
        import tempfile
        import shutil

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        cfg = od.parse_config({"owners": ["org1"]})
        self.reaper = od.ReapPass(cfg, str(self.tmp), "/opt/tartci", base_env={}, python="/py/python3")

    @staticmethod
    def runner(name: str, status: str = "offline", busy: bool = False, labels: Any = ("self-hosted", "linux-a64"),
               rid: int = 1) -> Dict[str, Any]:
        return {"id": rid, "name": name, "status": status, "busy": busy, "labels": [{"name": n} for n in labels]}

    def passes(self, runners: List[Dict[str, Any]], count: int = 3, step: float = 450.0):
        argv = self.reaper.argv("org1/a")[2:]
        deleted: List[str] = []
        for i in range(count):
            args = vm_reap.parse_args(argv)
            with mock.patch.object(vm_reap, "utcnow", return_value=self.T0 + dt.timedelta(seconds=i * step)), \
                 mock.patch.object(vm_reap.shutil, "which", return_value="/usr/bin/gh"), \
                 mock.patch.object(vm_reap, "tart_vms", side_effect=AssertionError("tart was listed")) as tart_vms, \
                 mock.patch.object(vm_reap, "load_states", side_effect=AssertionError("state files were read")), \
                 mock.patch.object(vm_reap, "delete_vm", side_effect=AssertionError("a VM was deleted")), \
                 mock.patch.object(vm_reap, "unlink_state", side_effect=AssertionError("a state file was removed")), \
                 mock.patch.object(vm_reap, "github_runners", return_value=runners), \
                 mock.patch.object(vm_reap, "delete_runner",
                                   side_effect=lambda repo, rid, name, endpoint="": deleted.append(name) or "ok"):
                digest, _ = vm_reap.build_digest(args)
            tart_vms.assert_not_called()
        return digest, deleted

    def test_pool_ghosts_of_both_lanes_are_deleted_after_three_passes_and_the_age(self) -> None:
        digest, deleted = self.passes([self.runner("tartci-owners-m123451-4242-1", rid=1),
                                       self.runner("linux-ephr-4343-2", rid=2)])
        self.assertEqual(sorted(deleted), ["linux-ephr-4343-2", "tartci-owners-m123451-4242-1"])
        self.assertTrue(digest["config"]["runners_only"])
        self.assertEqual(digest["vms"], [])
        self.assertEqual(digest["config"]["own_name_prefixes"], ["tartci-owners-", "linux-ephr-"])

    def test_fewer_than_three_passes_or_less_than_900_s_deletes_nothing(self) -> None:
        ghost = [self.runner("tartci-owners-m1-4242-1")]
        self.assertEqual(self.passes(ghost, count=2, step=1800)[1], [])
        self.assertEqual(self.passes(ghost, count=3, step=60)[1], [])

    def test_online_busy_and_foreign_registrations_are_never_deleted(self) -> None:
        runners = [
            self.runner("tartci-owners-m1-4242-1", status="online", rid=1),
            self.runner("tartci-owners-m1-4242-2", busy=True, rid=2),  # offline + busy: reported, never deleted
            self.runner("pulp-studio-01-4242-3", rid=3),  # another lane's prefix
            self.runner("win-ephr-studio-4242-4", rid=4),
            self.runner("tartci-owners-m1", rid=5),  # not the per-boot shape <name>-<pid>-<n>
            self.runner("linux-ephr-4343-6", labels=("self-hosted", "pulp-host-other"), rid=6),  # another host's label
            self.runner("my-own-runner-1-2", rid=7),
        ]
        digest, deleted = self.passes(runners, count=5)
        self.assertEqual(deleted, [])
        actions = {row["name"]: row["action"] for row in digest["github_runners"]}
        self.assertEqual(actions.get("tartci-owners-m1-4242-2"), "offline_busy_orphaned_no_local_owner")
        self.assertNotIn("my-own-runner-1-2", actions)  # outside the prefixes it is not even inspected

    def test_an_organization_ghost_is_never_listed_or_deleted_and_no_orgs_or_users_call_is_made(self) -> None:
        """Through the real census (only the `gh api` transport and the delete are faked)."""
        calls: List[str] = []
        deleted: List[str] = []
        rows = {
            "repos/org1/a/actions/runners?per_page=100": [
                self.runner("linux-ephr-4343-2", rid=2)],
            "orgs/org1/actions/runners?per_page=100": [
                self.runner("linux-ephr-7777-1", rid=91), self.runner("tartci-owners-m1-4242-1", rid=92)],
        }

        def fake_run_bounded(argv: List[str], **kwargs: Any) -> Any:
            calls.append(argv[2])
            if argv[2] not in rows:
                return mock.Mock(returncode=1, stdout="", stderr="HTTP 404")
            return mock.Mock(returncode=0, stdout=json.dumps([{"runners": rows[argv[2]]}]), stderr="")

        def fake_run(argv: List[str], **kwargs: Any) -> Any:
            calls.append(" ".join(argv))
            if "DELETE" in argv:
                deleted.append(argv[-1])
            return mock.Mock(returncode=0, stdout="{}", stderr="")

        argv = self.reaper.argv("org1/a")[2:]
        digest: Dict[str, Any] = {}
        for i in range(3):
            with mock.patch.object(vm_reap, "utcnow", return_value=self.T0 + dt.timedelta(seconds=i * 450)), \
                 mock.patch.object(vm_reap.shutil, "which", return_value="/usr/bin/gh"), \
                 mock.patch.dict(os.environ, {"TARTCI_GH_CLI": "ghx"}), \
                 mock.patch.object(vm_reap, "run_bounded", side_effect=fake_run_bounded), \
                 mock.patch.object(vm_reap, "run", side_effect=fake_run):
                digest, _ = vm_reap.build_digest(vm_reap.parse_args(argv))
        self.assertEqual(digest["config"]["scopes"], ["repository"])
        self.assertEqual([c for c in calls if c.startswith(("repos/", "orgs/", "users/"))],
                         ["repos/org1/a/actions/runners?per_page=100"] * 3)  # one read per pass, repository only
        self.assertEqual([c for c in calls if "orgs/" in c or "users/" in c], [])
        self.assertEqual(deleted, [c.split()[-1] for c in calls if "DELETE" in c])
        self.assertTrue(all("repos/org1/a/actions/runners/2" in d for d in deleted) and len(deleted) == 1)
        names = {row["name"] for row in digest["github_runners"]}
        self.assertNotIn("linux-ephr-7777-1", names)
        self.assertNotIn("tartci-owners-m1-4242-1", names)

    def test_the_default_still_reads_both_scopes_for_other_callers(self) -> None:
        args = vm_reap.parse_args(["--repo", "org1/a", "--runners-only"])
        self.assertEqual(args.scopes, "")
        seen: Dict[str, Any] = {}

        def fake_runners(repo: str, *a: Any, **kw: Any) -> List[Dict[str, Any]]:
            seen.update(kw)
            return []

        with mock.patch.object(vm_reap.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(vm_reap, "github_runners", side_effect=fake_runners):
            digest, _ = vm_reap.build_digest(args)
        self.assertNotIn("scopes", seen)  # unrestricted: census reads repository and organization
        self.assertEqual(digest["config"]["scopes"], ["repository", "organization"])

    def test_an_unknown_scope_is_refused(self) -> None:
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            vm_reap.parse_args(["--scopes", "enterprise"])

    def test_without_the_flag_the_default_run_is_unchanged(self) -> None:
        args = vm_reap.parse_args(["--repo", "org1/a", "--state-root", str(self.tmp / "s")])
        self.assertFalse(args.runners_only)
        with mock.patch.object(vm_reap.shutil, "which", return_value=None):
            digest, rc = vm_reap.build_digest(args)
        self.assertEqual(rc, 2)
        self.assertIn("missing_tool:tart", digest["problems"])  # the VM half still demands tart

    def test_runners_only_needs_no_tart_binary(self) -> None:
        args = vm_reap.parse_args(self.reaper.argv("org1/a")[2:])
        with mock.patch.object(vm_reap.shutil, "which", side_effect=lambda tool: "/usr/bin/gh" if tool == "gh" else None), \
             mock.patch.object(vm_reap, "github_runners", return_value=[]):
            digest, rc = vm_reap.build_digest(args)
        self.assertEqual(digest["problems"], [])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
