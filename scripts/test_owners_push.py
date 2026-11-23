#!/usr/bin/env python3
"""Tests for scripts/owners_push.py and the dispatcher's push mode, with fake processes,
clocks and GitHub (no network, no real forwarder)."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import owners_dispatch as od  # noqa: E402
import owners_push as op  # noqa: E402
from test_owners_dispatch import (  # noqa: E402
    FORK_OFF, FakeGh, FakeProc, FakePush, SchedulerHarness, fork_route, job_json, repo_json, run_json, scan_routes)

od.log_line = lambda message: None  # type: ignore[assignment]

FAKE_TOKEN = "gho_FakeForwarderLOGIN_Value9"
SECRET_VALUE = "Receiver-SECRET-for-tests"


def sign(body: bytes, secret: str = SECRET_VALUE) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def wj(repo: str, action: str = "queued", labels: Optional[List[str]] = None, job_id: int = 7,
       runner: str = "") -> Dict[str, Any]:
    return {"action": action, "repository": {"full_name": repo, "private": True},
            "workflow_job": {"id": job_id, "labels": labels if labels is not None else ["self-hosted", "linux-a64"],
                             "runner_name": runner}}


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeForwarderProc(FakeProc):
    def __init__(self) -> None:
        super().__init__()
        self.pgid = self.pid


class FakeApi:
    def __init__(self) -> None:
        self.hooks: Dict[int, Dict[str, Any]] = {}
        self.deleted: List[int] = []
        self.gets: List[int] = []
        self.requests = 0

    def get_hook(self, target: op.Target, hook_id: int) -> Optional[Dict[str, Any]]:
        self.requests += 1
        self.gets.append(hook_id)
        return self.hooks.get(hook_id)

    def delete_hook(self, target: op.Target, hook_id: int) -> bool:
        self.requests += 1
        self.deleted.append(hook_id)
        self.hooks.pop(hook_id, None)
        return True


class PushHarness(unittest.TestCase):
    """A Supervisor over fake processes: `spawned` records (argv, env, log), `kills` the signals sent
    to each group (SIGTERM/SIGKILL end the fake process), `report` is what the liveness check sees."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.clock = Clock()
        self.spawned: List[Tuple[List[str], Dict[str, str], str, FakeForwarderProc]] = []
        self.kills: List[Tuple[int, int]] = []
        self.report: Dict[int, Dict[str, Any]] = {}
        self.inspected = 0
        self.api = FakeApi()
        self.token_value: Optional[str] = FAKE_TOKEN
        self.procs: Dict[int, FakeForwarderProc] = {}

    def spawn(self, argv: List[str], env: Dict[str, str], log_path: str, err_path: Optional[str] = None) -> Any:
        proc = FakeForwarderProc()
        self.procs[proc.pgid] = proc
        self.spawned.append((argv, env, log_path, proc))
        return proc

    def kill(self, pgid: int, sig: int) -> None:
        self.kills.append((pgid, sig))
        proc = self.procs.get(pgid)
        if proc is not None and sig in (signal.SIGTERM, signal.SIGKILL) and proc.code is None:
            proc.code = -int(sig)

    def inspect(self, pgids: Iterable[int]) -> Dict[int, Dict[str, Any]]:
        self.inspected += 1
        return {pgid: self.report.get(pgid, {"alive": True, "stopped": False, "tcp": ["ESTABLISHED"]})
                for pgid in pgids}

    def token(self) -> str:
        if self.token_value is None:
            raise op.PushError("no github.com oauth_token in /x/hosts.yml: run the forwarder login")
        return self.token_value

    def supervisor(self, **cfg: Any) -> op.Supervisor:
        config = od.parse_config(dict({"owners": ["org1"], "repos": ["me/solo"]}, **cfg))
        receiver = op.Receiver(SECRET_VALUE, clock=self.clock)
        receiver.port = 45678
        return op.Supervisor(config, self.root, op.targets_for(config), receiver=receiver, token=self.token,
                             api=self.api, spawn=self.spawn, inspect=self.inspect,
                             identify=lambda pid: f"T{pid}", kill=self.kill, clock=self.clock,
                             home="/home/u", base_env={"PATH": "/usr/bin:/bin", "HOME": "/home/u",
                                                       "GH_TOKEN": "gho_InheritedShellTOKEN"},
                             binary="/home/u/.local/share/gh/extensions/gh-webhook/gh-webhook")

    def deliver(self, sup: op.Supervisor, key: str, event: str, payload: Dict[str, Any]) -> int:
        body = json.dumps(payload).encode()
        return sup.receiver.handle(f"/f/{key}", {"X-GitHub-Event": event, "X-Hub-Signature-256": sign(body)}, body)

    def ping(self, sup: op.Supervisor, key: str, hook_id: int, name: str = "cli") -> int:
        return self.deliver(sup, key, "ping", {"zen": "z", "hook_id": hook_id, "hook": {"name": name}})

    def proc_of(self, sup: op.Supervisor, key: str) -> FakeForwarderProc:
        return sup.forwarders[key].proc

    def write_log(self, sup: op.Supervisor, key: str, text: str) -> None:
        with open(sup.forwarders[key].log_path, "a", encoding="utf-8") as handle:
            handle.write(text)

    def up(self, sup: op.Supervisor, key: str, hook_id: int) -> op.PushSignals:
        sup.poll()
        self.ping(sup, key, hook_id)
        return sup.poll()


# --------------------------------------------------------------------------- config


class ConfigAndRegistrationTests(unittest.TestCase):
    def test_new_keys_defaults_and_validation(self) -> None:
        cfg = od.parse_config({"owners": ["o"]})
        self.assertEqual(cfg.forwarder_gh_config_dir, "~/.config/tartci/gh-webhooks")
        self.assertEqual(cfg.forwarder_port, 0)
        cfg = od.parse_config({"owners": ["o"], "forwarder_gh_config_dir": "/x/hooks", "forwarder_port": 18801})
        self.assertEqual((cfg.forwarder_gh_config_dir, cfg.forwarder_port), ("/x/hooks", 18801))
        for bad in ({"forwarder_port": -1}, {"forwarder_port": 70000}, {"forwarder_port": "1"},
                    {"forwarder_gh_config_dir": ""}, {"forwarder_gh_config_dir": 3}, {"webhook_dir": "/x"}):
            with self.subTest(bad=bad), self.assertRaises(od.ConfigError):
                od.parse_config(dict({"owners": ["o"]}, **bad))

    def test_rotate_seconds_defaults_to_an_hour_and_zero_turns_it_off(self) -> None:
        self.assertEqual(od.parse_config({"owners": ["o"]}).rotate_seconds, 3600)
        self.assertEqual(od.parse_config({"owners": ["o"], "rotate_seconds": 0}).rotate_seconds, 0)
        self.assertEqual(od.parse_config({"owners": ["o"], "rotate_seconds": 600}).rotate_seconds, 600)
        for bad in (599, 1, 86401, -1, True, "3600", 3600.0):
            with self.subTest(bad=bad), self.assertRaises(od.ConfigError):
                od.parse_config({"owners": ["o"], "rotate_seconds": bad})

    def test_one_org_forwarder_and_one_per_personal_repo(self) -> None:
        cfg = od.parse_config({"owners": ["Org1"], "repos": ["org1/extra", "me/a", "me/b", "other-org/c", "me/x"],
                               "exclude_repos": ["me/x"]})
        self.assertEqual([t.label for t in op.targets_for(cfg)],
                         ["org Org1", "repo me/a", "repo me/b", "repo other-org/c"])
        keys = [t.key for t in op.targets_for(cfg)]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertTrue(all(re_ok(key) for key in keys))

    def test_target_covers(self) -> None:
        org, repo = op.Target("org", "Org1"), op.Target("repo", "me/a")
        self.assertTrue(org.covers("org1/anything"))
        self.assertFalse(org.covers("org10/x"))
        self.assertFalse(org.covers("org1"))
        self.assertTrue(repo.covers("ME/A"))
        self.assertFalse(repo.covers("me/ab"))
        self.assertEqual(org.flag(), ["--org", "Org1"])
        self.assertEqual(repo.hooks_path(), "repos/me/a/hooks")

    def test_a_personal_account_in_owners_is_refused(self) -> None:
        client = FakeGh({"users/org1": {"type": "Organization"}, "users/me": {"type": "User"}})
        with self.assertRaises(od.ConfigError) as caught:
            op.check_owner_types(client, ["org1", "me"])
        self.assertIn("me is a personal account", str(caught.exception))
        self.assertIn("repos", str(caught.exception))

    def test_an_unreadable_owner_type_is_reported_not_refused(self) -> None:
        client = FakeGh({"users/org1": {"type": "Organization"}, "users/gone": od.GhError("HTTP 502", status=502)})
        self.assertEqual(len(op.check_owner_types(client, ["org1", "gone"])), 1)

    def test_the_directory_never_serves_a_personal_owner(self) -> None:
        routes: Dict[str, Any] = {"users/me": {"type": "User"}, "user": {"login": "me"},
                                  "user/repos?affiliation=owner&visibility=private": [repo_json("me/a")],
                                  fork_route("me/a"): FORK_OFF}
        current = od.Directory(FakeGh(dict(routes)), od.parse_config({"owners": ["me"]})).refresh()
        self.assertEqual(current.served, {})
        self.assertIn("personal account", current.errors["me"])


def re_ok(key: str) -> bool:
    import re
    return re.fullmatch(r"[A-Za-z0-9._-]+", key) is not None


# --------------------------------------------------------------------------- the login


class TokenTests(unittest.TestCase):
    def write(self, text: str) -> str:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        Path(tmp, "hosts.yml").write_text(text)
        return tmp

    def test_reads_the_active_users_token(self) -> None:
        path = self.write("github.com:\n    users:\n        bob:\n            oauth_token: gho_BobsOtherTOKEN\n"
                          f"        alice:\n            oauth_token: {FAKE_TOKEN}\n"
                          "    git_protocol: ssh\n    user: alice\n")
        self.assertEqual(op.read_forwarder_token(path), FAKE_TOKEN)

    def test_reads_a_host_level_token_and_a_single_user(self) -> None:
        self.assertEqual(op.read_forwarder_token(self.write(f"github.com:\n  oauth_token: {FAKE_TOKEN}\n  user: a\n")),
                         FAKE_TOKEN)
        self.assertEqual(op.read_forwarder_token(self.write(
            f"github.com:\n    users:\n        a:\n            oauth_token: {FAKE_TOKEN}\n")), FAKE_TOKEN)

    def test_other_hosts_are_ignored_and_errors_never_carry_a_token(self) -> None:
        path = self.write(f"ghe.example.com:\n    oauth_token: {FAKE_TOKEN}\n")
        with self.assertRaises(op.PushError) as caught:
            op.read_forwarder_token(path)
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))
        with self.assertRaises(op.PushError):
            op.read_forwarder_token("/nonexistent/forwarder-login")

    def test_mask(self) -> None:
        for token in ("ghp_AbC123_x", "gho_x", "ghs_Q", "ghu_zz9", FAKE_TOKEN):
            self.assertEqual(op.mask(f"a {token} b"), "a gh*_*** b")
        self.assertEqual(od._mask_tokens(f"x={FAKE_TOKEN}"), "x=gh*_***")

    def test_forwarder_env_has_only_its_own_token(self) -> None:
        env = op.forwarder_env({"PATH": "/usr/bin", "HOME": "/h", "GITHUB_TOKEN": "gho_ShellOtherTOKEN",
                                "TARTCI_X": "1", "GH_TOKEN": "gho_InheritedShellTOKEN"}, FAKE_TOKEN, "~/.config/x")
        self.assertEqual(env["GH_TOKEN"], FAKE_TOKEN)
        self.assertEqual(env["GH_CONFIG_DIR"], os.path.expanduser("~/.config/x"))
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("TARTCI_X", env)


# --------------------------------------------------------------------------- receiver


class ReceiverTests(unittest.TestCase):
    def receiver(self) -> op.Receiver:
        rec = op.Receiver(SECRET_VALUE, clock=lambda: 5.0)
        rec.allow("repo-me__a")
        return rec

    def post(self, rec: op.Receiver, payload: Any, event: str = "workflow_job", path: str = "/f/repo-me__a",
             signature: Optional[str] = None) -> int:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        headers = {"X-GitHub-Event": event}
        headers["X-Hub-Signature-256"] = sign(body) if signature is None else signature
        if signature == "":
            del headers["X-Hub-Signature-256"]
        return rec.handle(path, headers, body)

    def test_a_signed_queued_job_becomes_an_event(self) -> None:
        rec = self.receiver()
        self.assertEqual(self.post(rec, wj("me/a", labels=["Self-Hosted", "LINUX-A64"])), 200)
        (event,) = rec.drain()
        self.assertEqual((event.key, event.event, event.action, event.repo), ("repo-me__a", "workflow_job", "queued", "me/a"))
        self.assertEqual(event.labels, ("self-hosted", "linux-a64"))
        self.assertEqual(rec.stats["accepted"], 1)

    def test_unsigned_wrongly_signed_and_unknown_paths_are_dropped(self) -> None:
        rec = self.receiver()
        self.assertEqual(self.post(rec, wj("me/a"), signature=""), 401)
        self.assertEqual(self.post(rec, wj("me/a"), signature=sign(b"other body")), 401)
        self.assertEqual(self.post(rec, wj("me/a"), signature=sign(json.dumps(wj("me/a")).encode(), "Wrong-SECRET")), 401)
        self.assertEqual(self.post(rec, wj("me/a"), path="/f/repo-me__b"), 404)
        self.assertEqual(self.post(rec, wj("me/a"), path="/"), 404)
        self.assertEqual(self.post(rec, wj("me/a"), path="/f/../f/repo-me__a"), 404)
        self.assertEqual(self.post(rec, b"not json"), 400)
        self.assertEqual(self.post(rec, [1, 2]), 400)
        self.assertEqual(rec.drain(), [])
        self.assertEqual(rec.stats["bad_signature"], 3)
        self.assertEqual(rec.stats["unknown_path"], 3)

    def test_ping_carries_the_hook_and_other_events_are_ignored(self) -> None:
        rec = self.receiver()
        self.assertEqual(self.post(rec, {"zen": "z", "hook_id": 99, "hook": {"name": "cli"}}, event="ping"), 200)
        self.assertEqual(self.post(rec, {"action": "opened"}, event="issues"), 200)
        (ping,) = rec.drain()
        self.assertEqual((ping.event, ping.hook_id, ping.hook_name), ("ping", 99, "cli"))
        self.assertEqual(rec.stats["ignored"], 1)

    def test_real_server_binds_loopback_caps_the_body_and_needs_a_length(self) -> None:
        rec = self.receiver()
        rec.max_body = 64
        rec.start()
        self.addCleanup(rec.close)
        self.assertNotEqual(rec.port, 0)
        self.assertTrue(rec.url("repo-me__a").startswith(f"http://127.0.0.1:{rec.port}/f/"))
        body = json.dumps({"zen": "z", "hook_id": 5}).encode()

        def request(data: bytes, headers: Dict[str, str]) -> int:
            conn = http.client.HTTPConnection("127.0.0.1", rec.port, timeout=5)
            try:
                conn.request("POST", "/f/repo-me__a", body=data, headers=headers)
                return conn.getresponse().status
            finally:
                conn.close()

        self.assertEqual(request(body, {"X-GitHub-Event": "ping", "X-Hub-Signature-256": sign(body)}), 200)
        self.assertEqual(request(b"x" * 65, {"X-GitHub-Event": "ping", "X-Hub-Signature-256": sign(b"x" * 65)}), 413)
        conn = http.client.HTTPConnection("127.0.0.1", rec.port, timeout=5)
        conn.putrequest("POST", "/f/repo-me__a")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 411)
        conn.close()
        self.assertEqual([e.hook_id for e in rec.drain()], [5])
        self.assertEqual(rec._server.server_address[0], "127.0.0.1")


# --------------------------------------------------------------------------- supervisor


class SupervisorTests(PushHarness):
    ORG, SOLO = "org-org1", "repo-me__solo"

    def test_start_then_ping_is_up_and_asks_for_a_catch_up(self) -> None:
        sup = self.supervisor()
        signals = sup.poll()
        self.assertEqual(signals.established, [])
        self.assertEqual(len(self.spawned), 2)
        argv, env, log_path, _ = self.spawned[0]
        self.assertEqual(argv[:4], [sup.binary, "forward", "--org", "org1"])
        self.assertIn("--secret", argv)
        self.assertEqual(argv[argv.index("--url") + 1], "http://127.0.0.1:45678/f/org-org1")
        self.assertEqual(env["GH_TOKEN"], FAKE_TOKEN)
        self.assertEqual(sup.forwarders[self.ORG].state, "starting")
        self.assertEqual(self.ping(sup, self.ORG, 111), 200)
        signals = sup.poll()
        self.assertEqual([t.key for t in signals.established], [self.ORG])
        fwd = sup.forwarders[self.ORG]
        self.assertEqual((fwd.state, fwd.hook_id, fwd.restarts), ("up", 111, 0))
        saved = json.loads(Path(sup.state_path).read_text())
        self.assertEqual(saved["hooks"][self.ORG], [111])
        self.assertEqual(saved["live"][self.ORG]["pid_started"], f"T{fwd.proc.pid}")

    def test_an_up_forwarder_is_rotated_hourly_and_catches_up_again(self) -> None:
        self.api.hooks = {1: {"id": 1, "name": "cli"}, 2: {"id": 2, "name": "cli"}}  # the 30-min check finds them
        sup = self.supervisor()
        sup.poll()
        self.ping(sup, self.SOLO, 1)
        sup.poll()
        fwd = sup.forwarders[self.SOLO]
        up_at = self.clock.now
        self.assertGreaterEqual(fwd.rotate_at, up_at + 3600)
        self.assertLess(fwd.rotate_at, up_at + 3600 + op.ROTATE_STAGGER_MAX_SECONDS)
        old = self.proc_of(sup, self.SOLO)
        self.clock.now = fwd.rotate_at - 1
        sup.poll()
        self.assertIs(self.proc_of(sup, self.SOLO), old)  # not yet
        self.clock.now = fwd.rotate_at
        spawned = len(self.spawned)
        signals = sup.poll()
        self.assertEqual(signals.established, [])
        self.assertEqual(fwd.state, "rotating")
        self.assertIsNone(fwd.proc)
        self.assertIn((old.pgid, signal.SIGCONT), self.kills)  # the whole group, stopped ones too
        self.assertIn((old.pgid, signal.SIGTERM), self.kills)
        self.assertEqual(fwd.last_error, "")  # planned: not an error
        self.assertEqual(sup.stats["rotations"], 1)
        self.assertEqual(sup.summary()["forwarders"][1]["state"], "rotating")
        self.clock.now += op.RESTART_DELAY_SECONDS - 1
        sup.poll()
        self.assertEqual(len(self.spawned), spawned)  # GitHub needs a few seconds to drop the old hook
        self.clock.now += 1
        sup.poll()
        self.assertEqual(len(self.spawned), spawned + 1)
        self.assertEqual(fwd.state, "starting")
        self.ping(sup, self.SOLO, 2)
        signals = sup.poll()
        self.assertEqual([t.key for t in signals.established], [self.SOLO])  # the catch-up scan
        self.assertEqual((fwd.state, fwd.hook_id, fwd.rotations, fwd.restarts), ("up", 2, 1, 0))
        self.assertGreaterEqual(fwd.rotate_at, self.clock.now + 3600)  # the next one is planned

    def test_rotation_is_staggered_per_target_and_off_at_zero(self) -> None:
        sup = self.supervisor()
        sup.poll()
        self.ping(sup, self.ORG, 1)
        self.ping(sup, self.SOLO, 2)
        sup.poll()
        org, solo = sup.forwarders[self.ORG], sup.forwarders[self.SOLO]
        self.assertNotEqual(org.rotate_at, solo.rotate_at)
        rows = {row["target"]: row for row in sup.summary()["forwarders"]}
        self.assertTrue(all(3600 <= row["rotate_in_s"] <= 3600 + 300 for row in rows.values()))
        self.api.hooks = {3: {"id": 3, "name": "cli"}}
        off = self.supervisor(rotate_seconds=0)
        off.poll()
        self.ping(off, self.SOLO, 3)
        off.poll()
        fwd = off.forwarders[self.SOLO]
        self.assertIsNone(fwd.rotate_at)
        proc = fwd.proc
        self.clock.now += 86400
        off.poll()
        self.assertIs(fwd.proc, proc)
        self.assertEqual(fwd.state, "up")

    def test_a_rotation_that_meets_a_lingering_hook_backs_off_then_recovers(self) -> None:
        sup = self.supervisor()
        sup.poll()
        self.ping(sup, self.SOLO, 1)
        sup.poll()
        fwd = sup.forwarders[self.SOLO]
        self.clock.now = fwd.rotate_at
        sup.poll()
        self.clock.now += op.RESTART_DELAY_SECONDS
        sup.poll()
        self.write_log(sup, self.SOLO, "Error: HTTP 422: Validation Failed (Hook already exists on this repository)\n")
        self.proc_of(sup, self.SOLO).code = 1
        sup.poll()
        self.assertEqual(fwd.state, "backing-off")
        self.assertEqual(fwd.next_start_at, self.clock.now + op.BACKOFF_STEPS[0])
        self.clock.now = fwd.next_start_at
        sup.poll()
        self.ping(sup, self.SOLO, 4)
        signals = sup.poll()
        self.assertEqual([t.key for t in signals.established], [self.SOLO])
        self.assertEqual(fwd.state, "up")

    def test_without_the_gh_webhook_binary_it_runs_gh_webhook_forward(self) -> None:
        sup = self.supervisor()
        sup.binary = None
        sup.poll()
        self.assertEqual(self.spawned[0][0][:4], ["gh", "webhook", "forward", "--org"])

    def test_exit_restarts_and_the_new_ping_catches_up_again(self) -> None:
        sup = self.supervisor()
        sup.poll()
        self.ping(sup, self.SOLO, 1)
        sup.poll()
        self.clock.now += 3600
        self.write_log(sup, self.SOLO, "Error: error receiving json event: websocket: close 1006 (abnormal closure)\n")
        self.proc_of(sup, self.SOLO).code = 1
        sup.poll()
        fwd = sup.forwarders[self.SOLO]
        self.assertEqual(fwd.state, "backing-off")
        self.assertIn("close 1006", fwd.last_error)
        self.assertEqual(fwd.next_start_at, self.clock.now + op.RESTART_DELAY_SECONDS)
        self.assertIn((self.spawned[1][3].pgid, signal.SIGKILL), self.kills)  # the whole group, orphans too
        before = len(self.spawned)
        sup.poll()
        self.assertEqual(len(self.spawned), before)  # not yet
        self.clock.now += op.RESTART_DELAY_SECONDS
        sup.poll()
        self.assertEqual(len(self.spawned), before + 1)
        self.assertEqual(fwd.restarts, 1)
        self.ping(sup, self.SOLO, 2)
        self.assertEqual([t.key for t in sup.poll().established], [self.SOLO])
        self.assertEqual(sup.hooks[self.SOLO], [1, 2])

    def test_422_backs_off_10_20_40_80_120(self) -> None:
        sup = self.supervisor(owners=[])
        delays = []
        for _ in range(6):
            sup.poll()
            self.write_log(sup, self.SOLO, "Error: error creating webhook: HTTP 422: Validation Failed (x)\n"
                                           "Hook already exists on this repository\nUsage:\n  forward\n")
            self.proc_of(sup, self.SOLO).code = 1
            sup.poll()
            fwd = sup.forwarders[self.SOLO]
            self.assertEqual(fwd.state, "backing-off")
            self.assertIn("Hook already exists", fwd.last_error)
            self.assertNotIn("Usage", fwd.last_error)
            delays.append(fwd.next_start_at - self.clock.now)
            self.clock.now = fwd.next_start_at
        self.assertEqual(delays, [10.0, 20.0, 40.0, 80.0, 120.0, 120.0])

    def conflict(self, sup: op.Supervisor) -> None:
        sup.poll()
        self.write_log(sup, self.SOLO, "Error: error creating webhook: HTTP 422: Validation Failed\n"
                                       "Hook already exists on this repository\n")
        self.proc_of(sup, self.SOLO).code = 1
        sup.poll()

    def test_a_stale_recorded_cli_hook_is_deleted_after_60s_of_422(self) -> None:
        sup = self.supervisor(owners=[])
        sup.hooks[self.SOLO] = [41, 42]
        self.api.hooks = {41: {"id": 41, "name": "cli"}, 42: {"id": 42, "name": "web"}}
        self.conflict(sup)
        self.clock.now += 30
        sup.poll()
        self.assertEqual(self.api.deleted, [])  # not before 60 s
        for _ in range(4):
            fwd = sup.forwarders[self.SOLO]
            self.clock.now = max(self.clock.now + 1, fwd.next_start_at)
            if fwd.proc is None:
                sup.poll()
            if fwd.proc is not None:
                self.write_log(sup, self.SOLO, "Error: HTTP 422\nHook already exists on this repository\n")
                fwd.proc.code = 1
                sup.poll()
        self.assertEqual(self.api.deleted, [41])  # recorded AND named cli; 42 is not a cli hook
        self.assertEqual(sup.hooks[self.SOLO], [42])

    def test_a_conflicting_hook_this_pool_never_recorded_is_left_alone(self) -> None:
        sup = self.supervisor(owners=[])
        self.api.hooks = {77: {"id": 77, "name": "cli"}}
        self.conflict(sup)
        self.clock.now += 200
        sup.poll()
        self.assertEqual((self.api.deleted, self.api.gets), ([], []))
        self.assertEqual(sup.forwarders[self.SOLO].state, "starting")  # it keeps trying, backing off

    def test_stopped_process_is_restarted_at_once(self) -> None:
        sup = self.supervisor(owners=[])
        sup.poll()
        self.ping(sup, self.SOLO, 5)
        sup.poll()
        old = self.proc_of(sup, self.SOLO)
        self.report[old.pgid] = {"alive": True, "stopped": True, "tcp": ["ESTABLISHED"]}
        self.clock.now += op.LIVENESS_EVERY_SECONDS
        sup.poll()
        self.assertEqual(self.kills[:2], [(old.pgid, signal.SIGCONT), (old.pgid, signal.SIGTERM)])
        fwd = sup.forwarders[self.SOLO]
        self.assertEqual(fwd.state, "stalled")
        self.assertIn("state T", fwd.last_error)
        self.clock.now += op.RESTART_DELAY_SECONDS
        sup.poll()
        self.assertIsNot(fwd.proc, old)
        self.assertEqual(fwd.state, "starting")

    def test_close_wait_twice_restarts_once_only_checks_the_hook(self) -> None:
        sup = self.supervisor(owners=[])
        sup.poll()
        self.ping(sup, self.SOLO, 5)
        sup.poll()
        self.api.hooks = {5: {"id": 5, "name": "cli"}}
        proc = self.proc_of(sup, self.SOLO)
        self.report[proc.pgid] = {"alive": True, "stopped": False, "tcp": ["ESTABLISHED", "CLOSE_WAIT"]}
        self.clock.now += op.LIVENESS_EVERY_SECONDS
        sup.poll()
        self.assertEqual(sup.forwarders[self.SOLO].state, "up")
        self.assertEqual(self.api.gets, [5])  # suspicion: one REST check
        self.clock.now += op.LIVENESS_EVERY_SECONDS
        sup.poll()
        self.assertEqual(sup.forwarders[self.SOLO].state, "stalled")
        self.assertIn("CLOSE_WAIT", sup.forwarders[self.SOLO].last_error)

    def test_a_healthy_forwarder_costs_one_local_check_per_interval_and_rare_rest(self) -> None:
        sup = self.supervisor()
        sup.poll()
        self.ping(sup, self.ORG, 1)
        self.ping(sup, self.SOLO, 2)
        sup.poll()
        self.api.hooks = {1: {"name": "cli"}, 2: {"name": "cli"}}
        checks = self.inspected
        for _ in range(600):  # 10 minutes of 1 s ticks
            self.clock.now += 1
            sup.poll()
        self.assertEqual(self.inspected - checks, 600 // int(op.LIVENESS_EVERY_SECONDS))
        self.assertEqual(self.api.requests, 0)
        self.clock.now += op.HOOK_CHECK_SECONDS
        sup.poll()
        self.assertEqual(sorted(self.api.gets), [1, 2])

    def test_a_hook_gone_on_github_restarts_the_forwarder(self) -> None:
        sup = self.supervisor(owners=[])
        sup.poll()
        self.ping(sup, self.SOLO, 9)
        sup.poll()
        self.clock.now += op.HOOK_CHECK_SECONDS
        sup.poll()
        self.assertEqual(sup.forwarders[self.SOLO].state, "stalled")
        self.assertIn("gone", sup.forwarders[self.SOLO].last_error)

    def test_no_ping_but_alive_is_assumed_up_after_the_timeout(self) -> None:
        sup = self.supervisor(owners=[])
        sup.poll()
        self.clock.now += op.PING_TIMEOUT_SECONDS
        signals = sup.poll()
        self.assertEqual([t.key for t in signals.established], [self.SOLO])
        self.assertIn("assumed up", sup.forwarders[self.SOLO].last_error)

    def test_a_missing_login_backs_off_and_spawns_nothing(self) -> None:
        self.token_value = None
        sup = self.supervisor(owners=[])
        sup.poll()
        fwd = sup.forwarders[self.SOLO]
        self.assertEqual((self.spawned, fwd.state), ([], "backing-off"))
        self.assertIn("oauth_token", fwd.last_error)
        self.token_value = FAKE_TOKEN
        self.clock.now = fwd.next_start_at
        sup.poll()
        self.assertEqual(len(self.spawned), 1)

    def test_events_are_routed_and_foreign_repositories_dropped(self) -> None:
        sup = self.supervisor()
        sup.poll()
        self.assertEqual(self.deliver(sup, self.ORG, "workflow_job", wj("org1/a")), 200)
        self.assertEqual(self.deliver(sup, self.ORG, "workflow_job", wj("evil/a")), 200)
        self.assertEqual(self.deliver(sup, self.SOLO, "workflow_job", wj("org1/a")), 200)
        signals = sup.poll()
        self.assertEqual([e.repo for e in signals.events], ["org1/a"])
        self.assertEqual(sup.stats["foreign"], 2)
        self.assertEqual([t.key for t in signals.established], [self.ORG])  # an event proves delivery
        self.assertIsNotNone(sup.forwarders[self.ORG].last_event_at)

    def test_shutdown_kills_every_group_and_clears_the_record(self) -> None:
        sup = self.supervisor()
        sup.poll()
        groups = sorted(p.pgid for _, _, _, p in self.spawned)
        sup.shutdown()
        for pgid in groups:
            self.assertIn((pgid, signal.SIGCONT), self.kills)
            self.assertIn((pgid, signal.SIGTERM), self.kills)
        self.assertEqual(json.loads(Path(sup.state_path).read_text())["live"], {})
        self.assertTrue(all(f.state == "stopped" for f in sup.forwarders.values()))
        sup.poll()
        self.assertEqual(len(self.spawned), 2)  # nothing restarts after shutdown

    def test_the_token_reaches_only_the_forwarder_env(self) -> None:
        sup = self.supervisor()
        sup.poll()
        self.ping(sup, self.ORG, 3)
        sup.poll()
        for argv, env, log_path, _ in self.spawned:
            self.assertEqual(env["GH_TOKEN"], FAKE_TOKEN)
            self.assertNotIn(FAKE_TOKEN, " ".join(argv))
        self.assertNotIn(FAKE_TOKEN, Path(sup.state_path).read_text())
        self.assertNotIn(FAKE_TOKEN, json.dumps(sup.summary()))
        for _, _, log_path, _ in self.spawned:
            self.assertNotIn(FAKE_TOKEN, Path(log_path).read_text())
        self.assertNotEqual(os.environ.get("GH_TOKEN"), FAKE_TOKEN)

    def test_stop_recorded_stops_only_verified_leftovers(self) -> None:
        state = Path(self.root, "state")
        state.mkdir()
        (state / op.STATE_FILE).write_text(json.dumps({"live": {
            "a": {"pid": 4001, "pgid": 4001, "pid_started": "S1", "target": "repo me/a"},
            "b": {"pid": 4002, "pgid": 4002, "pid_started": "WRONG", "target": "repo me/b"},
            "c": {"pid": 4003, "pgid": 4003, "target": "repo me/c"}}, "hooks": {"a": [1]}}))
        alive = {4001: True}

        def is_alive(pid: Any, started: str) -> bool:
            return started == "S1" and alive.get(int(pid), False)

        def kill(pgid: int, sig: int) -> None:
            self.kills.append((pgid, sig))
            if sig == signal.SIGTERM:
                alive[pgid] = False

        stopped = op.stop_recorded(self.root, alive=is_alive, kill=kill, sleep=lambda s: None)
        self.assertEqual(stopped, ["repo me/a (pid 4001)"])
        self.assertEqual({pgid for pgid, _ in self.kills}, {4001})
        data = json.loads((state / op.STATE_FILE).read_text())
        self.assertEqual((data["live"], data["hooks"]), ({}, {"a": [1]}))  # the hook record survives


class LivenessParsingTests(unittest.TestCase):
    def test_one_ps_and_one_lsof_for_all_groups(self) -> None:
        calls: List[List[str]] = []

        def run(argv: List[str], **kw: Any) -> Any:
            calls.append(argv)
            if argv[0] == "ps":
                return mock.Mock(stdout="  10 10 Ss   Mon Nov 23 10:00:01 2026\n  11 10 S    Mon Nov 23 10:00:02 2026\n"
                                        "  20 20 T    Mon Nov 23 10:00:03 2026\n  30 30 S    Mon Nov 23 10:00:04 2026\n"
                                        "  99 99 R    Mon Nov 23 10:00:05 2026\n", returncode=0)
            return mock.Mock(returncode=0, stdout="p10\ng10\nf6\nn10.0.0.2:5->140.82.1.1:443\nTST=ESTABLISHED\n"
                             "f7\nn127.0.0.1:6->127.0.0.1:45678\nTST=CLOSE_WAIT\n"
                             "p20\ng20\nf6\nn10.0.0.2:7->140.82.1.1:443\nTST=CLOSE_WAIT\n")

        report = op.inspect_groups([10, 20, 30, 40], run=run)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-1], "pid=,pgid=,stat=,lstart=")  # group and start time in one reading
        self.assertEqual(calls[1][calls[1].index("-g") + 1], "10,20,30,40")
        self.assertEqual(report[10], {"alive": True, "stopped": False, "tcp": ["ESTABLISHED"],  # loopback ignored
                                      "pids": [10, 11], "started": {10: "Mon Nov 23 10:00:01 2026",
                                                                    11: "Mon Nov 23 10:00:02 2026"}})
        self.assertEqual(report[20], {"alive": True, "stopped": True, "tcp": ["CLOSE_WAIT"], "pids": [20],
                                      "started": {20: "Mon Nov 23 10:00:03 2026"}})
        self.assertEqual(report[30]["tcp"], [])
        self.assertFalse(report[40]["alive"])
        self.assertEqual(op.stall_reason(report[10]), "")
        self.assertIn("state T", op.stall_reason(report[20]))
        self.assertIn("no ESTABLISHED", op.stall_reason(report[30]))
        self.assertEqual(op.stall_reason({"alive": True, "stopped": False, "tcp": None}), "")

    def test_classify_exit(self) -> None:
        self.assertEqual(op.classify_exit(["Error: error creating webhook: HTTP 401: Bad credentials (x)", "Usage:"])[0],
                         "auth")
        self.assertEqual(op.classify_exit(["Forwarding Webhook events from GitHub...",
                                           "Error: error receiving json event: websocket: close 1006"])[0], "exit")
        kind, error = op.classify_exit(["Error: x", f"token {FAKE_TOKEN} leaked?"])
        self.assertNotIn(FAKE_TOKEN, error)


    def test_lsof_exit_1_with_no_output_is_unknown_not_no_socket(self) -> None:
        def run(argv: List[str], **kw: Any) -> Any:
            if argv[0] == "ps":
                return mock.Mock(stdout="  10 10 Ss\n  11 10 S\n", returncode=0)
            return mock.Mock(returncode=1, stdout="", stderr="lsof: WARNING: can't stat()")

        report = op.inspect_groups([10], run=run)
        self.assertIsNone(report[10]["tcp"])
        self.assertEqual(op.stall_reason(report[10]), "")

    def test_lsof_exit_1_with_no_output_at_all_is_no_socket(self) -> None:
        def run(argv: List[str], **kw: Any) -> Any:
            if argv[0] == "ps":
                return mock.Mock(stdout="  10 10 Ss\n", returncode=0)
            return mock.Mock(returncode=1, stdout="", stderr="")  # this host's lsof: nothing matched

        report = op.inspect_groups([10], run=run)
        self.assertEqual(report[10]["tcp"], [])
        self.assertIn("no ESTABLISHED", op.stall_reason(report[10]))


class ReviewRoundOneSupervisorTests(PushHarness):
    """Restart throttling (finding 4), the unverified owner type and the loud down log (5), the
    orphaned forwarder child (6) and the receiver's read timeout (7)."""
    ORG, SOLO = "org-org1", "repo-me__solo"

    def test_an_up_then_exit_loop_backs_off_and_limits_catch_ups(self) -> None:
        sup = self.supervisor(owners=[])
        fwd = sup.forwarders[self.SOLO]
        catchups = 0
        delays: List[float] = []
        end = self.clock.now + 600
        hook = 0
        while self.clock.now < end:
            self.clock.now = max(self.clock.now, fwd.next_start_at)
            hook += 1
            catchups += len(self.up(sup, self.SOLO, hook).established)
            self.clock.now += 10  # up for 10 s, then it dies (a misfiring trigger)
            self.write_log(sup, self.SOLO, "Error: error receiving json event: websocket: close 1006\n")
            fwd.proc.code = 1
            sup.poll()
            delays.append(fwd.next_start_at - self.clock.now)
        self.assertEqual(delays[:6], [10.0, 20.0, 40.0, 80.0, 120.0, 120.0])
        self.assertLessEqual(catchups, 9)  # unthrottled (5 s restarts) it was 40 in 10 minutes
        # once it STAYS up, the ladder resets and an ordinary exit comes straight back
        self.clock.now = fwd.next_start_at
        self.up(sup, self.SOLO, 99)
        self.clock.now += op.FAST_EXIT_SECONDS
        sup.poll()
        self.assertEqual(fwd.backoff_index, 0)
        self.clock.now += 600
        fwd.proc.code = 1
        sup.poll()
        self.assertEqual(fwd.next_start_at - self.clock.now, op.RESTART_DELAY_SECONDS)

    def test_repeated_forced_restarts_back_off_and_a_rotation_never_does(self) -> None:
        sup = self.supervisor(owners=[])
        fwd = sup.forwarders[self.SOLO]
        delays = []
        for hook in range(1, 5):
            self.clock.now = max(self.clock.now, fwd.next_start_at)
            self.up(sup, self.SOLO, hook)
            self.report[fwd.proc.pgid] = {"alive": True, "stopped": True, "tcp": ["ESTABLISHED"]}
            self.clock.now += op.LIVENESS_EVERY_SECONDS
            sup.poll()
            self.assertEqual(fwd.state, "stalled")
            delays.append(fwd.next_start_at - self.clock.now)
        self.assertEqual(delays, [op.RESTART_DELAY_SECONDS, 10.0, 20.0, 40.0])
        # a stall long after the last one is a fresh start of the ladder
        self.clock.now = fwd.next_start_at
        self.up(sup, self.SOLO, 10)
        self.clock.now += op.FORCED_RESTART_WINDOW_SECONDS
        sup.next_liveness = 0.0
        self.report[fwd.proc.pgid] = {"alive": True, "stopped": True, "tcp": ["ESTABLISHED"]}
        sup.poll()
        self.assertEqual(fwd.next_start_at - self.clock.now, op.RESTART_DELAY_SECONDS)
        # a planned rotation is not a failure: no backoff, whatever came before
        self.clock.now = fwd.next_start_at
        self.up(sup, self.SOLO, 11)
        fwd.backoff_index = 3
        self.clock.now = fwd.rotate_at
        sup.poll()
        self.assertEqual((fwd.state, fwd.backoff_index, fwd.last_error), ("rotating", 0, ""))
        self.assertEqual(fwd.next_start_at - self.clock.now, op.RESTART_DELAY_SECONDS)

    def test_an_unknown_lsof_answer_restarts_nothing(self) -> None:
        sup = self.supervisor()
        sup.poll()
        self.ping(sup, self.ORG, 1)
        self.ping(sup, self.SOLO, 2)
        sup.poll()
        org, solo = sup.forwarders[self.ORG], sup.forwarders[self.SOLO]
        self.api.hooks[1] = {"id": 1, "name": "cli"}
        # one CLOSE_WAIT reading of the org forwarder: a suspicion and one hook read, no restart
        self.report[org.proc.pgid] = {"alive": True, "stopped": False, "tcp": ["CLOSE_WAIT"]}
        self.clock.now += op.LIVENESS_EVERY_SECONDS
        sup.poll()
        self.assertEqual((org.state, org.suspect, self.api.gets), ("up", "its GitHub socket is in CLOSE_WAIT", [1]))
        for fwd in (org, solo):
            self.report[fwd.proc.pgid] = {"alive": True, "stopped": False, "tcp": None}
        spawned, restarts = len(self.spawned), sup.stats["restarts_forced"]
        for _ in range(10):
            self.clock.now += op.LIVENESS_EVERY_SECONDS
            sup.poll()
        self.assertEqual((len(self.spawned), sup.stats["restarts_forced"]), (spawned, restarts))
        self.assertEqual((org.state, solo.state), ("up", "up"))
        # neither a strike nor a clean bill: the suspicion stands, and GitHub is not asked again
        self.assertEqual((org.suspect, solo.suspect), ("its GitHub socket is in CLOSE_WAIT", ""))
        self.assertEqual(self.api.gets, [1])
        # the next reading that can say confirms it
        self.report[org.proc.pgid] = {"alive": True, "stopped": False, "tcp": ["CLOSE_WAIT"]}
        self.clock.now += op.LIVENESS_EVERY_SECONDS
        sup.poll()
        self.assertEqual((org.state, solo.state), ("stalled", "up"))

    def test_an_auth_failure_is_logged_as_down_at_most_every_10_minutes(self) -> None:
        self.token_value = None
        sup = self.supervisor(owners=[])
        logged: List[str] = []
        with mock.patch.object(od, "log_line", logged.append):
            for _ in range(60):  # 20 minutes of failing starts at the 120 s cap
                sup.poll()
                fwd = sup.forwarders[self.SOLO]
                self.assertLessEqual(fwd.next_start_at - self.clock.now, 120.0)
                self.clock.now += 20
        down = [line for line in logged if "is DOWN" in line]
        self.assertEqual(len(down), 2)
        self.assertIn("NOTHING is served for repo me/solo", down[0])

    def test_an_org_whose_type_could_not_be_read_waits_for_it(self) -> None:
        answers: List[Any] = [od.GhError("HTTP 502", status=502), None, "Organization"]

        def owner_type(owner: str) -> Optional[str]:
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        sup = self.supervisor()
        sup.owner_type = owner_type
        org = sup.forwarders[self.ORG]
        org.type_ok, org.state = False, "checking"
        sup.poll()
        self.assertEqual([argv[2] for argv, _, _, _ in self.spawned], ["--repo"])  # the org waits
        self.assertEqual(org.state, "checking")
        self.assertIn("502", org.last_error)
        self.assertEqual(org.next_start_at - self.clock.now, op.BACKOFF_STEPS[0])
        self.clock.now = org.next_start_at
        sup.poll()
        self.assertEqual(org.next_start_at - self.clock.now, op.BACKOFF_STEPS[1])
        self.assertEqual(len(self.spawned), 1)
        self.clock.now = org.next_start_at
        sup.poll()
        self.assertEqual([argv[2] for argv, _, _, _ in self.spawned], ["--repo", "--org"])
        self.assertEqual((org.state, org.backoff_index), ("starting", 0))

    def test_a_personal_owner_found_late_is_refused_not_started(self) -> None:
        sup = op.Supervisor(od.parse_config({"owners": ["me"]}), self.root, [op.Target("org", "me")],
                            receiver=op.Receiver(SECRET_VALUE, clock=self.clock), token=self.token, api=self.api,
                            spawn=self.spawn, inspect=self.inspect, identify=lambda pid: f"T{pid}", kill=self.kill,
                            clock=self.clock, home="/home/u", base_env={}, binary="/x/gh-webhook",
                            owner_type=lambda owner: "User", unverified=["ME"])
        for _ in range(5):
            sup.poll()
            self.clock.now += 200
        fwd = sup.forwarders["org-me"]
        self.assertEqual((self.spawned, fwd.state), ([], "refused"))
        self.assertIn("personal account", fwd.last_error)
        self.assertIsNone(sup.summary()["forwarders"][0]["retry_in_s"])

    def test_the_forwarder_child_is_recorded_and_an_orphan_of_a_dead_gh_is_stopped(self) -> None:
        sup = self.supervisor(owners=[])
        self.up(sup, self.SOLO, 1)
        proc = self.proc_of(sup, self.SOLO)
        child = proc.pid + 1000
        self.report[proc.pgid] = {"alive": True, "stopped": False, "tcp": ["ESTABLISHED"], "pids": [proc.pid, child],
                                  "started": {proc.pid: f"T{proc.pid}", child: f"T{child}"}}
        self.clock.now += op.LIVENESS_EVERY_SECONDS
        sup.poll()
        saved = json.loads(Path(sup.state_path).read_text())["live"][self.SOLO]
        self.assertEqual(saved["members"], {str(child): f"T{child}"})
        # the dispatcher crashes, then gh (the leader) dies; its gh-webhook child keeps the hook
        state = Path(sup.state_path)
        data = json.loads(state.read_text())
        data["live"][self.SOLO]["members"][str(child + 1)] = "RECYCLED"  # a pid now someone else's
        state.write_text(json.dumps(data))
        alive = {child: f"T{child}", child + 1: "OTHER"}
        signalled: List[Tuple[int, int]] = []

        def is_alive(pid: Any, started: str) -> bool:
            return alive.get(int(pid)) == started

        def kill_pid(pid: int, sig: int) -> None:
            signalled.append((pid, sig))
            if sig == signal.SIGTERM:
                alive.pop(pid, None)

        groups: List[Tuple[int, int]] = []
        stopped = op.stop_recorded(self.root, alive=is_alive, kill=lambda g, s: groups.append((g, s)),
                                   sleep=lambda s: None, kill_pid=kill_pid)
        self.assertEqual(groups, [])  # the leader cannot be verified: its group is not signalled
        self.assertEqual(signalled, [(child, signal.SIGCONT), (child, signal.SIGTERM)])
        self.assertEqual(stopped, [f"repo me/solo (orphaned pid {child})"])
        self.assertEqual(json.loads(state.read_text())["live"], {})

    def test_the_receiver_handler_has_a_read_timeout(self) -> None:
        rec = op.Receiver(SECRET_VALUE)
        rec.start()
        self.addCleanup(rec.close)
        self.assertEqual(rec._server.RequestHandlerClass.timeout, 10)


class ReviewRoundTwoSupervisorTests(PushHarness):
    """Down logging for any cause (N6), rotation accounting and the account-type fallback (N10),
    group members from one `ps` reading, and the quick-exit, kill-guard and stopping cases."""
    ORG, SOLO = "org-org1", "repo-me__solo"

    def test_an_exit_soon_after_a_late_ping_backs_off(self) -> None:
        sup = self.supervisor(owners=[])
        fwd = sup.forwarders[self.SOLO]
        sup.poll()
        self.clock.now += 25  # its ping comes late, just before the 30 s timeout
        self.ping(sup, self.SOLO, 1)
        sup.poll()
        self.assertEqual(fwd.state, "up")
        self.clock.now += 40  # 65 s after starting, but only 40 s after coming up
        fwd.proc.code = 1
        sup.poll()
        self.assertEqual(fwd.next_start_at - self.clock.now, op.BACKOFF_STEPS[0])

    def test_a_target_that_never_comes_up_is_logged_as_down_whatever_the_cause(self) -> None:
        sup = self.supervisor(owners=[])
        fwd = sup.forwarders[self.SOLO]
        start = self.clock.now
        logged: List[Tuple[float, str]] = []
        with mock.patch.object(od, "log_line", lambda line: logged.append((self.clock.now, line))):
            for _ in range(61):  # 20 minutes of "Hook already exists" (someone else's hook)
                sup.poll()
                if fwd.proc is not None and fwd.proc.code is None:
                    self.write_log(sup, self.SOLO, "Error: HTTP 422: Validation Failed\nHook already exists on this repository\n")
                    fwd.proc.code = 1
                self.clock.now += 20
            down = [(at - start, line) for at, line in logged if "is DOWN" in line]
            self.assertEqual([at for at, _ in down], [op.DOWN_AFTER_SECONDS, 2 * op.DOWN_AFTER_SECONDS])
            self.assertIn("Hook already exists", down[0][1])
            self.assertIn("NOTHING is served for repo me/solo", down[0][1])
            # it comes up: the old line no longer holds back the next outage's
            self.clock.now = max(self.clock.now, fwd.next_start_at)
            self.up(sup, self.SOLO, 5)
            self.assertEqual((fwd.state, fwd.down_logged_at, fwd.not_up_since), ("up", None, None))
            # a missing binary: the start itself fails, again and again
            sup.spawn = mock.Mock(side_effect=FileNotFoundError("no such file: gh-webhook"))
            self.clock.now += 600
            fwd.proc.code = 1
            logged.clear()
            went = self.clock.now
            for _ in range(31):
                sup.poll()
                self.clock.now += 20
        down = [(at - went, line) for at, line in logged if "is DOWN" in line]
        self.assertEqual([at for at, _ in down], [op.DOWN_AFTER_SECONDS])
        self.assertIn("cannot start", down[0][1])

    def test_a_rotation_whose_start_fails_is_counted_once(self) -> None:
        sup = self.supervisor(owners=[])
        fwd = sup.forwarders[self.SOLO]
        self.up(sup, self.SOLO, 1)
        self.clock.now = fwd.rotate_at
        sup.poll()
        self.assertEqual((fwd.state, fwd.rotations, sup.stats["rotations"]), ("rotating", 1, 1))
        spawn = sup.spawn
        sup.spawn = mock.Mock(side_effect=OSError("EMFILE"))
        self.clock.now = fwd.next_start_at
        sup.poll()
        self.assertEqual(fwd.state, "backing-off")
        sup.spawn = spawn
        self.clock.now = fwd.next_start_at
        self.up(sup, self.SOLO, 2)
        self.assertEqual((fwd.state, fwd.rotations, sup.stats["rotations"], fwd.restarts), ("up", 1, 1, 1))
        self.assertEqual(sup.summary()["forwarders"][0]["rotations"], sup.stats["rotations"])

    def test_no_rotation_while_stopping(self) -> None:
        sup = self.supervisor(owners=[])
        fwd = sup.forwarders[self.SOLO]
        self.up(sup, self.SOLO, 1)
        sup.stopping = True
        self.clock.now = fwd.rotate_at + 1
        sup.poll()
        self.assertEqual((fwd.state, fwd.rotations, sup.stats["rotations"], len(self.spawned)), ("up", 0, 0, 1))
        self.assertEqual(self.kills, [])

    def test_an_unverified_org_with_no_type_reader_is_refused_not_assumed(self) -> None:
        sup = op.Supervisor(od.parse_config({"owners": ["org1"]}), self.root, [op.Target("org", "org1")],
                            receiver=op.Receiver(SECRET_VALUE, clock=self.clock), token=self.token, api=self.api,
                            spawn=self.spawn, inspect=self.inspect, identify=lambda pid: f"T{pid}", kill=self.kill,
                            clock=self.clock, home="/home/u", base_env={}, binary="/x/gh-webhook",
                            owner_type=None, unverified=["org1"])
        for _ in range(3):
            sup.poll()
            self.clock.now += 200
        fwd = sup.forwarders["org-org1"]
        self.assertEqual((self.spawned, fwd.state, fwd.type_ok), ([], "refused", False))
        self.assertIn("unknown", fwd.last_error)

    def test_group_members_come_from_the_ps_reading_even_before_the_ping(self) -> None:
        sup = self.supervisor(owners=[])
        sup.poll()  # started; no ping yet
        fwd = sup.forwarders[self.SOLO]
        proc = fwd.proc
        known, unreadable = proc.pid + 1000, proc.pid + 1001
        self.report[proc.pgid] = {"alive": True, "stopped": False, "tcp": [], "pids": [proc.pid, known, unreadable],
                                  "started": {proc.pid: f"T{proc.pid}", known: "Mon Nov 23 10:00:02 2026"}}
        self.clock.now += op.LIVENESS_EVERY_SECONDS
        identify = mock.Mock(return_value="NOT USED")
        sup.identify = identify
        sup.poll()
        self.assertEqual(fwd.state, "starting")
        # a pid whose start time the reading lacks is never recorded (so never signalled)
        self.assertEqual(fwd.members, {known: "Mon Nov 23 10:00:02 2026"})
        saved = json.loads(Path(sup.state_path).read_text())["live"][self.SOLO]
        self.assertEqual(saved["members"], {str(known): "Mon Nov 23 10:00:02 2026"})
        identify.assert_not_called()  # no second `ps`, so no pid-reuse window
        self.assertEqual(sup.stats["restarts_forced"], 0)  # no socket while starting is no stall


class KillGuardTests(unittest.TestCase):
    def test_signals_never_reach_ourselves_our_group_or_init(self) -> None:
        with mock.patch.object(op.os, "kill") as kill, mock.patch.object(op.os, "killpg") as killpg:
            for pid in (os.getpid(), 1, 0, -7):
                op._killpid(pid, signal.SIGTERM)
            for pgid in (os.getpgrp(), 1, 0):
                op._killpg(pgid, signal.SIGTERM)
            kill.assert_not_called()
            killpg.assert_not_called()
            op._killpid(424242, signal.SIGTERM)
            op._killpg(424243, signal.SIGKILL)
        kill.assert_called_once_with(424242, signal.SIGTERM)
        killpg.assert_called_once_with(424243, signal.SIGKILL)


# --------------------------------------------------------------------------- dispatcher, push mode


def event(repo: str, action: str = "queued", labels: Tuple[str, ...] = ("self-hosted", "linux-a64"),
          key: str = "org-org1") -> op.Event:
    return op.Event(key, "workflow_job", action=action, repo=repo, labels=labels)


class PushDispatcherHarness(SchedulerHarness):
    """A push-mode Dispatcher over FakeGh and FakePush (no tests of its own)."""
    ORG = op.Target("org", "org1")
    RUNS_A = "repos/org1/a/actions/runs?per_page=100"
    RUNS_B = "repos/org1/b/actions/runs?per_page=100"

    def dispatcher(self, routes: Dict[str, Any], **cfg: Any) -> Tuple[od.Dispatcher, FakePush, FakeGh]:
        disp = self.make_dispatcher(routes, **cfg)
        return disp, disp.push, disp.client

    def two_repos(self, jobs_a: List[Dict[str, Any]], jobs_b: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")], {1: jobs_a})
        routes.update(scan_routes("org1/b", [run_json(2, "org1/b")], {2: jobs_b or []}))
        routes["orgs/org1/repos?type=private"] = [repo_json("org1/a"), repo_json("org1/b")]
        routes[fork_route("org1/b")] = FORK_OFF
        return routes

    def ticks(self, disp: od.Dispatcher, count: int) -> None:
        for _ in range(count):
            self.now += 1
            disp.tick()

    @staticmethod
    def listed(gh: FakeGh, since: int) -> int:
        return sum(1 for path in gh.calls[since:] if path.startswith("orgs/org1/repos?type=private"))

    def scanned(self, gh: FakeGh, since: int) -> List[str]:
        return sorted({path.split("/")[2] for path in gh.calls[since:] if path.startswith("repos/org1/")
                       and "/actions/runs?" in path})

    @staticmethod
    def runs_calls(gh: FakeGh, route: str, since: int = 0) -> int:
        return gh.calls[since:].count(route)


class PushDispatcherTests(PushDispatcherHarness):

    def test_idle_pool_makes_no_github_calls(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([]))
        disp.tick()  # startup discovery
        self.assertEqual(self.listed(gh, 0), 1)
        push.pending.established.append(self.ORG)
        self.ticks(disp, 1)  # the catch-up scan of the forwarder's repositories
        self.assertEqual(self.scanned(gh, 0), ["a", "b"])
        calls = len(gh.calls)
        self.ticks(disp, 1800)  # 30 idle minutes: no event, no child, nothing queued
        self.assertEqual(gh.calls[calls:], [])
        self.assertEqual(self.spawned, [])
        self.assertEqual(push.catchups[0][0], "org-org1")

    def test_a_queued_event_scans_only_that_repository_and_starts_a_child(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([], [job_json(5, ["self-hosted", "linux-a64"])]))
        disp.tick()
        calls = len(gh.calls)
        push.pending.events.append(event("org1/b"))
        self.ticks(disp, 1)
        self.assertEqual(self.scanned(gh, calls), ["b"])
        self.assertEqual([child.repo for child in disp.sched.children.values()], ["org1/b"])
        self.assertEqual(disp.scan_counts["hint"], 1)

    def test_events_that_cannot_need_a_runner_cost_nothing(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([]))
        disp.tick()
        calls = len(gh.calls)
        push.pending.events += [event("org1/a", labels=("ubicloud-standard-2",)), event("org1/a", "completed"),
                                event("org1/a", "in_progress"), event("not a repo")]
        self.ticks(disp, 5)
        self.assertEqual(gh.calls[calls:], [])
        self.assertEqual(disp.events_seen["completed"], 1)

    def test_a_queued_job_of_an_unknown_org_repository_discovers_once_then_scans(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        routes["orgs/org1/repos?type=private"] = [repo_json("org1/a"), repo_json("org1/b"), repo_json("org1/new")]
        routes.update(scan_routes("org1/new", [run_json(3, "org1/new")], {3: [job_json(9, ["linux-a64"])]}))
        routes[fork_route("org1/new")] = FORK_OFF
        self.now += od.DISCOVERY_MIN_GAP_SECONDS
        push.pending.events += [event("org1/new"), event("org1/new")]
        calls = len(gh.calls)
        self.ticks(disp, 1)
        self.assertEqual(self.listed(gh, calls), 1)
        self.assertEqual(self.scanned(gh, calls), ["new"])
        self.assertEqual([c.repo for c in disp.sched.children.values()], ["org1/new"])
        push.pending.events.append(event("org1/ghost"))  # unknown again, but discovery just ran
        calls = len(gh.calls)
        self.ticks(disp, 5)
        self.assertEqual(self.listed(gh, calls), 0)

    def test_a_freed_slot_rescans_only_the_backlog(self) -> None:
        jobs = [job_json(5, ["linux-a64"]), job_json(6, ["linux-a64"])]
        disp, push, gh = self.dispatcher(self.two_repos(jobs), max_jobs=1)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        (child,) = list(disp.sched.children.values())
        self.assertEqual(sorted(disp._backlog), ["org1/a"])
        self.write_state(child, "job-running", vm=f"linux-ephr-{child.proc.pid}-1")
        # the job runs, and no event comes: nothing is read
        calls = len(gh.calls)
        self.ticks(disp, 30)
        self.assertEqual(gh.calls[calls:], [])
        child.proc.code = 0  # done: its slot is free
        self.ticks(disp, 1)
        self.assertEqual(disp.scan_counts["backlog"], 1)
        self.assertEqual(len(disp.sched.children), 1)
        self.assertEqual(len(self.spawned), 2)

    def test_a_backoff_ending_rescans_its_repository(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([job_json(5, ["linux-a64"])]))
        disp.tick()
        disp.sched.backoff_until[("org1/a", "linux-a64")] = self.now + 40
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        self.assertEqual(self.spawned, [])
        self.assertEqual(disp._backlog["org1/a"], self.now + 39)
        self.ticks(disp, 30)
        self.assertEqual(self.spawned, [])
        self.ticks(disp, 10)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(disp.scan_counts["backlog"], 1)

    def test_closed_admission_is_rechecked_locally_then_the_backlog_rescanned(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([job_json(5, ["linux-a64"])]))
        disp.tick()
        disp.sched.admission = lambda: self.open
        self.open = False
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        self.assertTrue(disp.sched.admission_closed)
        calls = len(gh.calls)
        self.ticks(disp, 30)
        self.assertEqual(gh.calls[calls:], [])
        self.open = True
        self.ticks(disp, int(od.ADMISSION_RECHECK_SECONDS))
        self.assertEqual(len(self.spawned), 1)

    def test_an_hour_with_live_children_and_no_events_makes_no_call(self) -> None:
        routes = self.two_repos([job_json(5, ["linux-a64"])])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        (child,) = list(disp.sched.children.values())
        self.write_state(child, "job-running", vm=f"linux-ephr-{child.proc.pid}-1")
        calls = len(gh.calls)
        self.ticks(disp, 3600)  # no event, no catch-up: no guard timer, no scan, no listing
        self.assertEqual(gh.calls[calls:], [])
        self.assertEqual(disp.scan_counts["guard"], 0)
        self.assertEqual(len(disp.sched.children), 1)

    def test_a_fork_runs_event_stops_the_children_of_its_repository(self) -> None:
        for action in ("queued", "in_progress", "waiting"):  # the fork run's own events
            with self.subTest(action=action):
                self.sent.clear()
                self.spawned.clear()
                routes = self.two_repos([job_json(5, ["linux-a64"])])
                disp, push, gh = self.dispatcher(routes)
                disp.tick()
                push.pending.events.append(event("org1/a"))
                self.ticks(disp, 1)
                (child,) = list(disp.sched.children.values())
                self.write_state(child, "job-running", vm=f"linux-ephr-{child.proc.pid}-1")
                # a fork-headed run appears and its job lands on our child
                routes[self.RUNS_A] = {"workflow_runs": [
                    run_json(1, "org1/a", "in_progress"), run_json(2, "org1/a", "in_progress", head="stranger/a")]}
                routes["repos/org1/a/actions/runs/2/jobs?filter=latest&per_page=100"] = {"jobs": [
                    dict(job_json(8, ["linux-a64"], "in_progress"), runner_name=f"linux-ephr-{child.proc.pid}-1")]}
                self.ticks(disp, 600)
                self.assertEqual(self.sent, [])  # nothing is read without an event: the accepted gap
                push.pending.events.append(event("org1/a", action))
                self.ticks(disp, 1)
                self.assertIn((child.proc.pid, signal.SIGTERM), self.sent)
                self.assertNotIn("org1/a", disp.directory.current.served)
                self.assertEqual(disp.scan_counts["guard"], 1)
                self.assertEqual(len(self.spawned), 1)

    def test_a_catch_up_guards_a_repository_with_children(self) -> None:
        routes = self.two_repos([job_json(5, ["linux-a64"])])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        (child,) = list(disp.sched.children.values())
        self.write_state(child, "job-running", vm=f"linux-ephr-{child.proc.pid}-1")
        routes["repos/org1/a/actions/runs?per_page=100"] = {"workflow_runs": [
            run_json(1, "org1/a", "in_progress"), run_json(2, "org1/a", "in_progress", head="stranger/a")]}
        routes["repos/org1/a/actions/runs/2/jobs?filter=latest&per_page=100"] = {"jobs": [
            dict(job_json(8, ["linux-a64"], "in_progress"), runner_name=f"linux-ephr-{child.proc.pid}-1")]}
        self.ticks(disp, 100)
        push.pending.established.append(self.ORG)  # e.g. a rotation, or the forwarder coming back up
        self.ticks(disp, 1)
        self.assertIn((child.proc.pid, signal.SIGTERM), self.sent)

    def test_a_non_lane_event_of_a_repository_with_children_guards_it_but_starts_nothing(self) -> None:
        routes = self.two_repos([job_json(5, ["linux-a64"])])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        self.assertEqual(len(self.spawned), 1)
        # the same run now also queues a job for a hosted runner (no lane of ours)
        routes["repos/org1/a/actions/runs/1/jobs?filter=latest&per_page=100"] = {"jobs": [
            job_json(5, ["linux-a64"]), job_json(6, ["ubuntu-latest"])]}
        for number, (action, labels) in enumerate((("queued", ("ubuntu-latest",)), ("completed", ("gpu",)),
                                                   ("waiting", ())), start=1):
            with self.subTest(action=action):
                calls = len(gh.calls)
                push.pending.events.append(event("org1/a", action, labels=labels))
                self.ticks(disp, 1)
                self.assertEqual(self.scanned(gh, calls), ["a"])  # the guard: run heads read again
                self.assertEqual(disp.scan_counts["guard"], number)
                self.assertEqual(disp.scan_counts["hint"], 1)  # only the first, lane, event
                self.assertEqual(len(self.spawned), 1)  # job 6 needs no runner of ours; job 5 is covered
        calls = len(gh.calls)
        push.pending.events.append(event("org1/b", "completed", labels=("ubuntu-latest",)))  # no children
        self.ticks(disp, 1)
        self.assertEqual(gh.calls[calls:], [])

    def test_a_failed_guard_reading_of_an_unserved_repository_is_retried_on_the_ladder(self) -> None:
        routes = self.two_repos([job_json(5, ["linux-a64"])])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        (child,) = list(disp.sched.children.values())
        self.write_state(child, "job-running", vm=f"linux-ephr-{child.proc.pid}-1")
        disp.directory.drop("org1/a", "archived", security=False)  # unserved: the guard follows its child
        routes[self.RUNS_A] = od.GhError("HTTP 502", status=502)
        calls = len(gh.calls)
        push.pending.events.append(event("org1/a", "in_progress"))
        self.ticks(disp, 1)
        self.assertEqual(self.runs_calls(gh, self.RUNS_A, calls), 1)
        self.assertEqual(disp._guard_retry_at, self.now + od.RETRY_MIN_SECONDS)
        self.ticks(disp, 70)  # 10, 20, 40 s later
        self.assertEqual(self.runs_calls(gh, self.RUNS_A, calls), 4)
        routes[self.RUNS_A] = {"workflow_runs": [run_json(1, "org1/a", "in_progress"),
                                                 run_json(2, "org1/a", head="stranger/a")]}
        routes["repos/org1/a/actions/runs/2/jobs?filter=latest&per_page=100"] = {"jobs": [
            dict(job_json(8, ["linux-a64"], "in_progress"), runner_name=f"linux-ephr-{child.proc.pid}-1")]}
        self.ticks(disp, 80)
        self.assertIn((child.proc.pid, signal.SIGTERM), self.sent)
        child.proc.code = 0
        self.ticks(disp, 1)
        calls = len(gh.calls)
        self.ticks(disp, 3600)
        self.assertEqual(gh.calls[calls:], [])
        self.assertIsNone(disp._guard_retry_at)

    def test_any_event_of_any_label_scans_a_repository_with_children(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([job_json(5, ["linux-a64"])]))
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        calls = len(gh.calls)
        push.pending.events.append(event("org1/a", labels=("ubuntu-latest",)))
        self.ticks(disp, 1)
        self.assertEqual(self.scanned(gh, calls), ["a"])

    def test_a_catch_up_relists_only_when_the_last_listing_is_old(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([]))
        disp.tick()
        self.now += 4000
        calls = len(gh.calls)
        disp.tick()
        self.assertEqual(gh.calls[calls:], [])  # nothing re-lists on a timer
        push.pending.established.append(self.ORG)
        disp.tick()
        self.assertEqual(self.listed(gh, calls), 1)
        self.assertEqual(self.scanned(gh, calls), ["a", "b"])
        self.assertEqual(disp.scan_counts["discovery"], 2)  # startup, then the catch-up
        self.now += od.DISCOVERY_SECONDS - 10
        push.pending.established.append(self.ORG)
        calls = len(gh.calls)
        disp.tick()
        self.assertEqual(self.listed(gh, calls), 0)  # discovered recently enough
        self.assertEqual(self.scanned(gh, calls), ["a", "b"])

    def test_a_rate_limit_delays_hints_without_losing_them(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([job_json(5, ["linux-a64"])]))
        disp.tick()
        disp.rate_block_until = self.now + 30
        push.pending.events.append(event("org1/a"))
        calls = len(gh.calls)
        self.ticks(disp, 10)
        self.assertEqual(gh.calls[calls:], [])
        self.ticks(disp, 25)
        self.assertEqual(len(self.spawned), 1)

    def test_reap_runs_only_where_a_child_ended_badly(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([job_json(5, ["linux-a64"])]))
        started: List[Tuple[List[str], List[str]]] = []

        class Reaper:
            running = False

            def poll(self) -> None:
                pass

            def start(self, served: Iterable[str], busy: Iterable[str]) -> None:
                started.append((sorted(served), sorted(busy)))

        disp.reaper = Reaper()  # type: ignore[assignment]
        disp.tick()
        self.assertEqual(started, [])
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        (child,) = list(disp.sched.children.values())
        child.proc.code = 0
        self.ticks(disp, 1)
        self.assertEqual(started, [])  # a clean exit leaves no registration
        disp._reap_candidates["org1/a"] = self.now
        self.ticks(disp, 1)
        self.assertEqual(started[-1][0], ["org1/a"])
        self.now += od.REAP_CANDIDATE_SECONDS
        count = len(started)
        self.ticks(disp, 1)
        self.assertEqual(len(started), count)

    def test_shutdown_stops_the_forwarders(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([]))
        disp.request_stop(signal.SIGTERM)
        self.assertEqual(disp.run(), 128 + signal.SIGTERM)
        self.assertTrue(push.shut)

    def test_status_shows_each_forwarder(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([]))
        disp.tick()
        now = self.now
        push.summary = lambda: {  # type: ignore[assignment]
            "receiver": "127.0.0.1:41000", "mode": "gh-webhook", "hook_api_requests": 2,
            "receiver_stats": {"accepted": 9, "bad_signature": 1},
            "forwarders": [
                {"target": "org org1", "state": "up", "uptime_s": 3700, "restarts": 2, "rotations": 1,
                 "rotate_in_s": 1500, "pid": 321, "hook_id": 77,
                 "last_error": "", "last_event_at": now - 12, "last_catchup_at": now - 3700, "retry_in_s": None},
                {"target": "repo me/x", "state": "backing-off", "uptime_s": None, "restarts": 5, "pid": None,
                 "hook_id": None, "last_error": f"rc=1: HTTP 422 Hook already exists {FAKE_TOKEN}",
                 "last_event_at": None, "last_catchup_at": None, "retry_in_s": 40}]}
        lines: List[str] = []
        od.lines_for_discovery(disp.snapshot()["discovery"], now, lines.append)
        text = "\n".join(lines)
        self.assertIn("discovery: push via gh-webhook, receiver 127.0.0.1:41000", text)
        self.assertIn("forwarder org org1: up, for 61m, rotates in 25m, restarts=2, rotations=1, pid=321, hook=77, "
                      "last event 12s ago, "
                      "last catch-up 61m ago", text)
        self.assertIn("forwarder repo me/x: backing-off, retry in 40s, restarts=5, last event never, "
                      "last catch-up never", text)
        self.assertIn("last error: rc=1: HTTP 422 Hook already exists gh*_***", text)
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertIn("receiver rejected: bad_signature=1", text)
        self.assertNotIn("poll", text)


class ReviewRoundOneDispatcherTests(PushDispatcherHarness):
    """Lost-work paths (findings 1-3) and the status age of a reading (9)."""

    def test_a_child_that_exits_before_taking_its_job_rescans_its_repository(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([job_json(5, ["linux-a64"])]))
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        (child,) = list(disp.sched.children.values())
        self.assertEqual(disp._backlog, {})  # the plan covered the job
        child.proc.code = 1  # VM clone failed: never assigned
        self.ticks(disp, 1)
        backoff = disp.sched.backoff_until[("org1/a", "linux-a64")]
        self.assertEqual(disp._backlog, {"org1/a": backoff})  # rescanned, but not before its backoff
        self.assertEqual(len(self.spawned), 1)
        self.now = backoff - 1
        self.ticks(disp, 1)
        self.assertEqual(len(self.spawned), 2)  # the job still queued: served again
        self.assertEqual(disp.scan_counts["backlog"], 1)

    def test_a_failing_backlog_rescan_backs_off_and_never_exits_75(self) -> None:
        routes = self.two_repos([job_json(5, ["linux-a64"])])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        disp.sched.backoff_until[("org1/a", "linux-a64")] = self.now + 5
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        self.assertIn("org1/a", disp._backlog)
        routes[self.RUNS_A] = od.GhError("HTTP 502: Bad Gateway", status=502)
        calls = len(gh.calls)
        self.ticks(disp, 60)
        self.assertLessEqual(self.runs_calls(gh, self.RUNS_A, calls), 4)  # was ~57 (every tick)
        self.assertIn("org1/a", disp._backlog)  # kept, not dropped
        self.ticks(disp, 600)
        self.assertIsNone(disp.exit_code)
        self.assertFalse(disp.sched.stopping)
        routes[self.RUNS_A] = {"workflow_runs": [run_json(1, "org1/a")]}
        self.ticks(disp, int(od.RETRY_MAX_SECONDS))
        self.assertEqual(len(self.spawned), 1)  # GitHub recovered: the job is served
        self.assertEqual(disp._retry, {})

    def test_one_repository_failing_on_every_event_never_exits_75(self) -> None:
        routes = self.two_repos([job_json(5, ["linux-a64"])])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        (child,) = list(disp.sched.children.values())
        self.write_state(child, "job-running", vm=f"linux-ephr-{child.proc.pid}-1")
        routes[self.RUNS_A] = od.GhError("HTTP 500", status=500)
        calls = len(gh.calls)
        for _ in range(20):  # its job's events keep coming for 10 minutes: each one asks for the guard
            push.pending.events.append(event("org1/a", "in_progress"))
            self.ticks(disp, 30)
        self.assertGreaterEqual(self.runs_calls(gh, self.RUNS_A, calls), 20)
        self.assertIsNone(disp.exit_code)
        self.assertIsNone(disp.error_since)

    def test_every_served_repository_failing_still_exits_75(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        routes[self.RUNS_A] = od.GhError("network down")
        routes[self.RUNS_B] = od.GhError("network down")
        routes["rate_limit"] = od.GhError("network down")  # the confirmation fails as well
        push.pending.established.append(self.ORG)
        self.ticks(disp, 1)
        self.assertIsNotNone(disp.error_since)
        self.ticks(disp, int(od.SUSTAINED_FAILURE_SECONDS) + int(od.RETRY_MAX_SECONDS))
        self.assertEqual(disp.exit_code, 75)

    def test_a_hint_whose_scan_fails_is_retried_until_github_recovers(self) -> None:
        routes = self.two_repos([], [job_json(5, ["linux-a64"])])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        good = routes[self.RUNS_B]
        routes[self.RUNS_B] = od.GhError("HTTP 502: Bad Gateway", status=502)
        push.pending.events.append(event("org1/b"))
        self.ticks(disp, 1)
        self.assertEqual((self.spawned, sorted(disp._retry)), ([], ["org1/b"]))
        self.assertEqual(disp._retry["org1/b"], self.now + od.RETRY_MIN_SECONDS)
        calls = len(gh.calls)
        self.ticks(disp, int(od.RETRY_MIN_SECONDS))
        self.assertEqual(self.runs_calls(gh, self.RUNS_B, calls), 1)  # one retry, then a longer wait
        self.assertEqual(disp._retry["org1/b"], self.now + 2 * od.RETRY_MIN_SECONDS)
        routes[self.RUNS_B] = good
        self.ticks(disp, 2 * int(od.RETRY_MIN_SECONDS))
        self.assertEqual([c.repo for c in disp.sched.children.values()], ["org1/b"])
        self.assertEqual(disp.scan_counts["retry"], 2)
        self.assertEqual(disp._retry, {})

    def test_a_scan_cycle_that_raises_keeps_its_catch_up_and_hints(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([job_json(5, ["linux-a64"])]))
        disp.tick()
        real = disp.scan_cycle
        disp.scan_cycle = mock.Mock(side_effect=OSError("EMFILE"))  # type: ignore[assignment]
        push.pending.established.append(self.ORG)
        push.pending.events.append(event("org1/a"))
        disp._guard.add("org1/b")  # a guard request (an event of a repository with children) is kept too
        self.ticks(disp, 1)
        self.assertEqual(disp.tick_errors, 1)
        self.assertEqual(list(disp._catchup), [self.ORG.key])
        self.assertEqual(disp._requested, {"org1/a"})
        self.assertEqual(disp._guard, {"org1/b"})
        self.assertEqual(push.catchups, [])
        self.ticks(disp, 3)
        self.assertEqual(disp.scan_cycle.call_count, 1)  # it waits before trying again
        disp.scan_cycle = real  # type: ignore[assignment]
        self.ticks(disp, int(od.RETRY_MIN_SECONDS))
        self.assertEqual([key for key, _ in push.catchups], [self.ORG.key])
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual((disp._catchup, disp._requested, disp._guard), ({}, set(), set()))

    def test_a_freed_slot_survives_a_scan_cycle_that_raises(self) -> None:
        jobs = [job_json(5, ["linux-a64"]), job_json(6, ["linux-a64"])]
        disp, push, gh = self.dispatcher(self.two_repos(jobs), max_jobs=1, rotate_seconds=0)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        (child,) = list(disp.sched.children.values())
        self.write_state(child, "job-running", vm=f"linux-ephr-{child.proc.pid}-1")
        self.ticks(disp, 5)
        real = disp.scan_cycle
        disp.scan_cycle = mock.Mock(side_effect=OSError("EMFILE"))  # type: ignore[assignment]
        child.proc.code = 0  # its slot frees while job 6 waits in the backlog
        self.ticks(disp, 1)
        self.assertEqual(disp.tick_errors, 1)
        disp.scan_cycle = real  # type: ignore[assignment]
        self.ticks(disp, 3600)  # no event, no catch-up, no rotation: only the kept signal can serve it
        self.assertEqual(len(self.spawned), 2)

    def test_a_full_listing_settles_an_earlier_404_so_a_later_one_costs_a_minute_again(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.dispatcher(routes, rotate_seconds=0)
        disp.tick()
        good = routes[self.RUNS_A]
        routes[self.RUNS_A] = od.GhError("HTTP 404 on runs: Not Found", status=404)
        self.now += od.DISCOVERY_SECONDS
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        self.assertIn("org1/a", disp._relist_at)
        routes[self.RUNS_A] = good
        push.pending.established.append(self.ORG)  # a full listing while that re-list is pending
        self.ticks(disp, 1)
        self.assertIn("org1/a", disp.directory.current.served)
        self.assertEqual((disp._relist_at, disp._notfound_relisted), ({}, set()))
        routes[self.RUNS_A] = od.GhError("HTTP 404 on runs: Not Found", status=404)
        push.pending.events.append(event("org1/a"))  # another passing 404 much later
        self.ticks(disp, 1)
        self.assertNotIn("org1/a", disp.directory.current.served)
        routes[self.RUNS_A] = good
        self.ticks(disp, int(od.DISCOVERY_MIN_GAP_SECONDS) + 2)
        self.assertIn("org1/a", disp.directory.current.served)  # was: never, with rotation off

    def test_an_unknown_repository_is_kept_while_its_org_listing_fails(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        listing = "orgs/org1/repos?type=private"
        routes[listing] = od.GhError("HTTP 502", status=502)
        self.now += od.DISCOVERY_MIN_GAP_SECONDS
        push.pending.events.append(event("org1/new"))
        self.ticks(disp, 1)
        self.assertEqual(self.listed(gh, 0), 2)  # startup and this failed one
        self.assertEqual(disp._unknown, {"org1/new"})  # not forgotten
        routes[listing] = [repo_json("org1/a"), repo_json("org1/b"), repo_json("org1/new")]
        routes.update(scan_routes("org1/new", [run_json(3, "org1/new")], {3: [job_json(9, ["linux-a64"])]}))
        routes[fork_route("org1/new")] = FORK_OFF
        self.ticks(disp, int(od.DISCOVERY_MIN_GAP_SECONDS))
        self.assertEqual([c.repo for c in disp.sched.children.values()], ["org1/new"])
        self.assertEqual(disp._unknown, set())

    def test_a_refused_repository_that_becomes_served_is_scanned_at_once(self) -> None:
        routes = self.two_repos([], [job_json(5, ["linux-a64"])])
        routes[fork_route("org1/b")] = {"run_workflows_from_fork_pull_requests": True}
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        self.assertIn("org1/b", disp.directory.current.refused)
        routes[fork_route("org1/b")] = FORK_OFF  # the operator turned fork runs off
        self.now += 3600
        calls = len(gh.calls)
        disp.tick()
        self.assertEqual(gh.calls[calls:], [])  # nothing re-lists on a timer
        push.pending.established.append(self.ORG)  # the next catch-up (a rotation) re-lists
        disp.tick()
        self.assertIn("b", self.scanned(gh, calls))
        self.assertEqual([c.repo for c in disp.sched.children.values()], ["org1/b"])
        self.assertEqual(disp.scan_counts["newly-served"], 1)

    def test_status_shows_how_old_a_queued_reading_is(self) -> None:
        jobs = [job_json(5, ["linux-a64"]), job_json(6, ["linux-a64"])]
        disp, push, gh = self.dispatcher(self.two_repos(jobs), max_jobs=1)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        self.now += 125
        snap = disp.snapshot()
        self.assertEqual(snap["queued"], {"org1/a": 2})
        self.assertEqual(snap["queued_age_s"], {"org1/a": 125})


class ReviewRoundTwoDispatcherTests(PushDispatcherHarness):
    """The guard's own timer (N1, N2), outage evidence (N3), catch-ups and listing retries (N4),
    hinted jobs the job list does not show yet (N9) and pruning of unserved repositories."""

    JOBS_A = "repos/org1/a/actions/runs/1/jobs?filter=latest&per_page=100"
    JOBS_B = "repos/org1/b/actions/runs/2/jobs?filter=latest&per_page=100"

    def test_a_child_of_an_unserved_repository_leaves_no_backlog_and_nothing_is_read_without_an_event(self) -> None:
        routes = self.two_repos([job_json(5, ["linux-a64"])], [job_json(6, ["linux-a64"])])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        push.pending.events += [event("org1/a"), event("org1/b")]
        self.ticks(disp, 1)
        children = {c.repo: c for c in disp.sched.children.values()}
        self.assertEqual(sorted(children), ["org1/a", "org1/b"])
        busy = children["org1/b"]
        self.write_state(busy, "job-running", vm=f"linux-ephr-{busy.proc.pid}-1")
        routes[self.JOBS_B] = {"jobs": [job_json(6, ["linux-a64"], "in_progress")]}
        routes["orgs/org1/repos?type=private"] = [repo_json("org1/a", archived=True), repo_json("org1/b")]
        self.now += 3600
        push.pending.established.append(self.ORG)
        self.ticks(disp, 1)  # the catch-up's listing refuses org1/a (archived: not a security matter)
        self.assertNotIn("org1/a", disp.directory.current.served)
        children["org1/a"].proc.code = od.IDLE_EXIT_CODE  # it idles out, never assigned
        disp.rate_block_until = self.now + 5  # even while nothing is scanned (or pruned), status stays clean
        self.ticks(disp, 1)
        self.assertEqual((disp._backlog, disp.snapshot()["discovery"]["backlog"]), ({}, []))
        self.ticks(disp, 5)
        self.assertEqual(disp._backlog, {})
        backlog, calls = disp.scan_counts["backlog"], len(gh.calls)
        self.ticks(disp, 60)
        self.assertEqual(disp.scan_counts["backlog"], backlog)
        self.assertEqual(gh.calls[calls:], [])  # org1/b's busy child: read again on its next event

    def test_a_stale_backlog_entry_is_pruned_before_it_can_trigger_a_cycle(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        disp._backlog["org1/gone"] = self.now  # e.g. left by a path that predates a drop
        calls, cycles = len(gh.calls), disp.scan_counts["backlog"]
        self.ticks(disp, 30)
        self.assertEqual((disp._backlog, disp.scan_counts["backlog"], gh.calls[calls:]), ({}, cycles, []))

    def test_a_steady_stream_of_hints_never_swallows_a_guard_request(self) -> None:
        routes = self.two_repos([job_json(5, ["linux-a64"])], [])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        (child,) = list(disp.sched.children.values())
        self.write_state(child, "job-running", vm=f"linux-ephr-{child.proc.pid}-1")
        routes[self.JOBS_A] = {"jobs": [job_json(5, ["linux-a64"], "in_progress")]}
        calls = len(gh.calls)
        for number in range(24):  # a busy org: a queued job in org1/b every 5 s, for 2 minutes
            push.pending.events.append(event("org1/b"))
            if number % 4 == 0:  # and now and then an event of org1/a's own job
                push.pending.events.append(event("org1/a", "in_progress"))
            self.ticks(disp, 5)
        self.assertEqual(self.runs_calls(gh, self.RUNS_A, calls), 6)  # once per event of org1/a, no more
        self.assertGreaterEqual(self.runs_calls(gh, self.RUNS_B, calls), 24)

    def three_repos(self) -> Dict[str, Any]:
        routes = self.two_repos([])
        routes.update(scan_routes("org1/c", [run_json(3, "org1/c")], {3: []}))
        routes["orgs/org1/repos?type=private"] = [repo_json("org1/a"), repo_json("org1/b"), repo_json("org1/c")]
        routes[fork_route("org1/c")] = FORK_OFF
        return routes

    def test_two_repositories_failing_with_5xx_never_exit_75_while_github_answers(self) -> None:
        routes = self.three_repos()
        routes["rate_limit"] = {"resources": {"core": {"remaining": 4000}}}
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        push.pending.established.append(self.ORG)
        self.ticks(disp, 1)
        routes[self.RUNS_A] = od.GhError("HTTP 500 on runs", status=500)
        routes[self.RUNS_B] = od.GhError("HTTP 502 on runs", status=502)
        push.pending.events += [event("org1/a"), event("org1/b")]
        self.ticks(disp, 1)
        self.assertEqual(gh.calls.count("rate_limit"), 1)  # suspected, then asked
        self.assertIsNone(disp.error_since)
        self.ticks(disp, 900)
        self.assertIsNone(disp.exit_code)
        self.assertFalse(disp.sched.stopping)
        self.assertEqual(sorted(disp._retry), ["org1/a", "org1/b"])  # still retried, on the ladder
        # the same failures with GitHub really gone do count
        routes["rate_limit"] = od.GhError("gh api failed (rate_limit): no route to host")
        self.ticks(disp, 2 * int(od.RETRY_MAX_SECONDS) + 30)
        self.assertEqual(disp.exit_code, 75)

    def test_a_404_is_no_outage_and_unserves_the_repository_until_the_next_listing(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        good_a, good_b = routes[self.RUNS_A], routes[self.RUNS_B]
        routes[self.RUNS_A] = od.GhError("HTTP 404 on runs: Not Found", status=404)
        routes[self.RUNS_B] = od.GhError("HTTP 404 on runs: Not Found", status=404)
        push.pending.events += [event("org1/a"), event("org1/b")]
        self.ticks(disp, 1)
        self.assertIsNone(disp.error_since)
        self.assertNotIn("rate_limit", gh.calls)  # GitHub answered: nothing to confirm
        self.assertEqual((sorted(disp.directory.current.served), disp._retry), ([], {}))
        self.assertIn("not found", disp.directory.current.refused["org1/a"])
        calls = len(gh.calls)
        push.pending.events += [event("org1/a")]  # refused: a hint for it costs nothing
        self.ticks(disp, 600)
        # (R3-2) listed again once, a minute later; the 404 repeats, so it waits for the next listing
        self.assertEqual(sorted(gh.calls[calls:]), sorted([
            "repos/org1/a", "repos/org1/a/actions/permissions/fork-pr-workflows-private-repos", "repos/org1/a",
            self.RUNS_A, "repos/org1/b", "repos/org1/b/actions/permissions/fork-pr-workflows-private-repos",
            "repos/org1/b", self.RUNS_B]))
        self.assertEqual(sorted(disp.directory.current.served), [])
        self.assertIsNone(disp.exit_code)
        routes[self.RUNS_A], routes[self.RUNS_B] = good_a, good_b
        self.now += 3600
        push.pending.established.append(self.ORG)
        self.ticks(disp, 1)  # the next catch-up lists them again, and scans them
        self.assertEqual(sorted(disp.directory.current.served), ["org1/a", "org1/b"])
        self.assertEqual(self.scanned(gh, calls), ["a", "b"])

    def test_a_catch_up_waits_for_its_failed_listing_which_is_retried_on_the_ladder(self) -> None:
        routes = self.two_repos([job_json(5, ["linux-a64"])])
        listing = "orgs/org1/repos?type=private"
        good = routes[listing]
        routes[listing] = od.GhError("HTTP 502", status=502)
        disp, push, gh = self.dispatcher(routes)
        disp.tick()  # the startup listing fails
        push.pending.established.append(self.ORG)  # the forwarder pings a moment later
        self.ticks(disp, 1)
        self.assertEqual((push.catchups, list(disp._catchup_waiting)), ([], [self.ORG.key]))
        calls = len(gh.calls)
        self.ticks(disp, 600)  # GitHub keeps failing: the listing is retried 10, 20, 40... 300 s apart
        self.assertIn(self.listed(gh, calls), range(4, 8))
        self.assertEqual((push.catchups, self.spawned), ([], []))
        routes[listing] = good  # GitHub recovers
        self.ticks(disp, int(od.RETRY_MAX_SECONDS))
        self.assertEqual([c.repo for c in disp.sched.children.values()], ["org1/a"])
        self.assertEqual([key for key, _ in push.catchups], [self.ORG.key])
        self.assertEqual(disp._catchup_waiting, {})
        calls = len(gh.calls)
        self.ticks(disp, 600)
        self.assertEqual(self.listed(gh, calls), 0)  # a good listing ends the retries

    def test_a_hinted_job_the_scan_does_not_list_yet_is_rescanned_once(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        push.pending.events.append(op.Event("org-org1", "workflow_job", action="queued", repo="org1/a",
                                            labels=("self-hosted", "linux-a64"), job_id=5))
        self.ticks(disp, 1)  # GitHub's job list has not caught up with the event yet
        self.assertEqual(self.spawned, [])
        routes[self.RUNS_A] = {"workflow_runs": [run_json(1, "org1/a", updated_at="2026-01-01T00:00:09Z")]}
        routes[self.JOBS_A] = {"jobs": [job_json(5, ["linux-a64"])]}
        calls = len(gh.calls)
        self.ticks(disp, od.ASSIGN_LAG_SECONDS - 2)
        self.assertEqual(self.scanned(gh, calls), [])
        self.ticks(disp, 3)
        self.assertEqual([c.repo for c in disp.sched.children.values()], ["org1/a"])
        self.assertEqual(disp.scan_counts["recheck"], 1)
        self.assertEqual(disp._hinted, {})
        # a job that never shows up (taken elsewhere, cancelled) costs one rescan, then it is dropped
        push.pending.events.append(op.Event("org-org1", "workflow_job", action="queued", repo="org1/b",
                                            labels=("self-hosted", "linux-a64"), job_id=9))
        self.ticks(disp, 1)
        calls = len(gh.calls)
        self.ticks(disp, 60)
        self.assertEqual(self.runs_calls(gh, self.RUNS_B, calls), 1)
        self.assertEqual((disp._hinted, disp._recheck), ({}, {}))

    def test_a_retry_for_a_repository_no_longer_served_is_dropped(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        routes[self.RUNS_B] = od.GhError("HTTP 502", status=502)
        push.pending.events.append(event("org1/b"))
        self.ticks(disp, 1)
        self.assertIn("org1/b", disp._retry)
        routes[fork_route("org1/b")] = {"run_workflows_from_fork_pull_requests": True}
        self.now += 3600
        push.pending.established.append(self.ORG)
        self.ticks(disp, 1)  # the catch-up's listing refuses it
        self.assertNotIn("org1/b", disp.directory.current.served)
        self.assertEqual((disp._retry, dict(disp._retry_count)), ({}, {}))
        retries, calls = disp.scan_counts["retry"], len(gh.calls)
        self.ticks(disp, 600)
        self.assertEqual((disp.scan_counts["retry"], gh.calls[calls:]), (retries, []))


class ReviewRoundThreeDispatcherTests(PushDispatcherHarness):
    """Failed listings cost little (R3-1), a 404 costs about a minute (R3-2), a rejected login is
    logged loudly (R3-3)."""

    LISTING = "orgs/org1/repos?type=private"
    JOBS_A = "repos/org1/a/actions/runs/1/jobs?filter=latest&per_page=100"

    def up_and_caught_up(self, routes: Dict[str, Any]) -> Tuple[od.Dispatcher, FakePush, FakeGh]:
        disp, push, gh = self.dispatcher(routes, rotate_seconds=3600)
        disp.tick()
        push.pending.established.append(self.ORG)
        self.ticks(disp, 1)
        return disp, push, gh

    def test_a_listing_refused_with_a_4xx_is_retried_only_at_the_next_catch_up(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.up_and_caught_up(routes)
        routes[self.LISTING] = od.GhError("HTTP 403 on orgs/org1/repos: Resource protected by SAML", status=403)
        self.now += 3600
        push.pending.established.append(self.ORG)  # a rotation's catch-up: its full listing is refused
        self.ticks(disp, 1)
        self.assertEqual(list(disp._catchup_waiting), [self.ORG.key])
        calls = len(gh.calls)
        self.ticks(disp, 3600)  # an idle hour with no catch-up
        # was 15 listings, 30 runs reads and ~90 calls: now nothing until the next catch-up
        self.assertEqual(gh.calls[calls:], [])
        self.assertEqual(list(disp._catchup_waiting), [self.ORG.key])  # still waiting for its listing
        push.pending.established.append(self.ORG)  # the next rotation
        self.ticks(disp, 1)
        self.assertEqual(self.listed(gh, calls), 1)
        self.assertEqual(list(disp._catchup_waiting), [self.ORG.key])  # it waits for its listing
        routes[self.LISTING] = [repo_json("org1/a"), repo_json("org1/b")]  # SSO authorised again
        self.ticks(disp, 3600)
        self.assertEqual(self.listed(gh, calls), 1)  # a 4xx is not retried on the ladder
        push.pending.established.append(self.ORG)
        calls = len(gh.calls)
        self.ticks(disp, 1)
        self.assertEqual(self.scanned(gh, calls), ["a", "b"])  # the waiting catch-up runs once
        self.assertEqual(disp._catchup_waiting, {})

    def test_a_transient_failure_relists_only_what_failed_on_the_ladder(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.up_and_caught_up(routes)
        routes[fork_route("org1/b")] = od.GhError("HTTP 502 on fork setting", status=502)
        self.now += 3600
        push.pending.established.append(self.ORG)
        self.ticks(disp, 1)  # the catch-up's listing: org1/b keeps being served, its setting is read again
        self.assertIn("org1/b", disp.directory.current.served)
        calls = len(gh.calls)
        self.ticks(disp, 600)
        self.assertEqual(self.listed(gh, calls), 0)  # no full re-listing of the organization
        fork_b = gh.calls[calls:].count(fork_route("org1/b"))
        self.assertIn(fork_b, range(3, 7))  # 10, 20, 40, 80, 160 s... apart
        self.assertEqual(gh.calls[calls:].count(fork_route("org1/a")), 0)
        self.assertEqual(self.scanned(gh, calls), [])  # no catch-up rerun, nothing new served

    def test_a_run_deleted_mid_scan_is_skipped_not_a_repository_error(self) -> None:
        routes = self.two_repos([job_json(5, ["linux-a64"])])
        routes[self.RUNS_A] = {"workflow_runs": [run_json(1, "org1/a"), run_json(3, "org1/a")]}
        routes["repos/org1/a/actions/runs/3/jobs?filter=latest&per_page=100"] = \
            od.GhError("HTTP 404 on jobs: Not Found", status=404)
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        self.assertIn("org1/a", disp.directory.current.served)
        self.assertEqual((disp._retry, disp.last_scans["org1/a"].error), ({}, None))
        self.assertEqual([c.repo for c in disp.sched.children.values()], ["org1/a"])  # run 1 is served

    def test_a_404_on_the_runs_costs_about_a_minute(self) -> None:
        routes = self.two_repos([job_json(5, ["linux-a64"])])
        good = routes[self.RUNS_A]
        routes[self.RUNS_A] = od.GhError("HTTP 404 on runs: Not Found", status=404)
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        self.assertNotIn("org1/a", disp.directory.current.served)
        routes[self.RUNS_A] = good  # it was a glitch
        calls = len(gh.calls)
        self.ticks(disp, int(od.DISCOVERY_MIN_GAP_SECONDS) + 1)
        self.assertIn("org1/a", disp.directory.current.served)
        self.assertEqual(self.listed(gh, calls), 0)  # only that repository was read again
        self.assertEqual([c.repo for c in disp.sched.children.values()], ["org1/a"])

    def test_a_rejected_login_is_logged_as_down_every_10_minutes_and_never_exits(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.dispatcher(routes)
        disp.tick()
        for route in (self.RUNS_A, self.RUNS_B, "rate_limit"):
            routes[route] = od.GhError("HTTP 401 on x: Bad credentials", status=401)
        logged: List[Tuple[float, str]] = []
        with mock.patch.object(od, "log_line", lambda line: logged.append((self.now, line))):
            start = self.now
            for _ in range(130):  # a hint every 10 s for 21+ minutes
                push.pending.events += [event("org1/a"), event("org1/b")]
                self.ticks(disp, 10)
        down = [at - start for at, line in logged if "rejects the dispatcher's login" in line]
        self.assertEqual(len(down), 3)  # at once, then at most every 10 minutes
        self.assertGreaterEqual(down[1] - down[0], od.AUTH_DOWN_LOG_SECONDS)
        self.assertIsNone(disp.exit_code)  # a restart cannot fix a revoked login: no exit 75 loop
        self.assertFalse(disp.sched.stopping)


class ReCheckDispatcherTests(PushDispatcherHarness):
    """The re-check of round 3: a 404 in a cycle that lists everything still costs about a minute
    (R4-1), partial listings do not starve a transient retry (R4-2), and the R3-1/R3-2 paths the
    round-3 tests left unpinned (R4-3)."""

    LISTING = "orgs/org1/repos?type=private"
    LISTING_2 = "orgs/org2/repos?type=private"
    ORG2 = op.Target("org", "org2")

    def two_orgs(self) -> Tuple[Dict[str, Any], od.Dispatcher, FakePush, FakeGh]:
        routes = self.two_repos([])
        routes.update(scan_routes("org2/c", [run_json(7, "org2/c")], {7: []}))
        routes["users/org2"] = {"type": "Organization"}
        routes[self.LISTING_2] = [repo_json("org2/c")]
        routes[fork_route("org2/c")] = FORK_OFF
        disp, push, gh = self.dispatcher(routes, owners=["org1", "org2"], rotate_seconds=3600)
        push.targets.append(self.ORG2)
        disp.tick()
        push.pending.established += [self.ORG, self.ORG2]
        self.ticks(disp, 1)
        return routes, disp, push, gh

    def test_a_404_in_a_cycle_that_also_lists_everything_still_costs_about_a_minute(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.dispatcher(routes, rotate_seconds=3600)
        disp.tick()
        push.pending.established.append(self.ORG)
        self.ticks(disp, 1)
        good = routes[self.RUNS_A]
        routes[self.RUNS_A] = od.GhError("HTTP 404 on runs: Not Found", status=404)
        self.now += 16 * 60  # a rotation's catch-up comes with a full listing
        push.pending.established.append(self.ORG)
        self.ticks(disp, 1)
        self.assertNotIn("org1/a", disp.directory.current.served)
        routes[self.RUNS_A] = good  # it was a glitch
        self.ticks(disp, int(od.DISCOVERY_MIN_GAP_SECONDS) + 2)
        self.assertIn("org1/a", disp.directory.current.served)  # was: an hour later

    def test_partial_listings_of_something_else_do_not_push_back_a_transient_retry(self) -> None:
        retries = {}
        for noisy in (False, True):
            routes, disp, push, gh = self.two_orgs()
            routes[self.LISTING_2] = od.GhError("HTTP 502 on orgs/org2/repos", status=502)
            self.now += 3600
            push.pending.established.append(self.ORG2)
            self.ticks(disp, 1)  # the catch-up's listing: org2's fails, transiently
            self.assertIn("org2", disp.directory.current.errors)
            calls = len(gh.calls)
            for second in range(1800):
                if noisy and second % 61 == 0:  # a queued job of a repository org1 does not list
                    push.pending.events.append(event(f"org1/pub{second}"))
                self.ticks(disp, 1)
            retries[noisy] = sum(1 for path in gh.calls[calls:] if path.startswith(self.LISTING_2))
        self.assertGreaterEqual(retries[False], 5)
        self.assertEqual(retries[True], retries[False])  # was 2 against 9

    def test_an_unknown_repository_waits_for_the_retry_of_its_owners_failing_listing(self) -> None:
        routes = self.two_repos([])
        disp, push, gh = self.dispatcher(routes, rotate_seconds=3600)
        disp.tick()
        push.pending.established.append(self.ORG)
        self.ticks(disp, 1)
        routes[self.LISTING] = od.GhError("HTTP 502 on orgs/org1/repos", status=502)
        self.now += 3600
        push.pending.established.append(self.ORG)
        self.ticks(disp, 1)  # the catch-up's listing fails: retried on the 10, 20, 40... s ladder
        calls = len(gh.calls)
        for second in range(1200):
            if second % 61 == 0:
                push.pending.events.append(event(f"org1/new{second}"))
            self.ticks(disp, 1)
        # the ladder alone (10+20+40+80+160+300+300+300 s): not one more listing per unknown event
        self.assertLessEqual(self.listed(gh, calls), 8)

    def test_a_partial_listing_keeps_the_other_owners_and_their_transient_flags(self) -> None:
        routes, disp, push, gh = self.two_orgs()
        directory = disp.directory
        routes[self.LISTING_2] = od.GhError("HTTP 502 on orgs/org2/repos", status=502)
        directory.refresh(only={"org2"})
        self.assertEqual(sorted(directory.current.served), ["org1/a", "org1/b", "org2/c"])
        self.assertEqual((set(directory.current.errors), directory.current.transient), ({"org2"}, {"org2"}))
        directory.refresh(only={"org1"})
        self.assertEqual(sorted(directory.current.served), ["org1/a", "org1/b", "org2/c"])
        self.assertEqual((set(directory.current.errors), directory.current.transient), ({"org2"}, {"org2"}))

    def test_a_repository_that_keeps_answering_404_is_relisted_once_per_full_listing(self) -> None:
        routes = self.two_repos([])
        routes[self.RUNS_A] = od.GhError("HTTP 404 on runs: Not Found", status=404)
        disp, push, gh = self.dispatcher(routes, rotate_seconds=0)
        disp.tick()
        for phase in ("after startup", "after a catch-up"):
            with self.subTest(phase=phase):
                calls = len(gh.calls)
                if phase == "after a catch-up":
                    self.now += od.DISCOVERY_SECONDS
                    push.pending.established.append(self.ORG)  # the forwarder came back up: a full listing
                for _ in range(60):  # a hint for it every 30 s for half an hour
                    push.pending.events.append(event("org1/a"))
                    self.ticks(disp, 30)
                relists = gh.calls[calls:].count(fork_route("org1/a"))  # each listing reads its fork setting
                self.assertEqual(relists, 1 if phase == "after startup" else 2)  # (+ the full listing)


class AcceptanceFollowUpTests(PushDispatcherHarness):
    """ACC01: a child that deferred before it touched GitHub starts no reap pass."""

    def test_a_child_that_deferred_before_any_state_starts_no_reap_pass(self) -> None:
        jobs = [job_json(5, ["linux-a64"]), job_json(6, ["linux-a64"])]
        disp, push, gh = self.dispatcher(self.two_repos(jobs), rotate_seconds=0)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        early, late = sorted(disp.sched.children.values(), key=lambda c: c.name)
        early.proc.code = od.DEFERRED_EXIT_CODE  # the pool lock was held: it deferred at once
        self.write_state(late, "minting-jit", vm="linux-ephr-1-1")  # it got as far as minting
        self.ticks(disp, 1)
        late.proc.code = od.DEFERRED_EXIT_CODE
        self.ticks(disp, 1)
        self.assertEqual(list(disp._reap_candidates), ["org1/a"])  # only because of the late one

    def test_a_deferred_child_alone_leaves_the_repository_unvisited(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([job_json(5, ["linux-a64"])]), rotate_seconds=0)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        (child,) = list(disp.sched.children.values())
        child.proc.code = od.DEFERRED_EXIT_CODE
        self.ticks(disp, 1)
        self.assertEqual(disp._reap_candidates, {})
        self.assertIn("org1/a", disp._backlog)  # its job is still served from the backlog


class IntegratedReviewTests(PushDispatcherHarness):
    """ACC04 integrated review: restart reap seeding and the shutdown order."""

    def test_a_restart_reaps_only_for_children_that_did_not_end_cleanly(self) -> None:
        routes = self.two_repos([job_json(5, ["linux-a64"])], [job_json(6, ["linux-a64"])])
        disp, push, gh = self.dispatcher(routes, rotate_seconds=0)
        disp.tick()
        push.pending.events.extend([event("org1/a"), event("org1/b")])
        self.ticks(disp, 1)
        kids = {child.repo: child for child in disp.sched.children.values()}
        self.assertEqual(sorted(kids), ["org1/a", "org1/b"])
        kids["org1/a"].proc.code = 0  # served its job: the runner removed itself
        kids["org1/b"].proc.code = 1  # failed after it may have registered
        gh.routes.update(self.two_repos([], []))  # both jobs were taken: nothing is queued any more
        self.ticks(disp, 1)
        self.assertEqual(disp.sched.children, {})
        seeded = od.recent_child_repos(disp.root, time.time(), od.REAP_CANDIDATE_SECONDS)
        self.assertEqual(sorted(seeded), ["org1/b"])

    def test_an_adopted_child_that_vanishes_is_never_recorded_clean(self) -> None:
        # An adopted process's exit status cannot be read: AdoptedProc.poll reports 0 once it is gone.
        disp, push, gh = self.dispatcher(self.two_repos([job_json(5, ["linux-a64"])]), rotate_seconds=0)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        (child,) = list(disp.sched.children.values())
        child.adopted = True
        child.proc.code = 0
        gh.routes.update(self.two_repos([]))
        logged: List[str] = []
        with mock.patch.object(od, "log_line", logged.append):
            self.ticks(disp, 1)
        self.assertEqual(disp.sched.children, {})
        self.assertTrue([line for line in logged if line.startswith(f"exit {child.name} ")
                         and "rc=unknown (adopted)" in line], logged)
        meta = json.loads(Path(child.state_dir, od.CHILD_META).read_text())
        self.assertEqual((meta["ended"], meta["rc"], meta["clean"]), (True, None, False))
        self.assertIn("org1/a", disp._reap_candidates)
        seeded = od.recent_child_repos(disp.root, time.time(), od.REAP_CANDIDATE_SECONDS)
        self.assertEqual(sorted(seeded), ["org1/a"])

    def test_a_record_without_the_clean_mark_still_seeds_a_reap(self) -> None:
        state = Path(self.root, "seed", "state", od.CHILD_PREFIX + "a1-1-1")
        state.mkdir(parents=True)
        Path(state, od.CHILD_META).write_text(json.dumps({"repo": "org1/a", "ended": True}))  # an older record
        seeded = od.recent_child_repos(str(Path(self.root, "seed")), time.time(), od.REAP_CANDIDATE_SECONDS)
        self.assertEqual(sorted(seeded), ["org1/a"])

    def test_shutdown_signals_the_children_before_it_stops_the_forwarders(self) -> None:
        disp, push, gh = self.dispatcher(self.two_repos([job_json(5, ["linux-a64"])]), rotate_seconds=0)
        disp.tick()
        push.pending.events.append(event("org1/a"))
        self.ticks(disp, 1)
        (child,) = list(disp.sched.children.values())
        sent_before_forwarders: List[List[Tuple[int, Any]]] = []
        real_stop_push = disp.stop_push

        def stop_push() -> None:
            sent_before_forwarders.append(list(self.sent))
            child.proc.code = 143  # it deletes its VM and exits while the forwarders stop
            real_stop_push()

        disp.stop_push = stop_push  # type: ignore[method-assign]
        disp.request_stop(signal.SIGTERM)
        self.assertEqual(disp.run(), 128 + signal.SIGTERM)
        self.assertEqual(sent_before_forwarders[0], [(child.proc.pid, signal.SIGTERM)])
        self.assertEqual(self.sent, [(child.proc.pid, signal.SIGTERM)])  # TERM is never resent
        self.assertTrue(push.shut)

class ReviewRoundThreeSupervisorTests(PushHarness):
    def test_an_owner_of_another_account_type_is_refused_and_not_asked_again(self) -> None:
        asked: List[str] = []

        def owner_type(owner: str) -> Optional[str]:
            asked.append(owner)
            return "Bot"

        logged: List[str] = []
        sup = op.Supervisor(od.parse_config({"owners": ["odd"]}), self.root, [op.Target("org", "odd")],
                            receiver=op.Receiver(SECRET_VALUE, clock=self.clock), token=self.token, api=self.api,
                            spawn=self.spawn, inspect=self.inspect, identify=lambda pid: f"T{pid}", kill=self.kill,
                            clock=self.clock, home="/home/u", base_env={}, binary="/x/gh-webhook",
                            owner_type=owner_type, unverified=["odd"])
        with mock.patch.object(od, "log_line", logged.append):
            for _ in range(10):
                sup.poll()
                self.clock.now += 200
        fwd = sup.forwarders["org-odd"]
        self.assertEqual((asked, self.spawned, fwd.state), (["odd"], [], "refused"))
        self.assertIn("'Bot'", fwd.last_error)
        self.assertTrue([line for line in logged if "is DOWN" in line])


class RunLockTests(unittest.TestCase):
    def test_a_second_run_exits_without_touching_the_running_one(self) -> None:
        import fcntl  # noqa: PLC0415

        stopped: List[str] = []
        err = __import__("io").StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "owners")
            held = od.take_run_lock(root)  # the service's dispatcher
            self.assertIsNotNone(held)
            try:
                with mock.patch.object(op, "stop_recorded", lambda root, log=None: stopped.append(root) or []), \
                        mock.patch.object(od, "load_config", side_effect=AssertionError("must not read the config")), \
                        mock.patch("sys.stderr", err):
                    code = od.main(["--config", "x", "--root", root, "run"])
                self.assertEqual((code, stopped), (od.RUN_LOCKED_EXIT, []))
                self.assertIn(f"pid {os.getpid()}", err.getvalue())
                self.assertIn("already runs", err.getvalue())
                self.assertFalse(os.get_inheritable(held))  # no child or forwarder keeps it
            finally:
                fcntl.flock(held, fcntl.LOCK_UN)
                os.close(held)
            again = od.take_run_lock(root)  # released with its process: the next run takes it
            self.assertIsNotNone(again)
            os.close(again)


class PushStartupTests(unittest.TestCase):
    """`run` in push mode: leftovers first, then a missing login is a configuration error."""

    def run_main(self, token_error: Optional[str], types: Any) -> Tuple[int, List[str], str]:
        order: List[str] = []

        def stop_recorded(root: str, log: Any = None) -> List[str]:
            order.append("stop_recorded")
            return []

        def read_token(config_dir: str) -> str:
            order.append("token")
            if token_error:
                raise op.PushError(token_error)
            return FAKE_TOKEN

        def check_types(client: Any, owners: Any) -> Dict[str, str]:
            order.append("types")
            if isinstance(types, Exception):
                raise types
            return types

        err = __import__("io").StringIO()
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.object(od, "load_config", return_value=od.parse_config({"owners": ["org1"]})), \
                mock.patch.object(od, "GhClient", lambda *a, **kw: FakeGh({})), \
                mock.patch.object(od, "check_gh_auth", lambda *a, **kw: ""), \
                mock.patch.object(op, "stop_recorded", stop_recorded), \
                mock.patch.object(op, "read_forwarder_token", read_token), \
                mock.patch.object(op, "check_owner_types", check_types), \
                mock.patch.object(op, "build", side_effect=AssertionError("must not build")), \
                mock.patch("sys.stderr", err):
            code = od.main(["--config", "x", "--root", os.path.join(root, "owners"), "run"])
        return code, order, err.getvalue()

    def test_a_missing_forwarder_login_exits_2(self) -> None:
        code, order, err = self.run_main("cannot read the forwarder login /x/hosts.yml: No such file", {})
        self.assertEqual(code, 2)
        self.assertEqual(order, ["stop_recorded", "token"])
        self.assertIn("the pool needs the forwarder login", err)
        self.assertIn("/x/hosts.yml", err)
        self.assertNotIn("push =", err)  # there is no mode without forwarders to point to

    def test_leftovers_are_stopped_before_a_personal_owner_exits_2(self) -> None:
        code, order, err = self.run_main(None, od.ConfigError("owners: me is a personal account"))
        self.assertEqual(code, 2)
        self.assertEqual(order, ["stop_recorded", "token", "types"])

    def test_leftovers_are_stopped_before_the_config_is_read(self) -> None:
        cases = {"config error": lambda: od.ConfigError("x.toml: no such file"),
                 "push = false (removed)": lambda: od.parse_config({"owners": ["org1"], "push": False}),
                 "reconcile_seconds (removed)": lambda: od.parse_config({"owners": ["org1"], "reconcile_seconds": 900}),
                 "valid": lambda: od.parse_config({"owners": ["org1"]})}
        for name, make in cases.items():
            with self.subTest(name):
                order: List[str] = []
                try:
                    config: Any = make()
                except od.ConfigError as error:
                    config = error

                def load(path: str, config: Any = config) -> od.Config:
                    order.append("config")
                    if isinstance(config, Exception):
                        raise config
                    return config

                def stop_recorded(root: str, log: Any = None) -> List[str]:
                    order.append("stop_recorded")
                    return []

                def auth(*args: Any, **kw: Any) -> str:
                    order.append("auth")
                    raise RuntimeError("the test ends the run here")

                code: Optional[int] = None
                with tempfile.TemporaryDirectory() as root, mock.patch.object(od, "load_config", load), \
                        mock.patch.object(op, "stop_recorded", stop_recorded), \
                        mock.patch.object(od, "check_gh_auth", auth), \
                        mock.patch("sys.stderr", __import__("io").StringIO()):
                    try:
                        code = od.main(["--config", "x", "--root", os.path.join(root, "owners"), "run"])
                    except RuntimeError:
                        pass
                if isinstance(config, Exception):
                    self.assertEqual((code, order), (2, ["stop_recorded", "config"]))
                else:
                    self.assertEqual((code, order), (None, ["stop_recorded", "config", "auth"]))


class ChildEnvTokenTests(PushHarness):
    def test_the_forwarder_token_never_reaches_a_child(self) -> None:
        sup = self.supervisor()
        sup.poll()
        self.assertTrue(any(env.get("GH_TOKEN") == FAKE_TOKEN for _, env, _, _ in self.spawned))
        children: List[Dict[str, str]] = []

        def spawn_child(argv: List[str], env: Dict[str, str], log_path: str, err_path: Optional[str] = None) -> Any:
            children.append(env)
            return FakeProc()

        base = dict(os.environ, GH_TOKEN="gho_InheritedShellTOKEN", GITHUB_TOKEN="gho_InheritedOtherTOKEN")
        cfg = od.parse_config({"owners": ["org1"]})
        sched = od.Scheduler(cfg, self.root, "/opt/tartci", spawn=spawn_child, admission=lambda: True,
                             base_env=base, home="/home/u", identify=lambda pid: "T")
        sched.start("org1/a", "linux-a64")
        sched.start("org1/a", "macos-a64")
        for env in children:
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertFalse([key for key, value in env.items() if FAKE_TOKEN in value], key_of(env))
        self.assertNotIn(FAKE_TOKEN, json.dumps(os.environ.copy()))
        self.assertNotIn("GH_TOKEN", od.scrub_env({"GH_TOKEN": FAKE_TOKEN, "PATH": "/usr/bin"}))


def key_of(env: Dict[str, str]) -> str:
    return ",".join(sorted(env))


if __name__ == "__main__":
    unittest.main()
