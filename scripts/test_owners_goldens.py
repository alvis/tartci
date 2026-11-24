#!/usr/bin/env python3
"""Both owner-pool golden scripts (providers/tart-{linux,macos}/owners-golden.sh) when `tart list` fails.

Each test extracts the script's own `note`, `die`, `vm_exists` and `cleanup` functions and runs them
in bash against a fake `tart` shell function that records every call, so no VM is touched. A failed
listing must never read as "VM absent": that sent a live swap-in down the plain-rename path, which
failed against the existing golden and threw the finished build away.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
GOLDENS = {
    "linux": ROOT / "providers" / "tart-linux" / "owners-golden.sh",
    "macos": ROOT / "providers" / "tart-macos" / "owners-golden.sh",
}
# The interpreter the scripts run under on the host: macOS's /bin/bash is 3.2.
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"


def functions(script: Path) -> Dict[str, str]:
    body = script.read_text(encoding="utf-8")
    found = {}
    for name in ("note", "die"):
        match = re.search(rf"(?m)^{name}\(\)\s*\{{.*\}}$", body)
        assert match is not None, f"{script}: no one-line {name}()"
        found[name] = match.group(0)
    for name in ("vm_exists", "cleanup"):
        match = re.search(rf"(?ms)^{name}\(\)\s*\{{$.*?^\}}$", body)
        assert match is not None, f"{script}: no {name}()"
        found[name] = match.group(0)
    return found


class GoldenListingFailureTests(unittest.TestCase):
    def run_bash(self, script: Path, tart: str, tail: str, calls: Path) -> subprocess.CompletedProcess:
        f = functions(script)
        program = "\n".join([
            "set -euo pipefail",
            "sleep(){ :; }",
            f"tart(){{ printf '%s\\n' \"$*\" >>'{calls}'; {tart} }}",
            f["note"], f["die"], f["vm_exists"], f["cleanup"],
            tail,
        ])
        return subprocess.run([BASH, "-c", program], capture_output=True, text=True, timeout=30)

    def cleanup(self, script: Path, tart: str, built: int, tmp: str) -> subprocess.CompletedProcess:
        boot_log = Path(tmp) / "boot.log"
        boot_log.write_text("")
        state = (f'RPID=""; DONE=0; BUILT={built}; NAME=owners-x; BUILD=owners-x-build-1; OLD=owners-x-old-1; '
                 f"BOOT_LOG='{boot_log}'; cleanup")
        result = self.run_bash(script, tart, state, Path(tmp) / "calls")
        self.assertFalse(boot_log.exists(), "cleanup left the boot log behind")
        if script == GOLDENS["macos"]:
            # The macOS cleanup ends the script itself, and an unfinished build never exits 0.
            self.assertNotEqual(result.returncode, 0)
        return result

    @staticmethod
    def calls(tmp: str):
        return (Path(tmp) / "calls").read_text().split("\n")

    def test_vm_exists_answers_from_the_listing_and_refuses_to_guess_without_one(self) -> None:
        listing = "[ \"$1\" = list ] && printf 'owners-x\\nowners-y\\n';"
        failing = "echo 'tart: busy' >&2; return 1;"
        for os_name, script in GOLDENS.items():
            with self.subTest(os_name), tempfile.TemporaryDirectory() as tmp:
                calls = Path(tmp) / "calls"

                def ask(tart: str, name: str) -> subprocess.CompletedProcess:
                    return self.run_bash(script, tart, f'if vm_exists "{name}"; then echo present; '
                                         'else echo absent; fi', calls)

                self.assertEqual(ask(listing, "owners-x").stdout.strip(), "present")
                self.assertEqual(ask(listing, "owners-").stdout.strip(), "absent")
                result = ask(failing, "owners-x")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("absent", result.stdout)
                self.assertIn("tart list failed; cannot tell whether owners-x exists", result.stderr)
                # One transient failure is retried (vm_exists runs `tart` in a subshell: count in a file).
                calls.write_text("")
                flaky = (f"[ \"$(wc -l <'{calls}')\" -gt 1 ] || return 1; printf 'owners-x\\n';")
                self.assertEqual(ask(flaky, "owners-x").stdout.strip(), "present")

    def test_cleanup_renames_and_deletes_nothing_when_tart_list_fails(self) -> None:
        # A finished build whose swap-in stopped on a failed listing: nothing may be deleted.
        failing_list = "[ \"$1\" != list ] || { echo 'tart: busy' >&2; return 1; };"
        for os_name, script in GOLDENS.items():
            with self.subTest(os_name), tempfile.TemporaryDirectory() as tmp:
                result = self.cleanup(script, failing_list, 1, tmp)
                made = self.calls(tmp)
                self.assertFalse([c for c in made if c.startswith(("rename", "delete"))], made)
                self.assertIn("tart list failed, so nothing was renamed or deleted", result.stderr)

    def test_cleanup_still_deletes_a_failed_build_when_the_listing_works(self) -> None:
        listing = "[ \"$1\" != list ] || printf 'owners-x\\nowners-x-build-1\\n';"
        for os_name, script in GOLDENS.items():
            with self.subTest(os_name), tempfile.TemporaryDirectory() as tmp:
                self.cleanup(script, listing, 0, tmp)
                made = self.calls(tmp)
                self.assertIn("delete owners-x-build-1", made)
                self.assertFalse([c for c in made if c.startswith("rename")], made)

    def test_cleanup_keeps_a_complete_build_that_was_not_swapped_in(self) -> None:
        # The swap-in stopped (a failed listing, a failed rename) and the listing works again by cleanup.
        listing = "[ \"$1\" != list ] || printf 'owners-x\\nowners-x-build-1\\n';"
        for os_name, script in GOLDENS.items():
            with self.subTest(os_name), tempfile.TemporaryDirectory() as tmp:
                result = self.cleanup(script, listing, 1, tmp)
                made = self.calls(tmp)
                self.assertFalse([c for c in made if c.startswith(("rename", "delete"))], made)
                self.assertIn("keeping the complete build owners-x-build-1", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
