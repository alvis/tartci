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
        self.assertIn('for tool in node npm npx gh yarn; do\n'
                      '  check [ "$(command -v "$tool")" = "/usr/local/bin/$tool" ]\n', body)
        self.assertIn('check [ "$(stat -c %u /usr/local/bin)" = 0 ]', body)

    def test_golden_ships_a_sha256_pinned_gh_and_an_exact_yarn(self) -> None:
        # Hosted images ship the GitHub CLI and Yarn classic; jobs call both without installing them.
        body = GOLDEN_SCRIPT.read_text(encoding="utf-8")
        self.assertRegex(body, r'(?m)^GH_VERSION="\d+\.\d+\.\d+"$')
        self.assertRegex(body, r'(?m)^GH_SHA_ARM64="[0-9a-f]{64}"$')
        self.assertRegex(body, r'(?m)^YARN_VERSION="1\.\d+\.\d+"$')
        self.assertRegex(
            body,
            r'fetch_pinned "\$GH_TARBALL" "https://github\.com/cli/cli/releases/download/v\$GH_VERSION/'
            r'\$GH_TARBALL" "\$GH_SHA_ARM64"',
        )
        self.assertIn('"$DL_DIR/$GH_TARBALL"', body)
        self.assertRegex(
            body, r'sudo tar -xzf "/tmp/\$GH_TARBALL" -C /usr/local --strip-components=1 --no-same-owner'
        )
        # An exact version, never a range or a dist-tag: two builds give the same Yarn.
        self.assertIn('npm install --global --no-fund --no-audit --loglevel=error "yarn@$YARN_VERSION"', body)

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


    def test_golden_toolset_downloads_are_sha256_pinned(self) -> None:
        # Every direct download of the GitHub-image toolset goes through fetch_pinned with a pinned SHA-256.
        body = GOLDEN_SCRIPT.read_text(encoding="utf-8")
        table = re.search(r"(?ms)^TOOL_DOWNLOADS='\n(.*?)^'$", body)
        assert table is not None
        rows = [row.split() for row in table.group(1).splitlines() if row.strip()]
        keys = [row[0] for row in rows]
        self.assertEqual(len(keys), len(set(keys)))
        for row in rows:
            with self.subTest(row[0]):
                self.assertEqual(len(row), 5, row)
                key, version, file, url, sha = row
                self.assertRegex(key, r"^[A-Z0-9]+$")
                self.assertRegex(version, r"^\d+(\.\d+)+$")
                self.assertRegex(url, r"^https://")
                self.assertRegex(sha, r"^[0-9a-f]{64}$")
                self.assertIn("@V@", file + url)
                # The guest uses each row by its manifest name.
                self.assertIn(f"${key}_FILE", body)
        for tool in ("YQ", "KUBECTL", "HELM", "AWSCLI", "CMAKE", "GO", "RUSTUP", "GRADLE", "MAVEN", "AZDEVOPS"):
            self.assertIn(tool, keys)
        # Guest installs outside the table are exact versions or a pinned wheel, never "latest".
        self.assertIn('az extension add --source "$TOOLS/$AZDEVOPS_FILE"', body)
        self.assertNotIn("az extension add --name", body)
        pipx = re.findall(r"(?m)\bpipx install (\S+)", body)
        self.assertEqual(len(pipx), 2)
        for spec in pipx:
            self.assertRegex(spec, r"^[a-z][a-z0-9-]*==\d+(\.\d+)+$")
        # The manifest temp file only exists once the cleanup trap that removes it is set.
        self.assertLess(body.index("trap cleanup EXIT"), body.index('MANIFEST="$(mktemp'))
        loop = re.search(r'(?ms)^while read -r key ver file url sha; do$(.*?)^done <<<"\$TOOL_DOWNLOADS"$', body)
        assert loop is not None
        self.assertIn('fetch_pinned "$file" "$url" "$sha"', loop.group(1))
        self.assertRegex(body, r'(?m)^VCPKG_COMMIT="[0-9a-f]{40}"$')
        self.assertIn('[ "$(sudo git -C /usr/local/share/vcpkg rev-parse HEAD)" = "$VCPKG_COMMIT" ]', body)

    def test_golden_third_party_apt_keys_are_pinned_by_fingerprint(self) -> None:
        body = GOLDEN_SCRIPT.read_text(encoding="utf-8")
        repo_fn = re.search(r"(?ms)^apt_repo\(\)\{.*?^\}$", body)
        assert repo_fn is not None
        # The key must carry the pinned fingerprint; the keyring then holds only that key, and the
        # source is arm64-only so the amd64 multiarch never asks it for amd64 indexes.
        self.assertIn('grep -q "^fpr:::::::::$3:"', repo_fn.group(0))
        self.assertIn('gpg --batch --export "$3"', repo_fn.group(0))
        self.assertIn("Architectures: arm64", repo_fn.group(0))
        repos = re.findall(r"(?m)^apt_repo (\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)$", body)
        self.assertEqual({r[0] for r in repos},
                         {"docker", "azure-cli", "google-cloud", "adoptium", "hashicorp", "mozilla"})
        for name, key_url, fpr, uri, _suite, _component in repos:
            with self.subTest(name):
                self.assertRegex(fpr, r"^[0-9A-F]{40}$")
                self.assertTrue(key_url.startswith("https://") and uri.startswith("https://"))

    def test_golden_has_the_tools_the_failed_owner_jobs_lacked(self) -> None:
        # yq (linux-a64), docker (linux-x64), and libatomic for x64 binaries under Rosetta.
        body = GOLDEN_SCRIPT.read_text(encoding="utf-8")
        section = re.search(r"(?ms)^# --- self-checks -+$(.*?)^sudo apt-get clean$", body)
        assert section is not None
        checks = section.group(1)
        for probe in ('"yq --version"', '"docker --version"', '"docker buildx version"',
                      '"docker compose version"', '"kubectl version --client"', '"aws --version"',
                      '"az --version"', '"gcloud --version"', '"java -version"', '"go version"',
                      '"rustc --version"', '"cmake --version"', '"python --version"'):
            self.assertIn(probe, checks)
        self.assertIn('line="$(first_line $entry)"', checks)
        self.assertRegex(checks, r"(?m)^HELLO_WORLD=hello-world@sha256:[0-9a-f]{64}$")
        self.assertIn('check sudo docker run --rm "$HELLO_WORLD" >/dev/null', checks)
        self.assertIn('check sudo docker run --rm --platform linux/amd64 "$HELLO_WORLD" >/dev/null', checks)
        # Functional: the x64 Node dlopens each library through the amd64 loader under Rosetta.
        self.assertIn('check "$x64_node" -e', checks)
        self.assertRegex(checks, r"for so in libatomic\.so\.1 ")
        self.assertIn('check dpkg-query -W "$p:amd64" >/dev/null', checks)
        libs = re.search(r'(?ms)^AMD64_LIBS="(.*?)"$', body)
        assert libs is not None
        for lib in ("libatomic1", "libgcc-s1", "zlib1g", "libssl3t64", "libcurl4t64", "libffi8",
                    "liblzma5", "libbz2-1.0", "libreadline8t64", "libsqlite3-0", "libtinfo6", "libicu74"):
            self.assertIn(lib, libs.group(1).split())
        self.assertIn('sudo usermod -aG docker "$(id -un)"', body)

    def test_golden_sets_the_runner_environment_hosted_images_set(self) -> None:
        body = GOLDEN_SCRIPT.read_text(encoding="utf-8")
        for line in ('echo "ImageOS=ubuntu24"', 'echo "RUNNER_TOOL_CACHE=/opt/hostedtoolcache"',
                     'echo "AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache"',
                     'echo "JAVA_HOME=/usr/lib/jvm/temurin-25-jdk-arm64"',
                     'echo "JAVA_HOME_${v}_X64=/usr/lib/jvm/temurin-$v-jdk-arm64"'):
            self.assertIn(line, body)
        self.assertIn("for v in 11 17 21 25; do", body)
        # Both runners get it; only the x64 runner defaults containers to amd64.
        self.assertIn('install -m 0644 /tmp/tartci-image.env "$HOME/actions-runner/.env"', body)
        self.assertIn('{ cat /tmp/tartci-image.env; echo "DOCKER_DEFAULT_PLATFORM=linux/amd64"; } '
                      '>"$HOME/actions-runner-x64/.env"', body)
        self.assertIn('sudo install -d -o "$(id -u)" -g "$(id -g)" /opt/hostedtoolcache', body)

    def test_golden_guest_script_is_parsed_whole_before_it_runs(self) -> None:
        # bash -s reads the script from stdin; a step that reads stdin would eat the rest of it.
        body = GOLDEN_SCRIPT.read_text(encoding="utf-8")
        guest = body.split("<<'GUEST'\n", 1)[1].split("\nGUEST\n", 1)[0]
        code = [l for l in guest.splitlines() if l and not l.startswith("#")]
        self.assertEqual(code[0], "{")
        self.assertEqual(code[-1], "} </dev/null")

    def test_golden_never_handles_a_github_token(self) -> None:
        body = GOLDEN_SCRIPT.read_text(encoding="utf-8")
        for needle in ("GH_TOKEN", "GITHUB_TOKEN", "gh auth", "generate-jitconfig", "jit.cfg"):
            self.assertNotIn(needle, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
