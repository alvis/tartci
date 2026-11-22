#!/usr/bin/env python3
"""Tests for owners_dispatch.spawn_process: the macOS responsibility-disclaiming spawn must give a
child everything the previous `Popen(start_new_session=True)` did (own session and group, 0600 log
with stdout+stderr, stdin /dev/null, only the given env, default SIGPIPE, no inherited fds, exit
codes), and fall back to Popen where it is unavailable. Real processes; nothing leaves the host."""
from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import owners_dispatch as od  # noqa: E402

PROBE = r"""
import json, os, signal, sys
print(json.dumps({
    "pid": os.getpid(), "sid": os.getsid(0), "pgid": os.getpgrp(),
    "stdin": sys.stdin.read(),
    "env": sorted(os.environ), "fds": sorted(int(n) for n in os.listdir("/dev/fd") if n.isdigit()),
}), flush=True)
print("to-stderr", file=sys.stderr, flush=True)
"""


def wait_done(proc: Any, seconds: float = 20.0) -> int:
    deadline = time.monotonic() + seconds
    while proc.poll() is None:
        if time.monotonic() > deadline:
            raise AssertionError("child did not exit")
        time.sleep(0.02)
    return int(proc.returncode)


class SpawnCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="owners-spawn-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "child.log")
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "ONLY": "this"}

    def run_probe(self) -> Dict[str, Any]:
        proc = od.spawn_process([sys.executable, "-c", PROBE], self.env, self.log)
        self.assertEqual(wait_done(proc), 0)
        lines = Path(self.log).read_text().splitlines()
        self.assertIn("to-stderr", lines)  # stderr shares the log
        self.proc = proc
        return json.loads(lines[0])


class SpawnBehaviourTests(SpawnCase):
    def check_child_matches_popen_semantics(self, info: Dict[str, Any]) -> None:
        self.assertEqual(info["sid"], info["pid"])  # leads its own session
        self.assertEqual(info["pgid"], info["pid"])
        self.assertEqual(info["stdin"], "")  # /dev/null
        self.assertEqual(stat.S_IMODE(os.stat(self.log).st_mode), 0o600)
        self.assertTrue({"ONLY", "PATH"} <= set(info["env"]))
        self.assertNotIn("HOME", info["env"])  # the dispatcher's own environment is not inherited
        self.assertEqual([fd for fd in info["fds"] if fd > 2], [3] if info["fds"].count(3) else [],
                         "no descriptor of the dispatcher is inherited (3 may be the listing itself)")

    def test_disclaimed_spawn_matches_the_popen_semantics(self) -> None:
        if od._spawn_libc() is None:
            self.skipTest("not macOS")
        info = self.run_probe()
        self.assertIsInstance(self.proc, od.SpawnedProcess)
        self.assertEqual(self.proc.pid, info["pid"])
        self.assertEqual(self.proc.pgid, info["pid"])
        self.check_child_matches_popen_semantics(info)

    def test_fallback_popen_path_matches_too(self) -> None:
        with mock.patch.object(od, "_spawn_libc", return_value=None):
            info = self.run_probe()
        self.assertNotIsInstance(self.proc, od.SpawnedProcess)
        self.check_child_matches_popen_semantics(info)

    def test_sigpipe_is_back_to_its_default_in_the_child_on_both_paths(self) -> None:
        # Python ignores SIGPIPE itself; a child that inherited the ignore would see EPIPE (status 1)
        # where a default child is killed by the signal (status 141).
        script = "yes | head -1 >/dev/null; echo sigpipe-status=${PIPESTATUS[0]}"
        for libc in (od._spawn_libc(), None):
            with mock.patch.object(od, "_spawn_libc", return_value=libc):
                wait_done(od.spawn_process(["/bin/bash", "-c", script], self.env, self.log))
        self.assertEqual(Path(self.log).read_text().count("sigpipe-status=141"), 2)

    def test_sigxfsz_is_back_to_its_default_in_the_child_on_both_paths(self) -> None:
        # Python ignores SIGXFSZ too, and Popen(restore_signals=True) restores it: a default child
        # writing past its file-size limit is killed (status 153), an ignoring one just sees EFBIG.
        big = os.path.join(self.tmp, "big")
        script = f"ulimit -f 1; dd if=/dev/zero of={big} bs=1024 count=10 2>/dev/null; echo xfsz-status=$?"
        for libc in (od._spawn_libc(), None):
            with mock.patch.object(od, "_spawn_libc", return_value=libc):
                wait_done(od.spawn_process(["/bin/bash", "-c", script], self.env, self.log))
        self.assertEqual(Path(self.log).read_text().count("xfsz-status=153"), 2)

    def test_stderr_can_go_to_its_own_file_on_both_paths(self) -> None:
        err = os.path.join(self.tmp, "child.err")
        for libc in (od._spawn_libc(), None):
            with mock.patch.object(od, "_spawn_libc", return_value=libc):
                wait_done(od.spawn_process(["/bin/sh", "-c", "echo out; echo err >&2"], self.env, self.log, err))
        self.assertEqual(Path(self.log).read_text(), "out\nout\n")
        self.assertEqual(Path(err).read_text(), "err\nerr\n")
        self.assertEqual(stat.S_IMODE(os.stat(err).st_mode), 0o600)

    def test_an_unopenable_stderr_file_does_not_leak_the_log_descriptor(self) -> None:
        missing = os.path.join(self.tmp, "no-such-dir", "child.err")
        before = len(os.listdir("/dev/fd"))
        for _ in range(5):  # spawn_process itself records nothing; its callers do, per kind
            with self.assertRaises(OSError):
                od.spawn_process(["/bin/sh", "-c", "true"], self.env, self.log, missing)
        self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_the_log_is_appended_not_truncated(self) -> None:
        Path(self.log).write_text("earlier\n")
        os.chmod(self.log, 0o600)
        wait_done(od.spawn_process(["/bin/sh", "-c", "echo later"], self.env, self.log))
        self.assertEqual(Path(self.log).read_text(), "earlier\nlater\n")

    def test_dispatcher_descriptors_are_not_inherited(self) -> None:
        leak = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, leak)
        os.set_inheritable(leak, True)  # even an inheritable one must not reach the child
        info = self.run_probe()
        self.assertNotIn(leak, info["fds"] if leak > 3 else [])
        self.assertLessEqual(len([fd for fd in info["fds"] if fd > 2]), 1)

    def test_exit_codes_and_signals_are_reported_like_popen(self) -> None:
        self.assertEqual(wait_done(od.spawn_process(["/bin/sh", "-c", "exit 3"], self.env, self.log)), 3)
        proc = od.spawn_process(["/bin/sh", "-c", "sleep 30"], self.env, self.log)
        self.assertIsNone(proc.poll())
        os.kill(proc.pid, signal.SIGKILL)
        self.assertEqual(wait_done(proc), -signal.SIGKILL)
        self.assertEqual(proc.poll(), -signal.SIGKILL)  # stable once collected

    def test_wait_times_out_while_the_child_runs(self) -> None:
        proc = od.spawn_process(["/bin/sh", "-c", "sleep 30"], self.env, self.log)
        self.addCleanup(lambda: (os.kill(proc.pid, signal.SIGKILL), proc.wait(5)))
        if isinstance(proc, od.SpawnedProcess):
            import subprocess
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.wait(0.2)

    def test_killing_the_group_takes_the_whole_tree(self) -> None:
        proc = od.spawn_process(["/bin/sh", "-c", "sleep 60 & echo $! ; wait"], self.env, self.log)
        deadline = time.monotonic() + 10
        while not Path(self.log).read_text().strip() and time.monotonic() < deadline:
            time.sleep(0.05)
        grandchild = int(Path(self.log).read_text().split()[0])
        od._kill_group(proc, signal.SIGKILL)
        self.assertEqual(wait_done(proc), -signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the grandchild survived the group kill")

    def test_a_program_found_through_the_childs_own_path(self) -> None:
        bindir = os.path.join(self.tmp, "bin")
        os.mkdir(bindir)
        tool = os.path.join(bindir, "owners-spawn-tool")
        Path(tool).write_text("#!/bin/sh\necho tool-ran\n")
        os.chmod(tool, 0o755)
        env = {"PATH": bindir + ":/usr/bin:/bin"}
        self.assertEqual(wait_done(od.spawn_process(["owners-spawn-tool"], env, self.log)), 0)
        self.assertEqual(Path(self.log).read_text(), "tool-ran\n")

    def test_a_missing_or_unexecutable_program_raises_oserror_like_popen(self) -> None:
        with self.assertRaises(OSError):
            od.spawn_process(["/nonexistent/owners-spawn-tool"], self.env, self.log)
        with self.assertRaises(OSError):
            od.spawn_process(["owners-spawn-not-on-path"], self.env, self.log)
        plain = os.path.join(self.tmp, "plain")
        Path(plain).write_text("x")
        with self.assertRaises(OSError):
            od.spawn_process([plain], self.env, self.log)

    def test_the_log_descriptor_is_closed_in_the_dispatcher(self) -> None:
        before = len(os.listdir("/dev/fd"))
        for _ in range(5):  # spawn_process itself records nothing; its callers do, per kind
            wait_done(od.spawn_process(["/usr/bin/true"], self.env, self.log))
        self.assertLessEqual(len(os.listdir("/dev/fd")), before)


class FakeLibc:
    """Stands in for libSystem: every call succeeds (0) unless named in `fail`; records the calls."""

    def __init__(self, fail: Dict[str, int] = None) -> None:  # type: ignore[assignment]
        self.fail = fail or {}
        self.calls: List[str] = []

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any) -> int:
            self.calls.append(name)
            return self.fail.get(name, 0)
        return call


class SpawnCheckedCallsTests(unittest.TestCase):
    ENV = {"PATH": "/usr/bin:/bin"}

    def spawn(self, libc: FakeLibc) -> Any:
        fd = os.open(os.devnull, os.O_WRONLY)
        self.addCleanup(os.close, fd)
        return od.spawn_disclaimed(libc, ["/usr/bin/true"], self.ENV, fd)

    def test_a_failing_disclaim_raises_and_never_spawns(self) -> None:
        libc = FakeLibc({"responsibility_spawnattrs_setdisclaim": 22})
        with self.assertRaises(OSError) as caught:
            self.spawn(libc)
        self.assertEqual(caught.exception.errno, 22)
        self.assertIn("responsibility_spawnattrs_setdisclaim", str(caught.exception))
        self.assertNotIn("posix_spawn", libc.calls)  # no child, in particular no non-disclaimed one
        self.assertIn("posix_spawn_file_actions_destroy", libc.calls)  # and nothing leaks
        self.assertIn("posix_spawnattr_destroy", libc.calls)

    def test_every_attr_and_file_action_call_is_checked(self) -> None:
        calls = ["posix_spawnattr_init", "posix_spawn_file_actions_init", "responsibility_spawnattrs_setdisclaim",
                 "posix_spawnattr_setsigdefault", "posix_spawnattr_setsigmask", "posix_spawnattr_setflags",
                 "posix_spawn_file_actions_addopen", "posix_spawn_file_actions_adddup2"]
        for name in calls:
            libc = FakeLibc({name: 12})
            with self.subTest(call=name), self.assertRaises(OSError) as caught:
                self.spawn(libc)
            self.assertEqual(caught.exception.errno, 12)
            self.assertNotIn("posix_spawn", libc.calls)

    def test_a_failing_init_releases_only_what_was_initialised(self) -> None:
        libc = FakeLibc({"posix_spawnattr_init": 12})
        with self.assertRaises(OSError):
            self.spawn(libc)
        self.assertNotIn("posix_spawnattr_destroy", libc.calls)  # never initialised, so never destroyed
        libc = FakeLibc({"posix_spawn_file_actions_init": 12})
        with self.assertRaises(OSError):
            self.spawn(libc)
        self.assertIn("posix_spawnattr_destroy", libc.calls)
        self.assertNotIn("posix_spawn_file_actions_destroy", libc.calls)

    def test_a_failing_signal_set_call_uses_errno(self) -> None:
        for name in ("sigemptyset", "sigaddset"):
            libc = FakeLibc({name: -1})
            with self.subTest(call=name), mock.patch.object(od.ctypes, "get_errno", return_value=22), \
                 self.assertRaises(OSError) as caught:
                self.spawn(libc)
            self.assertEqual(caught.exception.errno, 22)
            self.assertNotIn("posix_spawn", libc.calls)

    def test_posix_spawn_failing_still_raises(self) -> None:
        with self.assertRaises(OSError) as caught:
            self.spawn(FakeLibc({"posix_spawn": 2}))
        self.assertEqual(caught.exception.errno, 2)

    def test_a_failing_disclaim_does_not_fall_back_to_a_plain_child(self) -> None:
        log = os.path.join(tempfile.mkdtemp(prefix="owners-spawn-"), "c.log")
        self.addCleanup(shutil.rmtree, os.path.dirname(log), True)
        with mock.patch.object(od, "_spawn_libc", return_value=FakeLibc({"responsibility_spawnattrs_setdisclaim": 1})), \
             mock.patch.object(od.subprocess, "Popen") as popen:
            with self.assertRaises(OSError):
                od.spawn_process(["/usr/bin/true"], self.ENV, log)
        popen.assert_not_called()

    def test_sigpipe_and_sigxfsz_are_both_reset(self) -> None:
        added: List[int] = []

        class Recording(FakeLibc):
            def __getattr__(self, name: str) -> Any:
                if name == "sigaddset":
                    def sigaddset(_set: Any, number: int) -> int:
                        added.append(number)
                        return 0
                    return sigaddset
                return super().__getattr__(name)

        self.spawn(Recording())
        self.assertEqual(added, [signal.SIGPIPE, signal.SIGXFSZ])


class SpawnModeTests(unittest.TestCase):
    def test_the_active_path_is_named_and_a_fallback_is_not_silent(self) -> None:
        with mock.patch.object(od, "_spawn_libc", return_value=object()):
            self.assertEqual(od.spawn_mode(), "disclaimed posix_spawn")
        with mock.patch.object(od, "_spawn_libc", return_value=None):
            self.assertIn("Popen fallback", od.spawn_mode())
            self.assertIn("NOT disclaimed", od.spawn_mode())


class SpawnLibcTests(unittest.TestCase):
    def test_no_libc_off_macos_or_when_a_symbol_is_missing(self) -> None:
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(od._load_spawn_libc())
        if sys.platform == "darwin":
            class Bare:
                def __getattr__(self, name: str) -> Any:
                    raise AttributeError(name)

            with mock.patch.object(od.ctypes, "CDLL", return_value=Bare()):
                self.assertIsNone(od._load_spawn_libc())

    def test_the_library_is_loaded_once(self) -> None:
        with mock.patch.object(od, "_SPAWN_LIBC", []), \
             mock.patch.object(od, "_load_spawn_libc", return_value="lib") as load:
            self.assertEqual([od._spawn_libc() for _ in range(3)], ["lib"] * 3)
        self.assertEqual(load.call_count, 1)

    def test_spawn_failures_are_recorded_per_kind(self) -> None:
        with mock.patch.dict(od.LAST_SPAWN_ERROR, clear=True):
            od.record_spawn("child", OSError(2, "No such file"))
            od.record_spawn("reap")  # a reap that works never clears a child failure
            self.assertIn("No such file", od.LAST_SPAWN_ERROR["child"]["error"])
            od.record_spawn("reap", OSError(24, "EMFILE"))
            od.record_spawn("child")
            self.assertEqual(sorted(od.LAST_SPAWN_ERROR), ["reap"])

if __name__ == "__main__":
    unittest.main()
