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
import re
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
  *'cat > ~/jit.cfg'*) cat >"$FAKE_LOG/jit-stdin"; exit "${FAKE_JIT_STREAM:-0}";;
  *'/mnt/rosetta'*) exit "${FAKE_ROSETTA_CHECK:-0}";;
  *'ACTIONS_RUNNER_INPUT_JITCONFIG='*)
    sleep 0.2  # stay alive past the pool-lock handoff's `kill -0` (an instant exit races it)
    echo 'Listening for Jobs'; echo 'Running job: smoke'; echo 'Job smoke completed'; exit 0;;
  *'prepare-ccache'*|*'bash -s'*) cat >/dev/null; exit 0;;
esac
exit 0
"""

RUNNER_START = "ACTIONS_RUNNER_INPUT_JITCONFIG="

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
        starts = [c for c in self.calls(log, "ssh") if RUNNER_START in c]
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

    def test_jit_config_travels_over_stdin_not_in_any_ssh_argument(self) -> None:
        for rosetta in ("0", "1"):
            with self.subTest(rosetta=rosetta):
                proc, log = self.run_lane(TARTCI_LINUX_ROSETTA=rosetta)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                ssh = self.calls(log, "ssh")
                self.assertFalse([c for c in ssh if "FAKE-JIT-CONFIG" in c], ssh)
                self.assertEqual((log / "jit-stdin").read_text(encoding="utf-8"), "FAKE-JIT-CONFIG")
                stream = next(i for i, c in enumerate(ssh) if "cat > ~/jit.cfg" in c)
                self.assertIn("umask 077", ssh[stream])
                self.assertLess(stream, ssh.index(self.runner_start(log)))
                self.assertNotIn("FAKE-JIT-CONFIG", proc.stdout + proc.stderr)

    def test_guest_reads_the_jit_config_from_the_environment_and_deletes_the_file(self) -> None:
        # No `--jitconfig <value>` in the guest's argv (visible to every guest process for
        # the whole job), and ~/jit.cfg is gone before the runner starts, as on macOS
        # (guest-aqua-runner.sh).
        for rosetta in ("0", "1"):
            with self.subTest(rosetta=rosetta):
                proc, log = self.run_lane(TARTCI_LINUX_ROSETTA=rosetta)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                ssh = self.calls(log, "ssh")
                self.assertFalse([c for c in ssh if "--jitconfig" in c], ssh)
                start = self.runner_start(log)
                read = start.index('jit="$(cat ~/jit.cfg)"')
                remove = start.index("rm -f ~/jit.cfg")
                run = start.index('ACTIONS_RUNNER_INPUT_JITCONFIG="$jit" ./run.sh')
                self.assertLess(read, remove)
                self.assertLess(remove, run)  # deleted before the runner starts
                self.assertEqual(start.count("jit.cfg"), 2)  # the one read and the one delete

    def test_failed_jit_stream_discards_the_vm_and_never_starts_the_runner(self) -> None:
        proc, log = self.run_lane(FAKE_JIT_STREAM="255")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertFalse([c for c in self.calls(log, "ssh") if RUNNER_START in c])
        self.assertTrue([c for c in self.calls(log, "tart") if c.startswith("delete ")])

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
        self.assertFalse([c for c in self.calls(log, "ssh") if RUNNER_START in c])
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

    def test_golden_ships_a_sha256_pinned_node_with_npm_in_usr_local(self) -> None:
        # pnpm/setup's `runtime: node@...` installs node without npm; jobs that run
        # `npm ...` rely on the image's Node, as on GitHub-hosted runners.
        body = GOLDEN_SCRIPT.read_text(encoding="utf-8")
        self.assertRegex(body, r'(?m)^NODE_VERSION="\d+\.\d+\.\d+"$')
        self.assertRegex(body, r'(?m)^NODE_SHA_ARM64="[0-9a-f]{64}"$')
        # Fetched through the same checksum gate as the runner archives, then copied in.
        self.assertRegex(
            body,
            r'fetch_pinned "\$NODE_TARBALL" "https://nodejs\.org/dist/v\$NODE_VERSION/'
            r'\$NODE_TARBALL" "\$NODE_SHA_ARM64"',
        )
        fetch = re.search(r"(?ms)^fetch_pinned\(\)\{.*?^\}", body)
        assert fetch is not None
        self.assertIn('[ "$got" = "$3" ] || {', fetch.group(0))
        self.assertIn('"$DL_DIR/$NODE_TARBALL"', body)
        # Installed root-owned into /usr/local, so node/npm/npx land on the default PATH.
        self.assertRegex(
            body, r'sudo tar -xJf "/tmp/\$NODE_TARBALL" -C /usr/local --strip-components=1 --no-same-owner'
        )
        # The build fails unless the guest resolves them there.
        self.assertIn('for tool in node npm npx; do\n'
                      '  check [ "$(command -v "$tool")" = "/usr/local/bin/$tool" ]\n', body)
        self.assertIn('check [ "$(stat -c %u /usr/local/bin)" = 0 ]', body)

    def test_golden_self_checks_fail_the_build(self) -> None:
        # Under `set -e`, a failure inside `a && b` (all but the last command) or inside a command
        # substitution among `echo`'s arguments does not stop the script: such a check proves nothing.
        body = GOLDEN_SCRIPT.read_text(encoding="utf-8")
        section = re.search(r"(?ms)^# --- self-checks -+$(.*?)^sudo apt-get clean$", body)
        assert section is not None
        self.assertIn('check(){ "$@" || { echo "self-check failed: $*" >&2; exit 1; }; }', section.group(1))
        for line in section.group(1).splitlines():
            if line.lstrip().startswith("#"):
                continue
            self.assertNotIn("&&", line)
            self.assertNotRegex(line, r'^\s*echo .*\$\(')
        for runner in ("actions-runner", "actions-runner-x64"):
            self.assertIn(f'check test -x "$HOME/{runner}/run.sh"', section.group(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
