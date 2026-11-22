#!/usr/bin/env python3
"""Opt-in Rosetta boot for the Tart Linux serving lane (TARTCI_LINUX_ROSETTA).

`providers/tart-linux/runner.sh` is run end to end with fake `tart`, `ssh` and
`gh` programs on PATH and a throwaway HOME, so no VM, network or GitHub call
happens. The fakes record every argument. Two properties are checked:

* unset (or 0): the lane boots with exactly the arguments it always did and
  starts the arm64 runner in ~/actions-runner, and makes no Rosetta guest call;
* TARTCI_LINUX_ROSETTA=1: it adds `--rosetta=rosetta` to `tart run`, checks the
  guest's Rosetta/binfmt/runner state before minting a JIT config, and starts
  the runner from the separate x64 directory.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-linux" / "runner.sh"
GOLDEN_SCRIPT = ROOT / "providers" / "tart-linux" / "owners-golden.sh"

FAKE_TART = """\
#!/bin/bash
printf '%s\\n' "$*" >>"$FAKE_LOG/tart.calls"
case "$1" in
  ip) echo 192.0.2.10;;
  run) exec sleep 300;;
  list) ;;
esac
exit 0
"""

# The runner start is the one ssh call whose command line contains run.sh. The
# Rosetta guest check is the one that names /mnt/rosetta. FAKE_ROSETTA_CHECK
# decides whether that check passes.
FAKE_SSH = """\
#!/bin/bash
printf '%s\\n' "$*" >>"$FAKE_LOG/ssh.calls"
case "$*" in
  *'/mnt/rosetta'*) exit "${FAKE_ROSETTA_CHECK:-0}";;
  *'./run.sh --jitconfig'*)
    sleep 0.2  # stay alive past the pool-lock handoff's `kill -0` (an instant exit races it)
    echo 'Listening for Jobs'; echo 'Running job: smoke'; echo 'Job smoke completed'; exit 0;;
  *'prepare-ccache'*|*'bash -s'*) cat >/dev/null; exit 0;;
esac
exit 0
"""

FAKE_GH = """\
#!/bin/bash
printf '%s\\n' "$*" >>"$FAKE_LOG/gh.calls"
case "$*" in
  *generate-jitconfig*) echo FAKE-JIT-CONFIG;;
esac
exit 0
"""


class LinuxRosettaLaneTests(unittest.TestCase):
    def run_lane(self, **env: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        bindir, home, log = root / "bin", root / "home", root / "log"
        for directory in (bindir, home, log, home / ".tart", root / "cache"):
            directory.mkdir(parents=True)
        for name, body in (("tart", FAKE_TART), ("ssh", FAKE_SSH), ("gh", FAKE_GH)):
            (bindir / name).write_text(body, encoding="utf-8")
            (bindir / name).chmod(0o755)
        full_env = {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "HOME": str(home),
            "TART_HOME": str(home / ".tart"),
            "FAKE_LOG": str(log),
            "TARTCI_VM_LEASES": "0",
            "TARTCI_LINUX_LOGS": str(root / "vmlogs"),
            "TARTCI_CI_CACHE": str(root / "cache"),
            "TARTCI_STATE_DIR": str(root / "state"),
            "TARTCI_VM_SSH_KEY": str(root / "key"),
            "TARTCI_RUNNER_REPO": "Org/repo",
            "TARTCI_RUNNER_LABELS": "self-hosted,linux-x64",
            "TARTCI_LINUX_GOLDEN": "owners-linux",
            "TARTCI_RUNNER_IDLE_TIMEOUT_SECS": "30",
            "TARTCI_POOL_STATE_FILE": str(root / "pool-state"),
            "TARTCI_POOL_LOCK_DIR": str(root / "pool-lock"),
            "TARTCI_DISK_FLOOR_GIB": "0",
        }
        full_env.update(env)
        proc = subprocess.run(
            ["bash", str(RUNNER), "--once"],
            env=full_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            cwd=str(root),
        )
        return proc, log

    @staticmethod
    def calls(log: Path, name: str) -> list[str]:
        path = log / f"{name}.calls"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def tart_run_line(self, log: Path) -> str:
        runs = [line for line in self.calls(log, "tart") if line.startswith("run ")]
        self.assertEqual(len(runs), 1, runs)
        return runs[0]

    def runner_start(self, log: Path) -> str:
        starts = [c for c in self.calls(log, "ssh") if "./run.sh --jitconfig" in c]
        self.assertEqual(len(starts), 1, starts)
        return starts[0]

    def test_default_boot_is_unchanged(self) -> None:
        proc, log = self.run_lane()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        run = self.tart_run_line(log)
        self.assertRegex(run, r"^run --no-graphics --dir=ccache:\S+/ccache-linux linux-ephr-\d+-1$")
        self.assertNotIn("rosetta", run)
        self.assertIn("cd ~/actions-runner && ", self.runner_start(log))
        self.assertNotIn("actions-runner-x64", "\n".join(self.calls(log, "ssh")))
        self.assertFalse([c for c in self.calls(log, "ssh") if "/mnt/rosetta" in c])

    def test_zero_and_empty_are_the_default(self) -> None:
        for value in ("0", ""):
            with self.subTest(value=value):
                proc, log = self.run_lane(TARTCI_LINUX_ROSETTA=value)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("rosetta", self.tart_run_line(log))
                self.assertIn("cd ~/actions-runner && ", self.runner_start(log))

    def test_opt_in_boots_with_rosetta_and_runs_the_x64_runner(self) -> None:
        proc, log = self.run_lane(TARTCI_LINUX_ROSETTA="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(
            self.tart_run_line(log),
            r"^run --no-graphics --dir=ccache:\S+/ccache-linux --rosetta=rosetta linux-ephr-\d+-1$",
        )
        start = self.runner_start(log)
        self.assertIn("cd ~/actions-runner-x64 && ", start)
        self.assertNotIn("cd ~/actions-runner &&", start)
        ssh = self.calls(log, "ssh")
        check = next(i for i, c in enumerate(ssh) if "/mnt/rosetta" in c)
        self.assertLess(check, ssh.index(start), "Rosetta check must precede the runner start")
        gh = self.calls(log, "gh")
        mint = next(i for i, c in enumerate(gh) if "generate-jitconfig" in c)
        self.assertTrue(gh[mint])
        self.assertIn("binfmt_misc/rosetta", ssh[check])
        self.assertIn("actions-runner-x64/run.sh", ssh[check])

    def test_x64_runner_dir_is_configurable(self) -> None:
        proc, log = self.run_lane(
            TARTCI_LINUX_ROSETTA="1", TARTCI_LINUX_X64_RUNNER_DIR="runner-x64.v2"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("cd ~/runner-x64.v2 && ", self.runner_start(log))

    def test_failed_guest_check_discards_before_minting_a_jit_config(self) -> None:
        proc, log = self.run_lane(TARTCI_LINUX_ROSETTA="1", FAKE_ROSETTA_CHECK="1")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("no usable Rosetta", proc.stderr)
        self.assertFalse([c for c in self.calls(log, "gh") if "generate-jitconfig" in c])
        self.assertFalse([c for c in self.calls(log, "ssh") if "./run.sh --jitconfig" in c])
        tart = self.calls(log, "tart")
        self.assertTrue([c for c in tart if c.startswith("delete ")], tart)

    def test_invalid_settings_are_refused_before_any_vm(self) -> None:
        cases = (
            {"TARTCI_LINUX_ROSETTA": "yes"},
            {"TARTCI_LINUX_ROSETTA": "1", "TARTCI_LINUX_X64_RUNNER_DIR": "../escape"},
            {"TARTCI_LINUX_ROSETTA": "1", "TARTCI_LINUX_X64_RUNNER_DIR": "a;b"},
        )
        for env in cases:
            with self.subTest(env=env):
                proc, log = self.run_lane(**env)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(self.calls(log, "tart"), [])

    def test_golden_builder_matches_the_lane_contract(self) -> None:
        body = GOLDEN_SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(
            subprocess.run(["bash", "-n", str(GOLDEN_SCRIPT)], check=False).returncode, 0
        )
        # Both runner dirs the lane may cd into, and the boot-time Rosetta unit.
        self.assertIn('"$HOME/actions-runner"', body)
        self.assertIn('"$HOME/actions-runner-x64"', body)
        self.assertIn("tartci-rosetta.service", body)
        self.assertIn("tartci-register-rosetta-binfmt", body)
        # The unit must be a no-op when no share is attached.
        self.assertIn("nothing to do", body)
        self.assertIn("exit 0", body)
        # Digest-pinned base and checksum-pinned runners keep the build reproducible.
        self.assertIn("@sha256:", body)
        self.assertEqual(body.count("RUNNER_SHA_"), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
