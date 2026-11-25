#!/usr/bin/env python3
"""Tests for scripts/owners_dispatch.py, with fakes for GitHub, tart and processes."""
from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import owners_dispatch as od  # noqa: E402

od.log_line = lambda message: None  # type: ignore[assignment]  # keep test output quiet
REAL_SIGNAL = od._signal
REAL_KILL_GROUP = od._kill_group


class Reply:
    """A canned answer that also carries response headers (rate limit tracking)."""

    def __init__(self, body: Any, headers: Optional[Dict[str, str]] = None) -> None:
        self.body = body
        self.headers = headers or {}


class FakeGh(od.GhClient):
    """Routes `get` to canned bodies; an Exception value is raised. Answers go through
    the real rate tracking (`_note_rate`), with `default_headers` on every response and
    `Reply` for per-route headers. `on_get` observes each request (it may raise)."""

    def __init__(self, routes: Optional[Dict[str, Any]] = None) -> None:
        super().__init__("gh")
        self.routes: Dict[str, Any] = routes or {}
        self.calls: List[str] = []
        self.default_headers: Dict[str, str] = {}
        self.on_get: Optional[Callable[[str], None]] = None

    def get(self, path: str, conditional: bool = False) -> od.Response:
        self.calls.append(path)
        if self.on_get:
            self.on_get(path)
        base = path.split("&page=")[0] if "&page=" in path else path
        listing = base.replace("&per_page=100", "")
        for key in (path, base, listing):
            if key in self.routes:
                value = self.routes[key]
                if isinstance(value, Exception):
                    raise value
                headers = dict(self.default_headers)
                if isinstance(value, Reply):
                    headers.update(value.headers)
                    value = value.body
                with self._lock:
                    self.requests += 1
                    self._note_rate({k.lower(): v for k, v in headers.items()})
                return od.Response(200, headers, value)
        raise od.GhError(f"HTTP 404 on {path}: Not Found", status=404)


def repo_json(full: str, **kw: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {"full_name": full, "private": True, "visibility": "private", "archived": False,
                            "disabled": False, "permissions": {"admin": True}, "owner": {"login": full.split("/")[0]}}
    data.update(kw)
    return data


FORK_OFF = {"run_workflows_from_fork_pull_requests": False}


def fork_route(full: str) -> str:
    return f"repos/{full}/actions/permissions/fork-pr-workflows-private-repos"


def run_json(run_id: int, full: str, status: str = "queued", head: Optional[str] = "same", **kw: Any) -> Dict[str, Any]:
    run = {"id": run_id, "status": status, "updated_at": "2026-01-01T00:00:00Z", "created_at": "2026-01-01T00:00:00Z",
           "run_attempt": 1, "repository": {"full_name": full, "private": True},
           "head_repository": None if head is None else {"full_name": full if head == "same" else head}}
    run.update(kw)
    return run


def job_json(job_id: int, labels: List[str], status: str = "queued", created: str = "2026-01-01T00:00:00Z") -> Dict[str, Any]:
    return {"id": job_id, "name": f"job{job_id}", "status": status, "labels": labels, "created_at": created,
            "html_url": f"https://example.invalid/{job_id}"}


def scan_routes(full: str, runs: List[Dict[str, Any]], jobs: Dict[int, List[Dict[str, Any]]],
                meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    routes: Dict[str, Any] = {f"repos/{full}": meta or repo_json(full),
                              f"repos/{full}/actions/runs?per_page=100": {"workflow_runs": runs}}
    for run_id, items in jobs.items():
        routes[f"repos/{full}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"] = {"jobs": items}
    return routes


class LaneLabelTests(unittest.TestCase):
    def test_label_subset_of_self_hosted_plus_one_lane(self) -> None:
        cases = [
            (["self-hosted", "macos-a64"], "macos-a64"),
            (["macos-a64"], "macos-a64"),
            (["Self-Hosted", "Linux-X64"], "linux-x64"),
            (["self-hosted", "linux-a64"], "linux-a64"),
            (["self-hosted", "linux-x64", "gpu"], None),
            (["self-hosted", "linux-x64", "linux-a64"], None),
            (["self-hosted"], None),
            (["ubuntu-latest"], None),
            (["self-hosted", "macOS", "ARM64"], None),
            ([], None),
        ]
        for labels, expected in cases:
            with self.subTest(labels=labels):
                self.assertEqual(od.lane_for_labels(labels), expected)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_goldens(self) -> None:
        cfg = od.parse_config({"owners": ["alvis"], "golden": {"linux-x64": "custom"}})
        self.assertEqual((cfg.max_jobs, cfg.max_macos, cfg.poll_seconds), (4, 2, 10))
        self.assertEqual(cfg.golden_for("macos-a64"), "owners-macos")
        self.assertEqual(cfg.golden_for("linux-a64"), "owners-linux")
        self.assertEqual(cfg.golden_for("linux-x64"), "custom")

    def test_rejects_bad_input(self) -> None:
        for data in ({}, {"owners": ["a b"]}, {"owners": ["x"], "repos": ["nope"]}, {"owners": ["x"], "max_jobs": 0},
                     {"owners": ["x"], "mystery": 1}, {"owners": ["x"], "golden": {"macos-x64": "g"}},
                     {"owners": ["x"], "max_macos": 3}, {"owners": ["x"], "reconcile_seconds": 0},
                     {"owners": ["x"], "push": "false"}, {"owners": ["x"], "push": 0}, {"owners": ["x"], "push": "yes"}):
            with self.subTest(data=data), self.assertRaises(od.ConfigError):
                od.parse_config(data)


    def test_push_is_parsed_strictly(self) -> None:
        self.assertTrue(od.parse_config({"owners": ["x"]}).push)
        self.assertTrue(od.parse_config({"owners": ["x"], "push": True}).push)
        self.assertFalse(od.parse_config({"owners": ["x"], "push": False}).push)
        cfg = od.parse_config({"owners": ["x"], "reconcile_seconds": 90})
        self.assertEqual(cfg.reconcile_seconds, 90)  # kept and validated for the push layer

    def test_vm_sizes_are_optional_and_validated(self) -> None:
        cfg = od.parse_config({"owners": ["x"]})
        self.assertEqual(cfg.vm_size, {})
        self.assertEqual(cfg.vm_size_view(), {"macos": {"cores": None, "mem_mb": None},
                                              "linux": {"cores": None, "mem_mb": None}})
        cfg = od.parse_config({"owners": ["x"], "macos_vm_cores": 4, "macos_vm_mem_mb": 10240,
                               "linux_vm_mem_mb": 8192})
        self.assertEqual(cfg.vm_size_view(), {"macos": {"cores": 4, "mem_mb": 10240},
                                              "linux": {"cores": None, "mem_mb": 8192}})
        for key, value in (("macos_vm_cores", 0), ("linux_vm_cores", 65), ("macos_vm_cores", "4"),
                           ("linux_vm_cores", True), ("macos_vm_mem_mb", 10), ("linux_vm_mem_mb", 2047),
                           ("macos_vm_mem_mb", 524289), ("linux_vm_mem_mb", 8192.0), ("windows_vm_cores", 2)):
            with self.subTest(key=key, value=value), self.assertRaises(od.ConfigError):
                od.parse_config({"owners": ["x"], key: value})

    def test_an_invalid_vm_size_exits_2_with_the_reason(self) -> None:
        err = io.StringIO()
        with mock.patch.object(od, "load_config",
                               side_effect=lambda path: od.parse_config({"owners": ["x"], "macos_vm_mem_mb": 10})), \
                contextlib.redirect_stderr(err):
            code = od.main(["--config", "x", "dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("macos_vm_mem_mb must be an integer between 2048 and 524288", err.getvalue())

    def test_status_shows_the_running_dispatcher_vm_size(self) -> None:
        view = od.parse_config({"owners": ["x"], "linux_vm_cores": 3}).vm_size_view()
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "state"))
            with open(os.path.join(root, "state", "dispatcher.json"), "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "running": True, "vm_size": view}, handle)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                od.main(["--root", root, "status"])
            self.assertIn("  child VM size: macos cores=host profile mem_mb=derived from cores; "
                          "linux cores=3 mem_mb=derived from cores\n", out.getvalue())
            with open(os.path.join(root, "state", "dispatcher.json"), "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "running": True}, handle)  # written by an older dispatcher
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                od.main(["--root", root, "status"])
            self.assertNotIn("child VM size", out.getvalue())

    def test_vm_size_line_names_configured_and_lane_default_sizes(self) -> None:
        self.assertEqual(od.format_vm_size(od.parse_config({"owners": ["x"]}).vm_size_view()),
                         "child VM size: macos cores=host profile mem_mb=derived from cores; "
                         "linux cores=host profile mem_mb=derived from cores")
        view = od.parse_config({"owners": ["x"], "macos_vm_cores": 4, "macos_vm_mem_mb": 10240,
                                "linux_vm_cores": 3}).vm_size_view()
        self.assertEqual(od.format_vm_size(view),
                         "child VM size: macos cores=4 mem_mb=10240; linux cores=3 mem_mb=derived from cores")


class DirectoryTests(unittest.TestCase):
    def directory(self, routes: Dict[str, Any], **cfg: Any) -> od.Directory:
        base: Dict[str, Any] = {"owners": ["org1", "me"]}
        base.update(cfg)
        routes.setdefault("user", {"login": "me"})
        routes.setdefault("users/org1", {"type": "Organization"})
        routes.setdefault("users/me", {"type": "User"})
        return od.Directory(FakeGh(routes), od.parse_config(base))

    def test_lists_private_repos_of_orgs_and_users_and_extras(self) -> None:
        routes: Dict[str, Any] = {
            "orgs/org1/repos?type=private": [repo_json("org1/a"), repo_json("org1/arch", archived=True)],
            "user/repos?affiliation=owner&visibility=private": [repo_json("me/b"), repo_json("collab/x")],
            "repos/other/extra": repo_json("other/extra"),
        }
        for full in ("org1/a", "me/b", "other/extra"):
            routes[fork_route(full)] = FORK_OFF
        found = self.directory(routes, repos=["other/extra"]).refresh()
        self.assertEqual(sorted(found.served), ["me/b", "org1/a", "other/extra"])
        self.assertEqual(found.refused, {"org1/arch": "archived"})

    def test_public_internal_and_non_admin_never_served(self) -> None:
        routes: Dict[str, Any] = {
            "orgs/org1/repos?type=private": [
                repo_json("org1/public", visibility="public", private=False),
                repo_json("org1/internal", visibility="internal"),
                repo_json("org1/noadmin", permissions={"admin": False, "push": True}),
                repo_json("org1/forkson"),
                repo_json("org1/ok"),
            ],
            "user/repos?affiliation=owner&visibility=private": [],
            fork_route("org1/forkson"): {"run_workflows_from_fork_pull_requests": True},
            fork_route("org1/ok"): FORK_OFF,
        }
        found = self.directory(routes).refresh()
        self.assertEqual(sorted(found.served), ["org1/ok"])
        self.assertIn("visibility=public", found.refused["org1/public"])
        self.assertIn("visibility=internal", found.refused["org1/internal"])
        self.assertIn("admin", found.refused["org1/noadmin"])
        self.assertIn("fork", found.refused["org1/forkson"])

    def test_explicit_public_repo_is_refused_and_exclusions_apply(self) -> None:
        routes: Dict[str, Any] = {
            "orgs/org1/repos?type=private": [repo_json("org1/a"), repo_json("org1/skip")],
            "user/repos?affiliation=owner&visibility=private": [],
            "repos/o/pub": repo_json("o/pub", visibility="public", private=False),
            fork_route("org1/a"): FORK_OFF,
        }
        found = self.directory(routes, repos=["o/pub"], exclude_repos=["org1/skip"]).refresh()
        self.assertEqual(sorted(found.served), ["org1/a"])
        self.assertIn("o/pub", found.refused)
        self.assertNotIn("org1/skip", found.served)

    def test_listing_failure_keeps_previous_repos_and_reports_error(self) -> None:
        routes: Dict[str, Any] = {
            "orgs/org1/repos?type=private": [repo_json("org1/a")],
            "user/repos?affiliation=owner&visibility=private": [],
            fork_route("org1/a"): FORK_OFF,
        }
        directory = self.directory(routes, owners=["org1"])
        self.assertEqual(sorted(directory.refresh().served), ["org1/a"])
        routes["orgs/org1/repos?type=private"] = od.GhError("HTTP 502")
        again = directory.refresh()
        self.assertEqual(sorted(again.served), ["org1/a"])
        self.assertIn("org1", again.errors)

    def test_transient_fork_setting_failure_keeps_a_served_repo_but_404_refuses(self) -> None:
        routes: Dict[str, Any] = {
            "orgs/org1/repos?type=private": [repo_json("org1/a")],
            "user/repos?affiliation=owner&visibility=private": [],
            fork_route("org1/a"): FORK_OFF,
        }
        directory = self.directory(routes, owners=["org1"])
        directory.refresh()
        routes[fork_route("org1/a")] = od.GhError("HTTP 403: rate limit", status=403, rate_limited=True,
                                                  retry_after=120.0)
        again = directory.refresh()
        self.assertEqual(sorted(again.served), ["org1/a"])  # a blip never unserves a repository
        self.assertEqual(again.retry_after, 120.0)
        self.assertEqual(again.rate_limited, {"org1/a"})
        del routes[fork_route("org1/a")]  # 404: the endpoint is not there for this repository
        gone = directory.refresh()
        self.assertEqual(gone.served, {})
        self.assertIn("fork-pull-request setting", gone.refused["org1/a"])

    def test_someone_elses_user_account_is_not_listable(self) -> None:
        routes: Dict[str, Any] = {"users/stranger": {"type": "User"}}
        found = self.directory(routes, owners=["stranger"]).refresh()
        self.assertEqual(found.served, {})
        self.assertIn("stranger", found.errors)


class ScannerTests(unittest.TestCase):
    FULL = "org1/a"

    def scan(self, routes: Dict[str, Any]) -> od.RepoScan:
        return od.Scanner(FakeGh(routes)).scan_repo(self.FULL)

    def test_only_queued_label_matching_jobs_are_served(self) -> None:
        routes = scan_routes(self.FULL, [run_json(1, self.FULL), run_json(2, self.FULL, "in_progress"),
                                         run_json(3, self.FULL, "completed")], {
            1: [job_json(10, ["self-hosted", "macos-a64"]), job_json(11, ["ubuntu-latest"]),
                job_json(12, ["self-hosted", "linux-x64", "gpu"])],
            2: [job_json(20, ["self-hosted", "linux-a64"], "in_progress"), job_json(21, ["linux-x64"])],
        })
        result = self.scan(routes)
        self.assertIsNone(result.error)
        self.assertEqual([(j.job_id, j.lane) for j in result.jobs], [(10, "macos-a64"), (21, "linux-x64")])
        self.assertEqual(sorted(j.job_id for j in result.skipped), [11, 12])

    def test_pending_run_is_scanned(self) -> None:
        routes = scan_routes(self.FULL, [run_json(1, self.FULL, "pending")], {1: [job_json(10, ["linux-a64"])]})
        self.assertEqual([j.job_id for j in self.scan(routes).jobs], [10])

    def test_fork_headed_or_unknown_head_run_refuses_the_repository(self) -> None:
        # GitHub hands any matching queued job to any runner of the repository, so a
        # fork-headed run means the fork setting is on: refuse the repo, never just skip.
        for head in ("fork/a", None):
            with self.subTest(head=head):
                routes = scan_routes(self.FULL, [run_json(1, self.FULL), run_json(2, self.FULL, head=head)],
                                     {1: [job_json(10, ["linux-a64"])], 2: [job_json(11, ["linux-a64"])]})
                result = self.scan(routes)
                self.assertEqual((result.jobs, result.skipped), ([], []))
                self.assertIn("fork", result.dropped or "")
                self.assertIsNone(result.error)

    def test_fork_head_is_checked_for_every_unfinished_run_status(self) -> None:
        for status in ("action_required", "waiting", "requested", "pending", "queued", "in_progress"):
            with self.subTest(status=status):
                routes = scan_routes(self.FULL, [run_json(1, self.FULL), run_json(2, self.FULL, status, head="fork/a")],
                                     {1: [job_json(10, ["linux-a64"])], 2: []})
                result = self.scan(routes)
                self.assertEqual((result.jobs, result.skipped), ([], []))
                self.assertIn("fork", result.dropped or "")
                self.assertTrue(result.dropped_security)
        # A finished run is inert (a re-run flips its status back, and is checked then).
        routes = scan_routes(self.FULL, [run_json(1, self.FULL), run_json(2, self.FULL, "completed", head="fork/a")],
                             {1: [job_json(10, ["linux-a64"])]})
        result = self.scan(routes)
        self.assertIsNone(result.dropped)
        self.assertEqual([j.job_id for j in result.jobs], [10])

    def test_fork_run_jobs_name_the_runners_that_hold_them(self) -> None:
        taken = dict(job_json(11, ["linux-a64"], "in_progress"), runner_name="linux-ephr-5001-1")
        also = dict(job_json(12, ["linux-a64"], "in_progress"), runner_name="tartci-owners-m1-77-1")
        routes = scan_routes(self.FULL, [run_json(1, self.FULL), run_json(2, self.FULL, head="fork/a")],
                             {1: [job_json(10, ["linux-a64"])],
                              2: [taken, also, dict(job_json(13, ["linux-a64"]), runner_name=None)]})
        result = self.scan(routes)
        self.assertEqual(result.fork_runners, ["linux-ephr-5001-1", "tartci-owners-m1-77-1"])
        self.assertFalse(result.fork_jobs_unreadable)
        self.assertEqual(result.jobs, [])
        self.assertIn("run 2", result.dropped or "")

    def test_unreadable_fork_run_jobs_fail_safe(self) -> None:
        for failure in (od.GhError("HTTP 502 on jobs", status=502), od.GhError("HTTP 403 rate limit", status=403,
                                                                               rate_limited=True, retry_after=60.0)):
            with self.subTest(failure=str(failure)):
                routes = scan_routes(self.FULL, [run_json(2, self.FULL, head="fork/a")], {})
                routes[f"repos/{self.FULL}/actions/runs/2/jobs?filter=latest&per_page=100"] = failure
                result = self.scan(routes)
                self.assertTrue(result.fork_jobs_unreadable)
                self.assertTrue(result.dropped_security)
                self.assertIsNone(result.error)  # still a drop, never "0 queued" or a plain scan error
        many = [run_json(n, self.FULL, head="fork/a") for n in range(2, 2 + od.MAX_FORK_RUNS_READ + 1)]
        self.assertTrue(self.scan(scan_routes(self.FULL, many, {})).fork_jobs_unreadable)

    def test_only_a_non_private_repository_is_a_security_drop(self) -> None:
        for meta, security in ((repo_json(self.FULL, archived=True), False), (repo_json(self.FULL, disabled=True), False),
                               (repo_json(self.FULL, visibility="public", private=False), True),
                               (repo_json(self.FULL, visibility="internal"), True)):
            with self.subTest(reason=od.refusal_reason(meta)):
                result = self.scan(scan_routes(self.FULL, [], {}, meta=meta))
                self.assertTrue(result.dropped)
                self.assertEqual(result.dropped_security, security)

    def test_unexpected_exception_in_a_scan_is_an_error_not_a_crash(self) -> None:
        client = FakeGh(scan_routes(self.FULL, [run_json(1, self.FULL)], {}))
        client.on_get = lambda path: (_ for _ in ()).throw(RuntimeError("boom")) if "/runs/" in path else None
        result = od.Scanner(client).scan_repo(self.FULL)
        self.assertTrue((result.error or "").startswith("RuntimeError"))
        self.assertEqual(od.scan_all(od.Scanner(client), [self.FULL, self.FULL])[self.FULL].error, result.error)

    def test_rate_limited_scan_carries_the_wait(self) -> None:
        routes = scan_routes(self.FULL, [], {})
        routes[f"repos/{self.FULL}"] = od.GhError("HTTP 403: secondary rate limit", status=403,
                                                  rate_limited=True, retry_after=600.0)
        result = self.scan(routes)
        self.assertEqual((result.rate_limited, result.retry_after), (True, 600.0))
        self.assertIsNotNone(result.error)

    def test_job_cache_forgets_runs_that_are_no_longer_active(self) -> None:
        routes = scan_routes(self.FULL, [run_json(1, self.FULL, "in_progress")],
                             {1: [job_json(10, ["linux-a64"], "in_progress")]})
        scanner = od.Scanner(FakeGh(routes))
        scanner.scan_repo(self.FULL)
        self.assertEqual(list(scanner._clean_jobs), [(self.FULL, 1)])
        routes[f"repos/{self.FULL}/actions/runs?per_page=100"] = {"workflow_runs": []}
        scanner.scan_repo(self.FULL)
        self.assertEqual(scanner._clean_jobs, {})

    def test_visibility_flip_drops_the_repo_immediately(self) -> None:
        routes = scan_routes(self.FULL, [run_json(1, self.FULL)], {1: [job_json(10, ["linux-a64"])]})
        client = FakeGh(routes)
        scanner = od.Scanner(client)
        self.assertEqual(len(scanner.scan_repo(self.FULL).jobs), 1)
        routes[f"repos/{self.FULL}"] = repo_json(self.FULL, visibility="public", private=False)
        flipped = scanner.scan_repo(self.FULL)
        self.assertEqual(flipped.jobs, [])
        self.assertIn("visibility=public", flipped.dropped or "")
        self.assertIsNone(flipped.error)

    def test_internal_repo_is_dropped_even_though_private_is_true(self) -> None:
        routes = scan_routes(self.FULL, [], {}, meta=repo_json(self.FULL, visibility="internal"))
        self.assertIn("internal", self.scan(routes).dropped or "")

    def test_run_reporting_public_repository_drops(self) -> None:
        run = run_json(1, self.FULL)
        run["repository"]["private"] = False
        routes = scan_routes(self.FULL, [run], {1: [job_json(10, ["linux-a64"])]})
        result = self.scan(routes)
        self.assertEqual(result.jobs, [])
        self.assertTrue(result.dropped)

    def test_scan_failure_is_an_error_never_zero(self) -> None:
        routes = scan_routes(self.FULL, [run_json(1, self.FULL)], {})
        routes[f"repos/{self.FULL}/actions/runs/1/jobs?filter=latest&per_page=100"] = od.GhError("HTTP 502")
        result = self.scan(routes)
        self.assertIn("502", result.error or "")
        self.assertEqual(result.jobs, [])
        routes[f"repos/{self.FULL}/actions/runs?per_page=100"] = od.GhError("timed out")
        self.assertIn("timed out", self.scan(routes).error or "")

    def test_malformed_payload_is_an_error(self) -> None:
        routes = scan_routes(self.FULL, [], {})
        routes[f"repos/{self.FULL}/actions/runs?per_page=100"] = {"workflow_runs": "nope"}
        self.assertIsNotNone(self.scan(routes).error)

    def test_full_page_falls_back_to_status_queries(self) -> None:
        filler = [run_json(100 + n, self.FULL, "completed") for n in range(100)]
        routes = scan_routes(self.FULL, filler, {7: [job_json(70, ["macos-a64"])]})
        routes[f"repos/{self.FULL}/actions/runs?per_page=100&status=queued"] = {"workflow_runs": [run_json(7, self.FULL)]}
        routes[f"repos/{self.FULL}/actions/runs?per_page=100&status=pending"] = {"workflow_runs": []}
        for status in ("in_progress", "action_required", "waiting", "requested"):
            routes[f"repos/{self.FULL}/actions/runs?per_page=100&status={status}"] = {"workflow_runs": []}
        self.assertEqual([j.job_id for j in self.scan(routes).jobs], [70])

    def test_full_page_also_finds_a_buried_fork_run_waiting_for_approval(self) -> None:
        filler = [run_json(100 + n, self.FULL, "completed") for n in range(100)]
        routes = scan_routes(self.FULL, filler, {})
        for status in ("queued", "pending", "in_progress", "waiting", "requested"):
            routes[f"repos/{self.FULL}/actions/runs?per_page=100&status={status}"] = {"workflow_runs": []}
        routes[f"repos/{self.FULL}/actions/runs?per_page=100&status=action_required"] = {
            "workflow_runs": [run_json(8, self.FULL, "action_required", head="stranger/a")]}
        routes[f"repos/{self.FULL}/actions/runs/8/jobs?filter=latest&per_page=100"] = {"jobs": []}
        self.assertTrue(self.scan(routes).dropped_security)


class RateTests(unittest.TestCase):
    def test_interval_widens_then_waits_for_reset(self) -> None:
        self.assertEqual(od.adaptive_interval(10, od.RateState(), 0)[0], 10)
        self.assertEqual(od.adaptive_interval(10, od.RateState(4000, 5000, 100.0), 0)[0], 10)
        widened, note = od.adaptive_interval(10, od.RateState(500, 5000, 100.0), 0)
        self.assertGreater(widened, 10)
        self.assertIn("widened", note)
        waiting, note = od.adaptive_interval(10, od.RateState(40, 5000, 160.0), 100.0)
        self.assertEqual(waiting, 60.0)
        self.assertIn("reset", note)

    def test_parse_gh_i_and_rate_headers(self) -> None:
        text = ("HTTP/2.0 200 OK\r\nX-Ratelimit-Remaining: 4873\r\nX-Ratelimit-Limit: 5000\r\n"
                "X-Ratelimit-Reset: 1795366219\r\nEtag: W/\"abc\"\r\n\r\n{\"ok\": true}")
        status, headers, body = od.parse_gh_i(text)
        self.assertEqual((status, headers["etag"], json.loads(body)), (200, 'W/"abc"', {"ok": True}))

    def make_client(self, outputs: List[Any], clock: float = 1000.0) -> od.GhClient:
        queue = list(outputs)
        seen: List[List[str]] = []

        def run(argv: List[str], **_kw: Any) -> Any:
            seen.append(argv)
            out = queue.pop(0)
            if isinstance(out, BaseException):
                raise out
            return subprocess.CompletedProcess(argv, out[0], out[1], out[2])

        client = od.GhClient("gh", run=run, clock=lambda: clock)
        client.seen = seen  # type: ignore[attr-defined]
        return client

    def test_304_reuses_cached_body_and_sends_if_none_match(self) -> None:
        ok = (0, 'HTTP/2.0 200 OK\nEtag: "e1"\nX-Ratelimit-Remaining: 90\nX-Ratelimit-Limit: 100\n\n{"n": 1}', "")
        nm = (1, "HTTP/2.0 304 Not Modified\nX-Ratelimit-Remaining: 90\nX-Ratelimit-Limit: 100\n\n", "gh: HTTP 304")
        client = self.make_client([ok, nm])
        self.assertEqual(client.get("p", conditional=True).body, {"n": 1})
        again = client.get("p", conditional=True)
        self.assertEqual((again.body, again.from_cache, client.not_modified), ({"n": 1}, True, 1))
        self.assertIn('If-None-Match: "e1"', client.seen[1])  # type: ignore[attr-defined]

    def test_backs_off_before_the_limit_without_calling_gh(self) -> None:
        low = (0, "HTTP/2.0 200 OK\nX-Ratelimit-Remaining: 30\nX-Ratelimit-Limit: 5000\nX-Ratelimit-Reset: 2000\n\n{}", "")
        client = self.make_client([low])
        client.get("a")
        with self.assertRaises(od.GhError) as ctx:
            client.get("b")
        self.assertTrue(ctx.exception.rate_limited)
        self.assertEqual(len(client.seen), 1)  # type: ignore[attr-defined]

    def test_403_with_retry_after_is_honoured_even_with_plenty_of_allowance(self) -> None:
        secondary = (1, 'HTTP/2.0 403 Forbidden\nRetry-After: 600\nX-Ratelimit-Remaining: 4000\n'
                        'X-Ratelimit-Limit: 5000\n\n{"message": "You have exceeded a secondary rate limit."}', "")
        client = self.make_client([secondary])
        with self.assertRaises(od.GhError) as ctx:
            client.get("x")
        self.assertEqual((ctx.exception.status, ctx.exception.rate_limited, ctx.exception.retry_after),
                         (403, True, 600.0))
        # Every later call waits without touching gh until the time is up.
        with self.assertRaises(od.GhError) as again:
            client.get("y")
        self.assertTrue(again.exception.rate_limited)
        self.assertEqual(again.exception.retry_after, 600.0)
        self.assertEqual(len(client.seen), 1)  # type: ignore[attr-defined]

    def test_primary_exhaustion_waits_for_the_reset_header(self) -> None:
        denied = (1, 'HTTP/2.0 403 Forbidden\nX-Ratelimit-Remaining: 0\nX-Ratelimit-Limit: 5000\n'
                     'X-Ratelimit-Reset: 1300\n\n{"message": "API rate limit exceeded"}', "")
        with self.assertRaises(od.GhError) as ctx:
            self.make_client([denied], clock=1000.0).get("x")
        self.assertEqual(ctx.exception.retry_after, 300.0)

    def test_other_403_is_not_a_rate_limit_and_does_not_block(self) -> None:
        forbidden = (1, 'HTTP/2.0 403 Forbidden\n\n{"message": "Resource not accessible"}', "")
        ok = (0, 'HTTP/2.0 200 OK\n\n{"n": 1}', "")
        client = self.make_client([forbidden, ok])
        with self.assertRaises(od.GhError) as ctx:
            client.get("x")
        self.assertEqual((ctx.exception.rate_limited, ctx.exception.retry_after), (False, 0.0))
        self.assertEqual(client.get("y").body, {"n": 1})

    def test_status_zero_reports_gh_stderr_without_a_status(self) -> None:
        client = self.make_client([(4, "", "gh: To get started with GitHub CLI, please run:  gh auth login\n")])
        with self.assertRaises(od.GhError) as ctx:
            client.get("x")
        self.assertIsNone(ctx.exception.status)
        self.assertFalse(ctx.exception.rate_limited)
        self.assertIn("gh auth login", str(ctx.exception))

    def test_http_errors_and_timeouts_raise(self) -> None:
        denied = (1, 'HTTP/2.0 403 Forbidden\nX-Ratelimit-Remaining: 0\n\n{"message": "API rate limit exceeded"}', "")
        with self.assertRaises(od.GhError) as ctx:
            self.make_client([denied]).get("x")
        self.assertTrue(ctx.exception.rate_limited)
        with self.assertRaises(od.GhError):
            self.make_client([subprocess.TimeoutExpired("gh", 1)]).get("x")
        with self.assertRaises(od.GhError):
            self.make_client([(1, "", "no route to host")]).get("x")


class FakeProc:
    _next_pid = 5000

    def __init__(self) -> None:
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.code: Optional[int] = None

    def poll(self) -> Optional[int]:
        return self.code


def fake_started(pid: Any) -> str:
    """The identity (start time) the harness's fake processes have."""
    return f"T{pid}"


class SchedulerHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.now = 1000.0
        self.open = True
        self.spawned: List[Any] = []
        self.spawn_err_paths: List[Optional[str]] = []
        # No test may signal a real pid or group: record instead.
        self.sent: List[Tuple[int, Any]] = []
        self._patches = [mock.patch.object(od, "_signal", lambda proc, sig: self.sent.append((proc.pid, sig))),
                         mock.patch.object(od, "_kill_group",
                                           lambda proc, sig: self.sent.append((proc.pid, ("group", sig))))]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def spawn(self, argv: List[str], env: Dict[str, str], log_path: str, err_path: Optional[str] = None) -> FakeProc:
        proc = FakeProc()
        self.spawned.append((argv, env, log_path, proc))
        self.spawn_err_paths.append(err_path)
        return proc

    def sched(self, base_env: Optional[Dict[str, str]] = None, **cfg: Any) -> od.Scheduler:
        config = od.parse_config(dict({"owners": ["o"]}, **cfg))
        return od.Scheduler(config, self.root, "/opt/tartci", spawn=self.spawn, admission=lambda: self.open,
                            clock=lambda: self.now, sleep=self.fake_sleep, home="/home/u",
                            base_env=base_env or {"PATH": "/usr/bin"}, identify=fake_started)

    def fake_sleep(self, seconds: float) -> None:
        self.now += seconds

    def job(self, job_id: int, lane: str, repo: str = "o/r", created: Optional[str] = None) -> od.QueuedJob:
        return od.QueuedJob(repo, 1, job_id, f"j{job_id}", ("self-hosted", lane), lane,
                            created or f"2026-01-01T00:00:{job_id:02d}Z")

    def make_dispatcher(self, routes: Dict[str, Any], tart: Optional[Callable[[List[str]], Any]] = None,
                        alive: Optional[Callable[..., bool]] = None, **cfg: Any) -> od.Dispatcher:
        config = od.parse_config(dict({"owners": ["org1"]}, **cfg))
        client = FakeGh(routes)
        sched = od.Scheduler(config, self.root, "/opt/tartci", spawn=self.spawn, admission=lambda: True,
                             clock=lambda: self.now, sleep=self.fake_sleep, home="/home/u", base_env={},
                             identify=fake_started)
        routes.setdefault("users/org1", {"type": "Organization"})
        routes.setdefault("user", {"login": "me"})
        routes.setdefault("orgs/org1/repos?type=private", [repo_json("org1/a")])
        routes.setdefault(fork_route("org1/a"), FORK_OFF)
        return od.Dispatcher(config, self.root, "/opt/tartci", client, sched, od.Scanner(client),
                             od.Directory(client, config), clock=lambda: self.now, sleep=self.fake_sleep,
                             tart=tart or (lambda argv: "[]"), alive=alive or (lambda pid, started="": False),
                             born=lambda vm: None)

    def launch(self, sched: od.Scheduler, jobs: List[od.QueuedJob]) -> List[od.Child]:
        return [sched.start(repo, lane) for repo, lane in sched.plan(jobs, self.now)]

    def write_state(self, child: od.Child, phase: str, vm: str = "vm-1") -> None:
        os.makedirs(child.state_dir, exist_ok=True)
        with open(os.path.join(child.state_dir, f"{child.name}.state.json"), "w") as handle:
            json.dump({"phase": phase, "vm": vm, "supervisor_pid": str(child.proc.pid)}, handle)


class CapTests(SchedulerHarness):
    def test_max_jobs_and_two_macos(self) -> None:
        sched = self.sched()
        jobs = [self.job(n, "macos-a64") for n in range(1, 5)] + [self.job(10 + n, "linux-a64") for n in range(1, 4)]
        started = self.launch(sched, jobs)
        lanes = [child.lane.label for child in started]
        self.assertEqual(len(started), 4)
        self.assertEqual(lanes.count("macos-a64"), 2)
        self.assertEqual(lanes.count("linux-a64"), 2)  # a macOS cap never blocks Linux behind it

    def test_max_jobs_config_is_honoured(self) -> None:
        sched = self.sched(max_jobs=1)
        self.assertEqual(len(self.launch(sched, [self.job(1, "linux-a64"), self.job(2, "linux-x64")])), 1)

    def test_macos_cap_env_lowers_the_limit_but_never_raises_it(self) -> None:
        self.assertEqual(self.sched({"TARTCI_MACOS_VM_CAP": "1"}).macos_cap, 1)
        self.assertEqual(self.sched({"TARTCI_MACOS_VM_CAP": "9"}).macos_cap, 2)

    def test_admission_closed_starts_nothing_but_open_resumes(self) -> None:
        sched = self.sched()
        self.open = False
        self.assertEqual(sched.plan([self.job(1, "linux-a64")], self.now), [])
        self.open = True
        self.assertEqual(len(sched.plan([self.job(1, "linux-a64")], self.now)), 1)

    def test_stopping_starts_nothing(self) -> None:
        sched = self.sched()
        sched.stopping = True
        self.assertEqual(sched.plan([self.job(1, "linux-a64")], self.now), [])


class CountingTests(SchedulerHarness):
    def test_d3_children_not_yet_assigned_cover_queued_jobs(self) -> None:
        sched = self.sched()
        jobs = [self.job(1, "linux-a64"), self.job(2, "linux-a64")]
        first = self.launch(sched, jobs)
        self.assertEqual(len(first), 2)
        self.assertEqual(self.launch(sched, jobs), [])  # 2 queued, 2 unassigned children: nothing more
        self.assertEqual(self.launch(sched, jobs + [self.job(3, "linux-a64")])[0].repo, "o/r")  # a third job

    def test_assigned_child_stops_covering_after_the_lag(self) -> None:
        sched = self.sched()
        first = self.launch(sched, [self.job(1, "linux-a64")])[0]
        self.write_state(first, "job-running")
        sched.observe()
        self.assertIsNotNone(first.assigned_at)
        # The job list still shows the job as queued right after assignment: it stays covered.
        self.assertEqual(sched.plan([self.job(1, "linux-a64")], self.now), [])
        # A scan started well after the assignment that still lists one queued job is a new job.
        self.now += od.ASSIGN_LAG_SECONDS + 5
        self.assertEqual(len(sched.plan([self.job(2, "linux-a64")], self.now)), 1)

    def test_pairs_are_counted_per_repo_and_lane(self) -> None:
        sched = self.sched()
        self.launch(sched, [self.job(1, "linux-a64", "o/one")])
        started = self.launch(sched, [self.job(1, "linux-a64", "o/one"), self.job(2, "linux-a64", "o/two"),
                                      self.job(3, "linux-x64", "o/one")])
        self.assertEqual(sorted((c.repo, c.lane.label) for c in started),
                         [("o/one", "linux-x64"), ("o/two", "linux-a64")])

    def test_fifo_by_job_creation_time(self) -> None:
        sched = self.sched(max_jobs=2)
        jobs = [self.job(1, "linux-a64", "o/late", "2026-01-01T00:00:09Z"),
                self.job(2, "linux-x64", "o/early", "2026-01-01T00:00:01Z"),
                self.job(3, "macos-a64", "o/mid", "2026-01-01T00:00:05Z")]
        started = self.launch(sched, jobs)
        self.assertEqual([c.repo for c in started], ["o/early", "o/mid"])


class RefusalTests(SchedulerHarness):
    def test_refused_child_frees_its_slot_and_backs_off_with_growth(self) -> None:
        sched = self.sched(max_jobs=1)
        jobs = [self.job(1, "macos-a64")]
        child = self.launch(sched, jobs)[0]
        self.assertEqual(len(sched.children), 1)
        child.proc.code = 1  # exited before any VM state: tartci refused it
        finished = sched.observe()
        self.assertEqual([(c.name, code) for c, code in finished], [(child.name, 1)])
        self.assertEqual(sched.children, {})  # the slot is free again
        self.assertEqual(sched.plan(jobs, self.now), [])  # but this pair is backing off
        self.now += od.denial_delay(1) + 1
        again = self.launch(sched, jobs)[0]
        again.proc.code = 75
        sched.observe()
        self.assertGreater(sched.backoff_until[("o/r", "macos-a64")] - self.now, od.denial_delay(1))
        # Another pair is unaffected.
        self.assertEqual(len(sched.plan([self.job(2, "linux-a64", "o/other")], self.now)), 1)

    def test_a_served_job_resets_the_backoff(self) -> None:
        sched = self.sched()
        sched.refusals[("o/r", "linux-a64")] = 3
        sched.backoff_until[("o/r", "linux-a64")] = self.now + 100
        child = sched.start("o/r", "linux-a64")
        self.write_state(child, "job-running")
        sched.observe()
        child.proc.code = 0
        sched.observe()
        self.assertNotIn(("o/r", "linux-a64"), sched.backoff_until)

    def test_idle_timeout_is_a_gentle_fixed_pause_not_a_denial(self) -> None:
        sched = self.sched()
        pair = ("o/r", "linux-a64")
        for _ in range(3):
            child = sched.start("o/r", "linux-a64")
            self.write_state(child, "idle-wait")  # it booted: admission, lease and JIT mint passed
            child.proc.code = od.IDLE_EXIT_CODE
            sched.observe()
            self.assertEqual(sched.backoff_until[pair] - self.now, od.IDLE_BACKOFF_SECONDS)  # never doubles
            self.assertNotIn(pair, sched.refusals)
            self.now += od.IDLE_BACKOFF_SECONDS + 1
        self.assertEqual(len(sched.plan([self.job(1, "linux-a64")], self.now)), 1)

    def test_idle_timeout_clears_an_earlier_denial_streak(self) -> None:
        sched = self.sched()
        refused = sched.start("o/r", "linux-a64")
        refused.proc.code = 1
        sched.observe()
        self.assertEqual(sched.refusals[("o/r", "linux-a64")], 1)
        idle = sched.start("o/r", "linux-a64")
        self.write_state(idle, "idle-wait")
        idle.proc.code = od.IDLE_EXIT_CODE
        sched.observe()
        self.assertNotIn(("o/r", "linux-a64"), sched.refusals)

    def test_pre_boot_refusal_escalates_but_a_post_boot_failure_does_too(self) -> None:
        sched = self.sched()
        pair = ("o/r", "macos-a64")
        delays = []
        for code, booted in ((1, False), (75, False), (1, True)):
            child = sched.start("o/r", "macos-a64")
            if booted:
                self.write_state(child, "preparing")
            child.proc.code = code
            sched.observe()
            delays.append(sched.backoff_until[pair] - self.now)
            self.now += 400
        self.assertEqual(delays, [od.denial_delay(1), od.denial_delay(2), od.denial_delay(3)])

    def test_a_child_we_terminated_is_not_a_denial(self) -> None:
        sched = self.sched()
        child = sched.start("o/r", "linux-a64")
        self.assertEqual(sched.stop_unassigned("o/r"), [child.name])
        child.proc.code = 143
        sched.observe()
        self.assertEqual((dict(sched.refusals), sched.backoff_until), ({}, {}))

    def test_spawn_failure_backs_the_pair_off_and_leaves_no_state_dir(self) -> None:
        sched = self.sched()

        def boom(argv: List[str], env: Dict[str, str], log_path: str) -> Any:
            raise OSError(errno.EMFILE, "Too many open files")

        sched.spawn = boom
        with self.assertRaises(OSError):
            sched.start("o/r", "linux-a64")
        self.assertEqual(sched.children, {})
        self.assertEqual(os.listdir(os.path.join(self.root, "state")), [])
        sched.note_start_failure("o/r", "linux-a64")
        self.assertEqual(sched.plan([self.job(1, "linux-a64")], self.now), [])

    def test_a_child_spawn_failure_is_recorded_as_child_until_a_child_starts(self) -> None:
        sched = self.sched()
        real = sched.spawn

        def boom(argv: List[str], env: Dict[str, str], log_path: str) -> Any:
            raise OSError(errno.ENOENT, "No such file or directory")

        with mock.patch.dict(od.LAST_SPAWN_ERROR, clear=True):
            sched.spawn = boom
            with self.assertRaises(OSError):
                sched.start("o/r", "linux-a64")
            self.assertIn("No such file", od.LAST_SPAWN_ERROR["child"]["error"])
            sched.spawn = real
            sched.start("o/r", "linux-a64")
            self.assertEqual(od.LAST_SPAWN_ERROR, {})

    def test_vm_state_marks_the_child_as_booted(self) -> None:
        sched = self.sched()
        child = sched.start("o/r", "linux-a64")
        self.assertFalse(child.vm_seen)
        self.write_state(child, "booting")
        sched.observe()
        self.assertTrue(child.vm_seen)
        self.assertIsNone(child.assigned_at)


class ChildEnvTests(SchedulerHarness):
    def test_macos_child_command_and_environment(self) -> None:
        sched = self.sched()
        child = sched.start("o/r", "macos-a64")
        argv, env, log_path, _proc = self.spawned[0]
        self.assertEqual(argv, ["/opt/tartci/tartci", "serve", "macos", "--once", "--repo", "o/r",
                                "--labels", "self-hosted,macos-a64", "--golden", "owners-macos"])
        self.assertEqual(env["TART_HOME"], "/home/u/.tart")
        self.assertEqual(env["TARTCI_VM_SSH_KEY"], "/home/u/.config/tartci/owners-vm-ed25519")
        self.assertTrue(env["TARTCI_RUNNER_NAME"].startswith("tartci-"))
        self.assertEqual(env["TARTCI_RUNNER_NAME"], child.name)
        self.assertEqual(env["TARTCI_QUEUE_LANE_ID"], child.name)
        self.assertEqual(env["TARTCI_RUNNER_IDLE_TIMEOUT_SECS"], "120")
        for key in ("TARTCI_MACOS_VM_CORES", "TARTCI_MACOS_VM_MEM_MB", "TARTCI_LINUX_VM_CORES"):
            self.assertNotIn(key, env)  # unset in owners.toml: the lane sizes the VM
        for key in ("TARTCI_STATE_DIR", "TARTCI_MACOS_STATE_DIR", "TARTCI_CI_CACHE", "TARTCI_MACOS_LOGS"):
            self.assertTrue(env[key].startswith(self.root), key)
        self.assertTrue(log_path.startswith(self.root))

    def test_linux_children_sizes_and_rosetta(self) -> None:
        sched = self.sched()
        arm = sched.start("o/r", "linux-a64")
        x64 = sched.start("o/r", "linux-x64")
        env_arm, env_x64 = self.spawned[0][1], self.spawned[1][1]
        for env in (env_arm, env_x64):
            for key in ("TARTCI_LINUX_VM_CORES", "TARTCI_LINUX_VM_MEM_MB", "TARTCI_MACOS_VM_CORES"):
                self.assertNotIn(key, env)
        self.assertEqual(env_arm["TARTCI_LINUX_ROSETTA"], "0")
        self.assertEqual(env_x64["TARTCI_LINUX_ROSETTA"], "1")
        self.assertEqual(self.spawned[1][0][-1], "owners-linux")
        self.assertNotEqual(arm.name, x64.name)

    def test_configured_vm_sizes_reach_only_their_own_os(self) -> None:
        sched = self.sched(macos_vm_cores=4, macos_vm_mem_mb=10240, linux_vm_cores=3, linux_vm_mem_mb=8192)
        sched.start("o/r", "macos-a64")
        sched.start("o/r", "linux-x64")
        macos_env, linux_env = self.spawned[0][1], self.spawned[1][1]
        self.assertEqual((macos_env["TARTCI_MACOS_VM_CORES"], macos_env["TARTCI_MACOS_VM_MEM_MB"]), ("4", "10240"))
        self.assertEqual((linux_env["TARTCI_LINUX_VM_CORES"], linux_env["TARTCI_LINUX_VM_MEM_MB"]), ("3", "8192"))
        self.assertFalse({"TARTCI_LINUX_VM_CORES", "TARTCI_LINUX_VM_MEM_MB"} & set(macos_env))
        self.assertFalse({"TARTCI_MACOS_VM_CORES", "TARTCI_MACOS_VM_MEM_MB"} & set(linux_env))

    def test_every_child_charges_its_lease_a_max_jobs_share(self) -> None:
        # The configured size is a ceiling: the lease charges 1/max_jobs of the host budget,
        # so max_jobs alone caps concurrency, sized or not, on either OS.
        sched = self.sched(max_jobs=6, macos_vm_cores=4)
        sched.start("o/r", "macos-a64")
        sched.start("o/r", "linux-a64")
        self.assertEqual([spawned[1]["TARTCI_VM_LEASE_SHARE_SLOTS"] for spawned in self.spawned], ["6", "6"])

    def test_an_inherited_share_does_not_reach_a_child(self) -> None:
        sched = self.sched({"PATH": "/usr/bin", "TARTCI_VM_LEASE_SHARE_SLOTS": "1"}, max_jobs=3)
        sched.start("o/r", "linux-x64")
        self.assertEqual(self.spawned[0][1]["TARTCI_VM_LEASE_SHARE_SLOTS"], "3")

    def test_one_configured_axis_leaves_the_other_to_the_lane(self) -> None:
        # Memory alone: cores come from the host profile; cores alone: memory is derived from them.
        sched = self.sched(macos_vm_mem_mb=12288, linux_vm_cores=2)
        sched.start("o/r", "macos-a64")
        sched.start("o/r", "linux-a64")
        macos_env, linux_env = self.spawned[0][1], self.spawned[1][1]
        self.assertEqual(macos_env["TARTCI_MACOS_VM_MEM_MB"], "12288")
        self.assertNotIn("TARTCI_MACOS_VM_CORES", macos_env)
        self.assertEqual(linux_env["TARTCI_LINUX_VM_CORES"], "2")
        self.assertNotIn("TARTCI_LINUX_VM_MEM_MB", linux_env)

    def test_an_inherited_size_override_does_not_reach_a_child(self) -> None:
        # Only owners.toml sizes a child: the operator's shell overrides are scrubbed like any TARTCI_*.
        inherited = {"PATH": "/usr/bin", "TARTCI_MACOS_VM_CORES": "8", "TARTCI_MACOS_VM_MEM_MB": "16384",
                     "TARTCI_LINUX_VM_CORES": "8", "TARTCI_LINUX_VM_MEM_MB": "16384"}
        sched = self.sched(inherited)
        sched.start("o/r", "macos-a64")
        sched.start("o/r", "linux-a64")
        for env in (self.spawned[0][1], self.spawned[1][1]):
            self.assertFalse((set(inherited) - {"PATH"}) & set(env))

    def test_identities_are_unique_across_concurrent_children(self) -> None:
        sched = self.sched(max_jobs=4)
        kids = [sched.start("o/r", "macos-a64") for _ in range(2)] + [sched.start("o/r", "linux-a64") for _ in range(2)]
        self.assertEqual(len({k.name for k in kids}), 4)
        self.assertEqual(len({k.slot for k in kids}), 4)
        self.assertEqual(len({k.state_dir for k in kids}), 4)
        envs = [s[1] for s in self.spawned]
        self.assertEqual(len({e["TARTCI_QUEUE_LANE_ID"] for e in envs}), 4)

    def test_freed_slot_is_reused(self) -> None:
        sched = self.sched()
        first = sched.start("o/r", "linux-a64")
        sched.start("o/r", "linux-a64")
        first.proc.code = 0
        sched.observe()
        self.assertEqual(sched.start("o/r", "linux-a64").slot, first.slot)

    def test_no_token_or_jit_in_child_environment_or_argv(self) -> None:
        # The dispatcher never mints a JIT config (only a child's runner.sh does, and it
        # keeps it off every command line), and the children authenticate through the
        # `gh` login: no token variable is forwarded, whatever the operator exported.
        tokens = {"GH_TOKEN": "ghp_SECRET_TOKEN_1", "GITHUB_TOKEN": "ghs_SECRET_TOKEN_2",
                  "GH_ENTERPRISE_TOKEN": "ghe_SECRET_TOKEN_3", "GITHUB_ENTERPRISE_TOKEN": "ghe_SECRET_TOKEN_4"}
        sched = self.sched(dict({"PATH": "/usr/bin"}, **tokens))
        sched.start("o/r", "linux-a64")
        argv, child_env = self.spawned[0][0], self.spawned[0][1]
        self.assertEqual(child_env["TARTCI_GH_CLI"], "gh")
        for name in tokens:
            self.assertNotIn(name, child_env)
        blob = "\n".join(list(argv) + [f"{k}={v}" for k, v in child_env.items()])
        for value in tokens.values():
            self.assertNotIn(value, blob)
        self.assertFalse([k for k in child_env if "JIT" in k.upper()])
        self.assertFalse([a for a in argv if "jit" in a.lower() or "eyJ" in a])

    def test_behaviour_changing_variables_do_not_leak_into_children(self) -> None:
        leaky = {"TARTCI_RUNNER_WORKFLOW_TIERS": "9", "TARTCI_RUNNER_GROUP_ID": "7", "TARTCI_RUNNER_REPO": "x/y",
                 "TARTCI_RUNNER_LABELS": "evil", "TARTCI_QUEUE_LANE_ID": "stolen", "TARTCI_LINUX_GOLDEN": "evil",
                 "TARTCI_MACOS_GOLDEN": "evil", "PULP_LANE": "p", "TARTCI_ADMISSION_CLEAN_MODE": "off",
                 "TARTCI_STATE_DIR": "/elsewhere", "TARTCI_CI_CACHE": "/elsewhere", "TARTCI_VM_SSH_KEY": "/other-key"}
        keep = {"PATH": "/usr/bin", "HOME": "/home/u", "GH_CONFIG_DIR": "/home/u/.config/gh",
                "TARTCI_MACOS_VM_CAP": "1"}
        sched = self.sched(dict(keep, **leaky))
        child = sched.start("o/r", "linux-x64")
        env = self.spawned[0][1]
        for name in ("TARTCI_RUNNER_WORKFLOW_TIERS", "TARTCI_RUNNER_GROUP_ID", "TARTCI_RUNNER_REPO",
                     "TARTCI_RUNNER_LABELS", "TARTCI_LINUX_GOLDEN", "TARTCI_MACOS_GOLDEN", "PULP_LANE",
                     "TARTCI_ADMISSION_CLEAN_MODE"):
            self.assertNotIn(name, env)
        self.assertEqual(env["TARTCI_QUEUE_LANE_ID"], child.name)  # ours, not the operator's
        self.assertEqual(env["TARTCI_STATE_DIR"], child.state_dir)
        self.assertEqual(env["TARTCI_CI_CACHE"], os.path.join(self.root, "cache"))
        self.assertEqual(env["TARTCI_VM_SSH_KEY"], "/home/u/.config/tartci/owners-vm-ed25519")
        for name, value in keep.items():
            self.assertEqual(env[name], value)
        self.assertEqual(sched.macos_cap, 1)  # the host-wide cap is still honoured by the dispatcher

    def test_the_child_environment_is_an_allowlist(self) -> None:
        # Everything the lanes read that is not on the list stays out, however it is named:
        # credentials, host-path overrides, lease and yield knobs, ssh agents.
        stray = {"LANG": "en_GB.UTF-8", "LC_ALL": "en_GB.UTF-8", "LC_TIME": "en_GB.UTF-8", "TZ": "Asia/Tokyo",
                 "TARTCI_JIT_GH_CLI": "/bin/other-gh", "TARTCI_SCAN_GH_CLI": "x", "TARTCI_SCAN_APP_ID": "1",
                 "TARTCI_HOST_CI_CACHE": "/host/cache", "TARTCI_PIP_WHEELHOUSE_DIR": "/host/wheels",
                 "TARTCI_ARTIFACT_CACHE_DIR": "/host/artifacts", "TARTCI_ACTIVE_VM_LEASE_ID": "lease-9",
                 "TARTCI_VM_USER": "root", "TARTCI_GUEST_HTTP_PROXY": "http://proxy", "TARTCI_YIELD_TO_X": "1",
                 "TARTCI_ASSIGNMENT_V2_MODE": "1", "TARTCI_ROOT": "/elsewhere", "TARTCI_RUNNER_CHROME_APP_DIR": "/Apps",
                 "SSH_AUTH_SOCK": "/tmp/agent.sock", "SSH_AGENT_PID": "4", "GH_TOKEN": "ghp_x", "GITHUB_TOKEN": "ghs_x",
                 "GH_ENTERPRISE_TOKEN": "ghe_x", "AWS_SECRET_ACCESS_KEY": "s", "HTTPS_PROXY": "http://proxy",
                 "PULP_SHARED_FETCHCONTENT_SOURCE_DIR": "/Users/example/Library/Caches/Pulp/fetchcontent-src"}
        allowed = {"PATH": "/usr/bin", "HOME": "/home/u", "USER": "u", "LOGNAME": "u", "SHELL": "/bin/zsh",
                   "TMPDIR": "/tmp/", "TERM": "dumb",
                   "GH_CONFIG_DIR": "/home/u/.config/gh", "XDG_CONFIG_HOME": "/home/u/.config",
                   "TARTCI_MACOS_VM_CAP": "1"}
        for lane in ("linux-a64", "macos-a64"):
            with self.subTest(lane=lane):
                self.spawned.clear()
                sched = self.sched(dict(allowed, **stray))
                sched.start("o/r", lane)
                env = self.spawned[0][1]
                for name in stray:
                    if name == "PULP_SHARED_FETCHCONTENT_SOURCE_DIR" and lane == "macos-a64":
                        continue  # set by the dispatcher itself, checked below
                    self.assertNotIn(name, env, name)
                for name, value in allowed.items():
                    self.assertEqual(env[name], value, name)
        self.assertEqual(od.scrub_env({"PATH": "/p", "LC_X": "1", "LANG": "x", "TZ": "UTC", "FOO": "2",
                                       "TARTCI_RUNNER_REPO": "x"}), {"PATH": "/p"})

    def test_macos_fetchcontent_dir_is_pool_owned_and_private(self) -> None:
        # Scrubbing PULP_* alone would make the macOS lane fall back to
        # $HOME/Library/Caches/Pulp/fetchcontent-src and mount that host dir into guests.
        sched = self.sched({"PATH": "/usr/bin", "PULP_SHARED_FETCHCONTENT_SOURCE_DIR": "/Users/example/host-cache"})
        sched.start("o/r", "macos-a64")
        sched.start("o/r", "linux-a64")
        macos_env, linux_env = self.spawned[0][1], self.spawned[1][1]
        directory = macos_env["PULP_SHARED_FETCHCONTENT_SOURCE_DIR"]
        self.assertEqual(directory, os.path.join(self.root, "fetchcontent-src"))
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        self.assertNotIn("PULP_SHARED_FETCHCONTENT_SOURCE_DIR", linux_env)  # only the macOS lane reads it
        for key, value in macos_env.items():  # and no other variable points the lane outside the pool root
            if key.endswith(("_DIR", "_CACHE", "_LOGS", "_ROOT")) and key != "TART_HOME":
                self.assertTrue(value.startswith(self.root), f"{key}={value}")

    def test_spawn_records_identity_with_a_private_token(self) -> None:
        sched = self.sched()
        child = sched.start("o/r", "macos-a64")
        path = os.path.join(child.state_dir, od.CHILD_META)
        meta = json.loads(Path(path).read_text())
        self.assertEqual((meta["name"], meta["lane"], meta["repo"], meta["slot"], meta["pid"]),
                         (child.name, "macos-a64", "o/r", child.slot, child.proc.pid))
        self.assertEqual(meta["pid_started"], fake_started(child.proc.pid))  # the identity adoption will check
        self.assertNotIn("ended", meta)
        self.assertEqual(len(meta["token"]), 24)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(child.state_dir).st_mode), 0o700)
        self.assertNotEqual(sched.start("o/r", "macos-a64").name, child.name)


class ReapTests(unittest.TestCase):
    SPAWNED = 5_000.0

    def make_child(self, root: str, name: str, pid: str, vm: str, token: Optional[str] = "tok-" + "0" * 20,
                   mtime: float = 5_100.0) -> Path:
        directory = Path(root, "state", name)
        directory.mkdir(parents=True)
        if token is not None:
            (directory / od.CHILD_META).write_text(json.dumps({"name": name, "token": token, "spawned_at": self.SPAWNED}))
        state = directory / f"{vm}.state.json"
        state.write_text(json.dumps({"vm": vm, "supervisor_pid": pid, "phase": "job-running"}))
        os.utime(state, (mtime, mtime))
        return directory

    def tart_fake(self, calls: List[List[str]], vms: List[str]) -> Callable[[List[str]], str]:
        def tart(argv: List[str]) -> str:
            calls.append(argv)
            if argv[0] == "list":
                return json.dumps([{"Name": n} for n in ["owners-linux", "owners-macos"] + vms])
            return ""
        return tart

    def test_reaps_only_this_pools_dead_children_and_removes_their_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            dead = self.make_child(root, "tartci-owners-a1", "111", "linux-ephr-111-1")
            live = self.make_child(root, "tartci-owners-a2", "222", "linux-ephr-222-1")
            calls: List[List[str]] = []
            vms = ["linux-ephr-111-1", "linux-ephr-222-1", "tartci-owners-m123-333-1", "tartci-owners-m123-444-1"]
            removed = od.reap_leftovers(root, self.tart_fake(calls, vms),
                                        alive=lambda pid, started="": str(pid) in ("222", "444"),
                                        born=lambda vm: self.SPAWNED + 30.0)
            self.assertEqual(sorted(removed), ["linux-ephr-111-1", "tartci-owners-m123-333-1"])
            deleted = [c[1] for c in calls if c[0] == "delete"]
            self.assertEqual(sorted(deleted), ["linux-ephr-111-1", "tartci-owners-m123-333-1"])
            self.assertNotIn("owners-linux", deleted)  # goldens are never touched
            self.assertFalse(dead.exists(), "a reaped child's state is removed so no start re-checks it")
            self.assertTrue(live.exists())

    def test_a_recycled_pid_never_reaps_a_native_lanes_vm(self) -> None:
        # The old supervisor is dead, but a native tartci Linux lane now owns that pid and
        # created linux-ephr-<pid>-1 long after the pool child's last state write.
        with tempfile.TemporaryDirectory() as root:
            child = self.make_child(root, "tartci-owners-a1", "111", "linux-ephr-111-1")
            calls: List[List[str]] = []
            removed = od.reap_leftovers(root, self.tart_fake(calls, ["linux-ephr-111-1"]),
                                        alive=lambda pid, started="": False,
                                        born=lambda vm: self.SPAWNED + 86_400.0)
            self.assertEqual(removed, [])
            self.assertEqual([c for c in calls if c[0] in ("stop", "delete")], [])
            self.assertTrue(child.exists())

    def test_no_identity_record_or_unknown_birth_means_no_reap(self) -> None:
        for token, born in ((None, self.SPAWNED + 30.0), ("tok", None), ("tok", self.SPAWNED - 3_600.0)):
            with self.subTest(token=token, born=born), tempfile.TemporaryDirectory() as root:
                self.make_child(root, "tartci-owners-a1", "111", "linux-ephr-111-1", token=token)
                calls: List[List[str]] = []
                removed = od.reap_leftovers(root, self.tart_fake(calls, ["linux-ephr-111-1"]),
                                            alive=lambda pid, started="": False, born=lambda vm: born)
                self.assertEqual((removed, [c for c in calls if c[0] == "delete"]), ([], []))

    def test_vm_name_must_embed_the_recorded_supervisor_pid(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self.make_child(root, "tartci-owners-a1", "111", "linux-ephr-999-1")
            removed = od.reap_leftovers(root, self.tart_fake([], ["linux-ephr-999-1"]),
                                        alive=lambda pid, started="": False, born=lambda vm: self.SPAWNED + 30.0)
            self.assertEqual(removed, [])

    def test_a_stale_state_file_whose_vm_is_gone_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            gone = self.make_child(root, "tartci-owners-a1", "111", "linux-ephr-111-1")
            od.reap_leftovers(root, self.tart_fake([], []), alive=lambda pid, started="": False,
                              born=lambda vm: None)
            self.assertFalse(gone.exists())

    def test_a_live_child_with_a_verified_identity_keeps_its_dir_even_without_a_state_file(self) -> None:
        # A live child that startup did not adopt (and runner.sh's trap-deleted state file)
        # must not lose its identity record to the startup reap.
        with tempfile.TemporaryDirectory() as root:
            live = Path(root, "state", "tartci-owners-a1")
            live.mkdir(parents=True)
            (live / od.CHILD_META).write_text(json.dumps(
                {"name": "tartci-owners-a1", "token": "tok", "pid": 321, "pid_started": "S321"}))
            wrong = Path(root, "state", "tartci-owners-a2")  # the pid was recycled
            wrong.mkdir(parents=True)
            (wrong / od.CHILD_META).write_text(json.dumps(
                {"name": "tartci-owners-a2", "token": "tok", "pid": 321, "pid_started": "old"}))
            ended = Path(root, "state", "tartci-owners-a3")
            ended.mkdir(parents=True)
            (ended / od.CHILD_META).write_text(json.dumps(
                {"name": "tartci-owners-a3", "token": "tok", "ended": True, "pid": 321, "pid_started": "S321"}))
            od.reap_leftovers(root, self.tart_fake([], []),
                              alive=lambda pid, started="": str(pid) == "321" and started == "S321", born=lambda vm: None)
            self.assertTrue(live.exists())
            self.assertFalse(wrong.exists())
            self.assertFalse(ended.exists())

    def test_protected_names_are_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            kept = self.make_child(root, "tartci-owners-a1", "111", "linux-ephr-111-1")
            od.reap_leftovers(root, self.tart_fake([], []), alive=lambda pid, started="": False,
                              born=lambda vm: None, protect=["tartci-owners-a1"])
            self.assertTrue(kept.exists())

    def test_tart_runs_with_the_home_tart_dir_not_an_inherited_one(self) -> None:
        seen: List[Dict[str, str]] = []

        def fake_run(argv: List[str], **kw: Any) -> Any:
            seen.append(kw["env"])
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        with mock.patch.dict(os.environ, {"TART_HOME": "/somewhere/else"}), mock.patch.object(od.subprocess, "run", fake_run):
            od.tart_runner(["list"])
        self.assertEqual(seen[0]["TART_HOME"], os.path.join(os.path.expanduser("~"), ".tart"))


class PruneTests(unittest.TestCase):
    def test_retention_by_age_and_count_spares_live_children(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            now = 1_000_000.0
            ages = [8 * 86400, 3600, 7200, 10_800, 14_400, 9 * 86400]  # a0 is live
            Path(root, "logs", "children").mkdir(parents=True)
            for index, age in enumerate(ages):
                name = f"tartci-owners-a{index}"
                Path(root, "state", name).mkdir(parents=True)
                Path(root, "logs", "children", f"{name}.log").write_text("x")
                for path in (Path(root, "state", name), Path(root, "logs", "children", f"{name}.log")):
                    os.utime(path, (now - age, now - age))
            old_vm_logs = Path(root, "logs", "linux", "vm-old")
            old_vm_logs.mkdir(parents=True)
            os.utime(old_vm_logs, (now - 9 * 86400, now - 9 * 86400))

            def state() -> List[str]:
                return sorted(p.name for p in Path(root, "state").iterdir())

            od.prune_children(root, live=["tartci-owners-a0"], now=now)  # age only
            self.assertEqual(state(), [f"tartci-owners-a{n}" for n in range(5)])
            self.assertFalse(old_vm_logs.exists())
            od.prune_children(root, live=["tartci-owners-a0"], now=now, keep=3)  # then the count
            self.assertEqual(state(), [f"tartci-owners-a{n}" for n in range(4)])
            self.assertEqual(sorted(p.name for p in Path(root, "logs", "children").iterdir()),
                             [f"tartci-owners-a{n}.log" for n in range(4)])


class FilePermissionTests(unittest.TestCase):
    def test_private_dir_files_and_child_logs_are_never_group_or_world_readable(self) -> None:
        old = os.umask(0)  # even under a permissive umask
        try:
            with tempfile.TemporaryDirectory() as root:
                owned = os.path.join(root, "pool")
                os.mkdir(owned, 0o755)
                od.ensure_private_dir(owned)
                self.assertEqual(stat.S_IMODE(os.stat(owned).st_mode), 0o700)
                snap = os.path.join(owned, "x.json")
                od.write_private_json(snap, {"a": 1})
                self.assertEqual(stat.S_IMODE(os.stat(snap).st_mode), 0o600)
                log = os.path.join(owned, "child.log")
                proc = od.spawn_process(["/bin/echo", "hi"], {"PATH": "/usr/bin:/bin"}, log)
                proc.wait(timeout=10)
                self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)
                self.assertEqual(Path(log).read_text().strip(), "hi")
        finally:
            os.umask(old)


class AdoptionTests(SchedulerHarness):
    def make_orphan(self, name: str, pid: int, repo: str = "o/r", phase: str = "idle-wait", lane: str = "linux-a64",
                    slot: int = 3, meta: bool = True) -> str:
        directory = os.path.join(self.root, "state", name)
        os.makedirs(directory)
        if meta:
            Path(directory, od.CHILD_META).write_text(json.dumps(
                {"name": name, "lane": lane, "repo": repo, "slot": slot, "token": "t" * 24, "spawned_at": 900.0,
                 "pid": pid, "pid_started": fake_started(pid), "log": f"/logs/{name}.log"}))
        Path(directory, f"{name}.state.json").write_text(json.dumps(
            {"vm": "vm-1", "phase": phase, "supervisor_pid": str(pid), "repo": repo,
             "supervisor_pid_started_at": fake_started(pid)}))
        return directory

    def test_live_children_are_adopted_and_count_against_every_cap(self) -> None:
        sched = self.sched(max_jobs=2)
        self.make_orphan("tartci-owners-a111111", 4242)
        self.make_orphan("tartci-owners-a111112", 4243, phase="job-running", slot=4)
        self.make_orphan("tartci-owners-a111113", 4244)  # dead
        adopted = sched.adopt(alive=lambda pid, started="": str(pid) in ("4242", "4243"))
        self.assertEqual(sorted(c.name for c in adopted), ["tartci-owners-a111111", "tartci-owners-a111112"])
        waiting, running = sched.children["tartci-owners-a111111"], sched.children["tartci-owners-a111112"]
        self.assertTrue(waiting.adopted)
        self.assertIsNone(waiting.assigned_at)
        self.assertIsNotNone(running.assigned_at)
        self.assertEqual((waiting.repo, waiting.lane.label, waiting.slot, running.slot), ("o/r", "linux-a64", 3, 4))
        self.assertEqual(waiting.log_path, "/logs/tartci-owners-a111111.log")
        # max_jobs=2 is already full: nothing more starts, however much is queued.
        self.assertEqual(sched.plan([self.job(n, "linux-a64", f"o/other{n}") for n in range(1, 4)], self.now), [])

    def test_adopted_unassigned_child_covers_a_queued_job_and_slots_are_not_reused(self) -> None:
        sched = self.sched()
        self.make_orphan("tartci-owners-a111111", 4242, slot=1)
        sched.adopt(alive=lambda pid, started="": True)
        self.assertEqual(sched.plan([self.job(1, "linux-a64")], self.now), [])  # D3: already covered
        self.assertEqual(len(sched.plan([self.job(1, "linux-a64"), self.job(2, "linux-a64")], self.now)), 1)
        self.assertNotEqual(sched.start("o/r", "linux-a64").slot, 1)

    def test_adopted_macos_child_counts_against_the_macos_cap(self) -> None:
        sched = self.sched()
        self.make_orphan("tartci-owners-m111111", 4242, lane="macos-a64")
        self.make_orphan("tartci-owners-m111112", 4243, lane="macos-a64", slot=2)
        sched.adopt(alive=lambda pid, started="": True)
        self.assertEqual(sched.plan([self.job(1, "macos-a64", "o/new")], self.now), [])
        self.assertEqual(len(sched.plan([self.job(1, "linux-a64", "o/new")], self.now)), 1)

    def test_without_an_identity_record_lane_and_repo_come_from_the_name_and_state(self) -> None:
        sched = self.sched()
        self.make_orphan("tartci-owners-x111111", 4242, lane="", meta=False)
        sched.adopt(alive=lambda pid, started="": True)
        child = sched.children["tartci-owners-x111111"]
        self.assertEqual((child.lane.label, child.repo), ("linux-x64", "o/r"))
        self.assertGreater(child.slot, 90)

    def test_an_adopted_child_that_exits_frees_its_slot_without_a_denial(self) -> None:
        sched = self.sched()
        directory = self.make_orphan("tartci-owners-a111111", 4242)
        sched.adopt(alive=lambda pid, started="": True)
        child = sched.children["tartci-owners-a111111"]
        child.proc = mock.Mock(pid=4242, poll=lambda: 0)
        sched.observe()
        self.assertEqual((sched.children, dict(sched.refusals), sched.backoff_until), ({}, {}, {}))
        self.assertTrue(os.path.isdir(directory))  # retention, not exit, removes the dir

    def test_adopted_proc_reports_liveness_by_identity_not_by_pid(self) -> None:
        # The REAL identity check: this process, with its true start time, is "alive";
        # the same pid with another start time, or none recorded, is not.
        started = od.process_started_at(os.getpid())
        self.assertTrue(started)
        self.assertIsNone(od.AdoptedProc(os.getpid(), os.getpgrp(), started or "").poll())
        self.assertEqual(od.AdoptedProc(os.getpid(), os.getpgrp(), "Thu Jan  1 00:00:00 1970").poll(), 0)
        self.assertEqual(od.AdoptedProc(os.getpid(), os.getpgrp()).poll(), 0)  # nothing recorded: unverified
        done = subprocess.Popen(["/usr/bin/true"])
        done.wait()
        self.assertEqual(od.AdoptedProc(done.pid, done.pid, started or "x").poll(), 0)

    def test_eperm_means_dead(self) -> None:
        # A pid we may not signal is not our child, whatever its start time.
        with mock.patch.object(od.os, "kill", side_effect=PermissionError(errno.EPERM, "denied")):
            self.assertFalse(od.pid_alive(1234, "x"))
            self.assertFalse(od.same_process(1234, "x"))
            self.assertEqual(od.AdoptedProc(1234, 1234, "x").poll(), 0)

    def test_pid_alive_lenient_for_the_reaper_strict_for_adoption(self) -> None:
        me = os.getpid()
        self.assertTrue(od.pid_alive(me))  # no start time to compare: alive is enough for the reaper
        self.assertFalse(od.pid_alive(me, strict=True))  # ...never for adoption or a signal
        with mock.patch.object(od, "process_started_at", return_value=None):  # ps could not answer
            self.assertTrue(od.pid_alive(me, "x"))
            self.assertFalse(od.pid_alive(me, "x", strict=True))
        for bad in (0, 1, -5, "", None, "abc"):
            self.assertFalse(od.pid_alive(bad), bad)

    # -- N1: a recycled pid is never adopted, counted or signalled

    def test_real_identity_adopts_a_matching_process_and_refuses_a_wrong_start_time(self) -> None:
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        real = od.process_started_at(sleeper.pid)
        self.assertTrue(real)
        # A dir with only the identity record: runner.sh deleted its state file on exit.
        recycled = os.path.join(self.root, "state", "tartci-owners-a111111")
        genuine = os.path.join(self.root, "state", "tartci-owners-a111112")
        for directory, started in ((recycled, "Thu Jan  1 00:00:00 1970"), (genuine, real)):
            os.makedirs(directory)
            Path(directory, od.CHILD_META).write_text(json.dumps(
                {"name": os.path.basename(directory), "lane": "linux-a64", "repo": "o/r", "slot": 1,
                 "token": "t" * 24, "spawned_at": 900.0, "pid": sleeper.pid, "pid_started": started}))
        sched = self.sched(max_jobs=4)
        adopted = sched.adopt()  # the REAL same_process / process_started_at
        self.assertEqual([c.name for c in adopted], ["tartci-owners-a111112"])
        self.assertIsNone(adopted[0].proc.poll())
        self.assertEqual(len(sched.plan([self.job(n, "linux-a64") for n in (1, 2, 3, 4)], self.now)), 3)  # it holds 1 of 4

    def test_a_record_without_a_start_time_is_not_adopted(self) -> None:
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        directory = os.path.join(self.root, "state", "tartci-owners-a111111")
        os.makedirs(directory)
        Path(directory, od.CHILD_META).write_text(json.dumps(
            {"name": "tartci-owners-a111111", "lane": "linux-a64", "repo": "o/r", "slot": 1, "token": "t" * 24,
             "pid": sleeper.pid}))
        self.assertEqual(self.sched().adopt(), [])

    def test_an_ended_child_record_is_never_adopted(self) -> None:
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        sched = self.sched()
        child = sched.start("o/r", "linux-a64")
        meta_path = os.path.join(child.state_dir, od.CHILD_META)
        meta = json.loads(Path(meta_path).read_text())
        meta.update(pid=sleeper.pid, pid_started=od.process_started_at(sleeper.pid))  # a live, matching process
        Path(meta_path).write_text(json.dumps(meta))
        child.proc.code = 0
        sched.observe()  # the child exits: its record is retired
        ended = json.loads(Path(meta_path).read_text())
        self.assertTrue(ended["ended"])
        self.assertNotIn("pid", ended)
        self.assertNotIn("pid_started", ended)
        self.assertEqual(ended["token"], meta["token"])  # the reaper's identity survives
        # Even if the pid were put back, an ended record is not adopted.
        ended.update(pid=sleeper.pid, pid_started=od.process_started_at(sleeper.pid))
        Path(meta_path).write_text(json.dumps(ended))
        self.assertEqual(self.sched().adopt(), [])

    def test_the_runners_own_state_file_identity_is_enough_and_must_match(self) -> None:
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        real = od.process_started_at(sleeper.pid)
        for started, expected in ((real, 1), ("Thu Jan  1 00:00:00 1970", 0)):
            with self.subTest(started=started):
                directory = os.path.join(self.root, "state", "tartci-owners-a111111")
                shutil.rmtree(directory, ignore_errors=True)
                os.makedirs(directory)
                Path(directory, "vm-1.state.json").write_text(json.dumps(
                    {"vm": "vm-1", "phase": "idle-wait", "supervisor_pid": str(sleeper.pid), "repo": "o/r",
                     "supervisor_pid_started_at": started}))
                self.assertEqual(len(self.sched().adopt()), expected)

    def test_an_adopted_process_is_never_signalled_unless_its_identity_still_holds(self) -> None:
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        real = od.process_started_at(sleeper.pid)
        stranger = od.AdoptedProc(sleeper.pid, sleeper.pid, "Thu Jan  1 00:00:00 1970")  # the pid was recycled
        owned = od.AdoptedProc(sleeper.pid, sleeper.pid, real or "")
        # The harness replaced _signal/_kill_group with recorders: use the real ones.
        REAL_SIGNAL(stranger, signal.SIGTERM)
        REAL_KILL_GROUP(stranger, signal.SIGKILL)
        time.sleep(0.2)
        self.assertIsNone(sleeper.poll(), "an unverified process must never be signalled")
        REAL_SIGNAL(owned, signal.SIGTERM)
        self.assertEqual(sleeper.wait(timeout=10), -signal.SIGTERM)

    def test_an_adopted_pid_that_is_recycled_later_reads_as_exited(self) -> None:
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        sched = self.sched()
        directory = os.path.join(self.root, "state", "tartci-owners-a111111")
        os.makedirs(directory)
        meta_path = os.path.join(directory, od.CHILD_META)
        Path(meta_path).write_text(json.dumps(
            {"name": "tartci-owners-a111111", "lane": "linux-a64", "repo": "o/r", "slot": 1, "token": "t" * 24,
             "pid": sleeper.pid, "pid_started": od.process_started_at(sleeper.pid)}))
        self.assertEqual(len(sched.adopt()), 1)
        child = sched.children["tartci-owners-a111111"]
        self.assertEqual(sched.observe(), [])  # still the same process
        child.proc.started_at = "Thu Jan  1 00:00:00 1970"  # now the pid belongs to something else
        self.assertEqual([(c.name, code) for c, code in sched.observe()], [("tartci-owners-a111111", 0)])
        self.assertEqual(sched.children, {})
        self.assertTrue(json.loads(Path(meta_path).read_text())["ended"])
        self.assertEqual((dict(sched.refusals), sched.backoff_until), ({}, {}))  # unknown exit: not a denial
        self.assertIsNone(sleeper.poll())

    def test_dispatcher_startup_adopts_before_reaping_and_protects_the_adopted(self) -> None:
        self.make_orphan("tartci-owners-a111111", 4242)
        tart_calls: List[List[str]] = []

        def tart(argv: List[str]) -> str:
            tart_calls.append(argv)
            return json.dumps([{"Name": "vm-1"}]) if argv[0] == "list" else ""

        disp = self.make_dispatcher({}, tart=tart, alive=lambda pid, started="": str(pid) == "4242")
        disp.startup()
        self.assertEqual(list(disp.sched.children), ["tartci-owners-a111111"])
        self.assertEqual([c for c in tart_calls if c[0] in ("stop", "delete")], [])  # vm-1 belongs to a live child


class DispatcherTests(SchedulerHarness):
    def dispatcher(self, routes: Dict[str, Any], **cfg: Any) -> od.Dispatcher:
        return self.make_dispatcher(routes, **cfg)

    def queued_a(self, *job_ids: int, labels: Optional[List[str]] = None) -> Dict[str, Any]:
        return scan_routes("org1/a", [run_json(1, "org1/a")],
                           {1: [job_json(n, labels or ["self-hosted", "linux-a64"]) for n in job_ids]})

    def test_scan_cycle_starts_children_for_queued_jobs_only(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")],
                             {1: [job_json(10, ["self-hosted", "linux-a64"]), job_json(11, ["ubuntu-latest"])]})
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        self.assertEqual([s[0][2] for s in self.spawned], ["linux"])
        self.assertEqual(len(disp.sched.children), 1)

    def test_failed_scan_starts_nothing(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")], {})
        routes["repos/org1/a/actions/runs/1/jobs?filter=latest&per_page=100"] = od.GhError("HTTP 502")
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        self.assertEqual(self.spawned, [])
        self.assertIn("502", disp.last_scans["org1/a"].error or "")
        self.assertEqual(disp.snapshot()["scan_errors"]["org1/a"], disp.last_scans["org1/a"].error)

    def test_snapshot_records_the_configured_vm_size(self) -> None:
        disp = self.dispatcher({})
        self.assertEqual(disp.snapshot()["vm_size"], {"macos": {"cores": None, "mem_mb": None},
                                                      "linux": {"cores": None, "mem_mb": None}})

    def test_repo_that_turns_public_is_dropped_and_not_served_again(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")], {1: [job_json(10, ["linux-a64"])]},
                             meta=repo_json("org1/a", visibility="public", private=False))
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        self.assertEqual(self.spawned, [])
        self.assertNotIn("org1/a", disp.directory.current.served)
        self.assertIn("org1/a", disp.directory.current.refused)

    # -- security: a dropped repository must not keep a runner waiting for a job

    def test_dropped_repo_stops_its_unassigned_children_but_lets_assigned_ones_finish(self) -> None:
        routes = self.queued_a(10, 11)
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        running, waiting = list(disp.sched.children.values())
        self.write_state(running, "job-running")
        disp.sched.observe()
        routes["repos/org1/a"] = repo_json("org1/a", visibility="public", private=False)
        self.now += 30
        disp.scan_cycle()
        self.assertEqual(self.sent, [(waiting.proc.pid, signal.SIGTERM)])
        self.assertTrue(waiting.terminating)
        self.assertFalse(running.terminating)
        self.assertEqual(len(self.spawned), 2)  # nothing new was started for the dropped repo

    def test_child_assigned_between_observe_and_a_security_drop_is_stopped(self) -> None:
        # Its state file moved on since the last observe, so the drop is the first time we
        # see the assignment: that is "recently assigned", and it may hold the very job that
        # made the repository unsafe (M1b). A non-security drop leaves it alone (next test).
        routes = self.queued_a(10)
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        child = next(iter(disp.sched.children.values()))
        self.write_state(child, "job-running")
        routes["repos/org1/a"] = repo_json("org1/a", visibility="public", private=False)
        self.now += 30
        disp.scan_cycle()
        self.assertEqual(self.sent, [(child.proc.pid, signal.SIGTERM)])

    def test_child_assigned_between_observe_and_a_non_security_drop_is_left_alone(self) -> None:
        routes = self.queued_a(10)
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        child = next(iter(disp.sched.children.values()))
        self.write_state(child, "job-running")
        routes["repos/org1/a"] = repo_json("org1/a", archived=True)
        self.now += 30
        disp.scan_cycle()
        self.assertEqual(self.sent, [])
        self.assertFalse(child.terminating)

    def test_run_reporting_a_public_repo_drops_and_stops_waiting_children(self) -> None:
        routes = self.queued_a(10)
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        child = next(iter(disp.sched.children.values()))
        public_run = run_json(1, "org1/a")
        public_run["repository"]["private"] = False
        routes["repos/org1/a/actions/runs?per_page=100"] = {"workflow_runs": [public_run]}
        self.now += 30
        disp.scan_cycle()
        self.assertEqual(self.sent, [(child.proc.pid, signal.SIGTERM)])
        self.assertIn("org1/a", disp.directory.current.refused)

    def test_fork_headed_run_refuses_the_repo_and_stops_its_waiting_children(self) -> None:
        routes = self.queued_a(10, 11)
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        self.assertEqual(len(disp.sched.children), 2)
        kids = list(disp.sched.children.values())
        routes["repos/org1/a/actions/runs?per_page=100"] = {
            "workflow_runs": [run_json(1, "org1/a"), run_json(2, "org1/a", head="stranger/a")]}
        routes["repos/org1/a/actions/runs/2/jobs?filter=latest&per_page=100"] = {"jobs": [job_json(12, ["linux-a64"])]}
        self.now += 30
        disp.scan_cycle()
        self.assertEqual(sorted(self.sent), sorted((k.proc.pid, signal.SIGTERM) for k in kids))
        self.assertNotIn("org1/a", disp.directory.current.served)
        self.assertIn("fork", disp.directory.current.refused["org1/a"])
        self.assertEqual(len(self.spawned), 2)

    def test_repo_refused_by_a_later_discovery_also_loses_its_waiting_children(self) -> None:
        routes = self.queued_a(10)
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        child = next(iter(disp.sched.children.values()))
        routes[fork_route("org1/a")] = {"run_workflows_from_fork_pull_requests": True}  # someone turned it on
        self.now += od.DISCOVERY_SECONDS + 1
        disp.scan_cycle()
        self.assertEqual(self.sent, [(child.proc.pid, signal.SIGTERM)])
        self.assertIn("org1/a", disp.directory.current.refused)
        self.assertIn("org1/a", disp.directory.current.refused_security)

    # -- N2: only SECURITY drops stop waiting children; the others let them idle out

    def test_non_security_scan_drop_starts_nothing_new_and_leaves_waiting_children_alone(self) -> None:
        for meta in (repo_json("org1/a", archived=True), repo_json("org1/a", disabled=True)):
            with self.subTest(reason=od.refusal_reason(meta)):
                self.spawned.clear()
                self.sent.clear()
                routes = self.queued_a(10, 11)
                disp = self.dispatcher(routes)
                disp.scan_cycle()
                kids = list(disp.sched.children.values())
                self.assertEqual(len(kids), 2)
                routes["repos/org1/a"] = meta
                routes["repos/org1/a/actions/runs/1/jobs?filter=latest&per_page=100"] = {
                    "jobs": [job_json(n, ["linux-a64"]) for n in (10, 11, 12, 13)]}  # more work arrives
                self.now += 30
                disp.scan_cycle()
                self.assertEqual(self.sent, [])  # nobody signalled: a just-assigned job is not killed
                self.assertFalse(any(k.terminating for k in kids))
                self.assertEqual(len(self.spawned), 2)  # ...and nothing new is started for the repo
                self.assertNotIn("org1/a", disp.directory.current.served)
                self.assertIn("org1/a", disp.directory.current.refused)
                self.assertNotIn("org1/a", disp.directory.current.refused_security)
                for kid in kids:  # they idle out unassigned (tartci's 120 s assignment timeout)
                    self.write_state(kid, "idle-wait")
                    kid.proc.code = od.IDLE_EXIT_CODE
                self.assertEqual(len(disp.sched.observe()), 2)
                self.assertEqual(disp.sched.children, {})

    def test_non_security_discovery_refusals_leave_waiting_children_alone(self) -> None:
        cases = {
            "admin lost": lambda routes: routes.__setitem__("orgs/org1/repos?type=private",
                                                            [repo_json("org1/a", permissions={"admin": False})]),
            "archived": lambda routes: routes.__setitem__("orgs/org1/repos?type=private",
                                                          [repo_json("org1/a", archived=True)]),
            "setting 403": lambda routes: routes.__setitem__(fork_route("org1/a"),
                                                             od.GhError("HTTP 403 on setting", status=403)),
            "setting 404": lambda routes: routes.__setitem__(fork_route("org1/a"),
                                                             od.GhError("HTTP 404 on setting", status=404)),
        }
        for label, change in cases.items():
            with self.subTest(case=label):
                self.sent.clear()
                routes = self.queued_a(10)
                disp = self.dispatcher(routes)
                disp.scan_cycle()
                child = next(iter(disp.sched.children.values()))
                change(routes)
                self.now += od.DISCOVERY_SECONDS + 1
                disp.scan_cycle()
                self.assertNotIn("org1/a", disp.directory.current.served)
                self.assertIn("org1/a", disp.directory.current.refused)
                self.assertNotIn("org1/a", disp.directory.current.refused_security)
                self.assertEqual(self.sent, [])
                self.assertFalse(child.terminating)

    def test_a_repository_that_vanishes_from_the_private_listing_counts_as_security(self) -> None:
        routes = self.queued_a(10)
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        child = next(iter(disp.sched.children.values()))
        routes["orgs/org1/repos?type=private"] = []  # it may just have turned public
        self.now += od.DISCOVERY_SECONDS + 1
        disp.scan_cycle()
        self.assertEqual(self.sent, [(child.proc.pid, signal.SIGTERM)])

    def test_directory_marks_which_refusals_are_security(self) -> None:
        listing = [repo_json("org1/a"), repo_json("org1/b", archived=True), repo_json("org1/c", visibility="internal"),
                   repo_json("org1/d", permissions={"admin": False}), repo_json("org1/e"), repo_json("org1/f")]
        routes: Dict[str, Any] = {"users/org1": {"type": "Organization"}, "orgs/org1/repos?type=private": listing}
        for name in "adef":
            routes[fork_route(f"org1/{name}")] = FORK_OFF
        routes[fork_route("org1/e")] = {"run_workflows_from_fork_pull_requests": True}
        routes[fork_route("org1/f")] = od.GhError("HTTP 404", status=404)
        routes[fork_route("org1/b")] = FORK_OFF
        directory = od.Directory(FakeGh(routes), od.parse_config({"owners": ["org1"]}))
        current = directory.refresh()
        self.assertEqual(sorted(current.served), ["org1/a"])
        self.assertEqual(current.refused_security, {"org1/c", "org1/e"})  # not private; fork setting on
        self.assertEqual(sorted(set(current.refused) - current.refused_security), ["org1/b", "org1/d", "org1/f"])

    # -- N3: a fork job may already sit on an assigned child

    def three_children(self, routes: Dict[str, Any]) -> Tuple[od.Dispatcher, List[od.Child]]:
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        kids = list(disp.sched.children.values())
        self.assertEqual(len(kids), 3)
        self.write_state(kids[0], "job-running", vm=f"linux-ephr-{kids[0].proc.pid}-1")  # holds the fork job
        self.write_state(kids[1], "job-running", vm=f"linux-ephr-{kids[1].proc.pid}-1")  # a legitimate job
        disp.sched.observe()
        self.assertEqual([k.assigned_at is not None for k in kids], [True, True, False])
        return disp, kids

    def test_fork_run_stops_the_child_that_holds_its_job_even_though_it_is_assigned(self) -> None:
        routes = self.queued_a(10, 11, 12)
        disp, kids = self.three_children(routes)
        routes["repos/org1/a/actions/runs?per_page=100"] = {
            "workflow_runs": [run_json(1, "org1/a"), run_json(2, "org1/a", "in_progress", head="stranger/a")]}
        routes["repos/org1/a/actions/runs/2/jobs?filter=latest&per_page=100"] = {"jobs": [
            dict(job_json(20, ["linux-a64"], "in_progress"), runner_name=f"linux-ephr-{kids[0].proc.pid}-1"),
            dict(job_json(21, ["linux-a64"]), runner_name=None)]}
        self.now += 30
        disp.scan_cycle()
        self.assertEqual(sorted(self.sent), sorted([(kids[0].proc.pid, signal.SIGTERM), (kids[2].proc.pid, signal.SIGTERM)]))
        self.assertFalse(kids[1].terminating)  # its own job is not a fork job: it finishes
        self.assertIn("org1/a", disp.directory.current.refused_security)
        self.assertEqual(len(self.spawned), 3)

    def test_fork_run_with_unreadable_jobs_stops_every_child_of_the_repo(self) -> None:
        routes = self.queued_a(10, 11, 12)
        disp, kids = self.three_children(routes)
        routes["repos/org1/a/actions/runs?per_page=100"] = {
            "workflow_runs": [run_json(1, "org1/a"), run_json(2, "org1/a", "action_required", head="stranger/a")]}
        routes["repos/org1/a/actions/runs/2/jobs?filter=latest&per_page=100"] = od.GhError("HTTP 502", status=502)
        self.now += 30
        disp.scan_cycle()
        self.assertEqual(sorted(self.sent), sorted((k.proc.pid, signal.SIGTERM) for k in kids))  # assigned ones too
        self.assertTrue(all(k.terminating for k in kids))
        self.assertEqual(len(self.spawned), 3)

    def test_fork_run_leaves_other_repositories_children_alone(self) -> None:
        routes = self.queued_a(10)
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        other = disp.sched.start("org1/z", "linux-a64")
        self.write_state(other, "job-running")
        routes["repos/org1/a/actions/runs?per_page=100"] = {"workflow_runs": [run_json(2, "org1/a", head="stranger/a")]}
        routes["repos/org1/a/actions/runs/2/jobs?filter=latest&per_page=100"] = od.GhError("HTTP 502", status=502)
        self.now += 30
        disp.scan_cycle()
        self.assertNotIn((other.proc.pid, signal.SIGTERM), self.sent)

    def test_child_recognises_its_runner_registration(self) -> None:
        sched = self.sched()
        linux = sched.start("o/r", "linux-a64")
        mac = sched.start("o/r", "macos-a64")
        pid = linux.proc.pid
        self.assertTrue(linux.owns_runner(f"linux-ephr-{pid}-3"))
        self.assertFalse(linux.owns_runner(f"linux-ephr-{pid}0-3"))
        self.assertFalse(linux.owns_runner(f"linux-ephr-{pid + 1}-1"))
        self.assertTrue(mac.owns_runner(f"{mac.name}-{mac.proc.pid}-1"))
        self.assertFalse(mac.owns_runner(f"{mac.name}9-{mac.proc.pid}-1"))  # a different child's prefix
        self.assertFalse(mac.owns_runner(""))
        linux.vm = "custom-vm-name"
        self.assertTrue(linux.owns_runner("custom-vm-name"))

    # -- failure handling

    def test_sustained_failure_exits_75_only_after_children_finish(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")], {1: [job_json(10, ["linux-a64"])]})
        disp = self.dispatcher(routes)
        disp.scan_cycle()
        self.assertEqual(len(disp.sched.children), 1)
        routes["repos/org1/a"] = od.GhError("network down")
        self.now += 100
        disp.scan_cycle()
        self.assertIsNone(disp.exit_code)  # failing, not yet "sustained"
        self.now += od.SUSTAINED_FAILURE_SECONDS
        disp.scan_cycle()
        self.assertEqual(disp.exit_code, 75)
        self.assertTrue(disp.sched.stopping)
        self.assertEqual(len(disp.sched.children), 1)  # never exits while a child runs

    def test_rate_limit_with_retry_after_is_honoured_and_never_counts_as_unreachable(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")], {1: [job_json(10, ["linux-a64"])]})
        disp = self.dispatcher(routes)
        disp.client.default_headers = {"X-Ratelimit-Remaining": "4800", "X-Ratelimit-Limit": "5000",
                                       "X-Ratelimit-Reset": str(int(self.now) + 3000)}
        disp.scan_cycle()
        self.assertEqual(disp.client.rate.remaining, 4800)  # tracked through the fake's headers
        routes["repos/org1/a"] = od.GhError("HTTP 403: secondary rate limit", status=403,
                                            rate_limited=True, retry_after=600.0)
        for _ in range(6):  # 1 hour of being rate limited: far beyond the 3-minute unreachable rule
            self.now = disp.next_scan
            disp.scan_cycle()
            self.assertGreaterEqual(disp.next_scan - self.now, 600.0)  # not "next scan in 10 s"
            self.assertIsNone(disp.error_since)
            self.assertIsNone(disp.exit_code)
            self.assertFalse(disp.sched.stopping)
        # A push hint cannot bypass the wait.
        disp.request_scan("org1/a")
        self.assertFalse(disp._scan_due())
        self.now = disp.rate_block_until
        self.assertTrue(disp._scan_due())

    def test_largest_wait_of_the_cycle_wins_and_other_failures_still_count(self) -> None:
        routes: Dict[str, Any] = {"orgs/org1/repos?type=private": [repo_json("org1/a"), repo_json("org1/b")],
                                  fork_route("org1/b"): FORK_OFF}
        routes.update(scan_routes("org1/a", [], {}))
        routes.update(scan_routes("org1/b", [], {}))
        disp = self.dispatcher(routes)
        routes["repos/org1/a"] = od.GhError("429", status=429, rate_limited=True, retry_after=120.0)
        routes["repos/org1/b"] = od.GhError("403", status=403, rate_limited=True, retry_after=900.0)
        disp.scan_cycle()
        self.assertEqual(disp.next_scan - self.now, 900.0)
        self.assertIsNone(disp.error_since)
        # One repo rate limited, the other genuinely failing: nothing was readable, so it counts.
        routes["repos/org1/b"] = od.GhError("connection reset")
        self.now = disp.next_scan
        disp.scan_cycle()
        self.assertIsNotNone(disp.error_since)

    def test_rate_headroom_widens_the_next_scan(self) -> None:
        routes = scan_routes("org1/a", [], {})
        disp = self.dispatcher(routes)
        disp.client.rate = od.RateState(300, 5000, self.now + 1000)
        disp.scan_cycle()
        self.assertGreater(disp.next_scan - self.now, disp.cfg.poll_seconds)

    def test_rate_headers_from_the_client_widen_the_interval(self) -> None:
        routes = scan_routes("org1/a", [], {})
        disp = self.dispatcher(routes)
        disp.client.default_headers = {"X-Ratelimit-Remaining": "300", "X-Ratelimit-Limit": "5000",
                                       "X-Ratelimit-Reset": str(int(self.now) + 1000)}
        disp.scan_cycle()
        self.assertEqual(disp.client.rate.remaining, 300)
        self.assertGreater(disp.next_scan - self.now, disp.cfg.poll_seconds)
        self.assertIn("widened", disp.interval_note)

    def test_spawn_failure_does_not_stop_the_cycle_and_backs_the_pair_off(self) -> None:
        routes = self.queued_a(10)
        disp = self.dispatcher(routes)

        def emfile(argv: List[str], env: Dict[str, str], log_path: str) -> Any:
            raise OSError(errno.EMFILE, "Too many open files")

        disp.sched.spawn = emfile
        disp.scan_cycle()
        self.assertEqual(disp.sched.children, {})
        self.assertEqual(disp.tick_errors, 1)
        self.assertGreater(disp.sched.backoff_until[("org1/a", "linux-a64")], self.now)

    def test_dry_run_output_names_lane_and_never_starts_anything(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")],
                             {1: [job_json(10, ["self-hosted", "macos-a64"]), job_json(11, ["self-hosted", "gpu"])]})
        disp = self.dispatcher(routes)
        found = disp.directory.refresh()
        scans = od.scan_all(disp.scanner, sorted(found.served))
        lines, errors = od.format_dry_run(disp.cfg, found, scans)
        text = "\n".join(lines)
        self.assertEqual(errors, 0)
        self.assertIn("would serve job 10", text)
        self.assertIn("-> lane macos-a64", text)
        self.assertIn("not served job 11", text)
        self.assertEqual(self.spawned, [])


class ForkGuardTests(SchedulerHarness):
    """M1: the fork-head guard outlives any refusal, and security drops are conservative about
    children whose assignment we may have seen late."""

    RUNS = "repos/org1/a/actions/runs?per_page=100"

    @staticmethod
    def jobs_route(run_id: int) -> str:
        return f"repos/org1/a/actions/runs/{run_id}/jobs?filter=latest&per_page=100"

    def start(self, *job_ids: int) -> Tuple[Dict[str, Any], od.Dispatcher, List[od.Child]]:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")],
                             {1: [job_json(n, ["self-hosted", "linux-a64"]) for n in job_ids]})
        disp = self.make_dispatcher(routes)
        disp.scan_cycle()
        kids = list(disp.sched.children.values())
        self.assertEqual(len(kids), len(job_ids))
        return routes, disp, kids

    def assign(self, disp: od.Dispatcher, *kids: od.Child) -> None:
        for kid in kids:
            self.write_state(kid, "job-running", vm=f"linux-ephr-{kid.proc.pid}-1")
        disp.sched.observe()

    def fork_run(self, routes: Dict[str, Any], jobs: List[Dict[str, Any]], status: str = "in_progress") -> None:
        routes[self.RUNS] = {"workflow_runs": [run_json(1, "org1/a"), run_json(2, "org1/a", status, head="stranger/a")]}
        routes[self.jobs_route(2)] = {"jobs": jobs}

    @staticmethod
    def fork_job(job_id: int, runner: Optional[str], status: str = "in_progress") -> Dict[str, Any]:
        return dict(job_json(job_id, ["linux-a64"], status), runner_name=runner)

    def terms(self, *kids: od.Child) -> List[Tuple[int, Any]]:
        return sorted((k.proc.pid, signal.SIGTERM) for k in kids)

    # -- (1) the guard outlives the refusal

    def test_discovery_first_security_refusal_still_stops_the_assigned_child_holding_a_fork_job(self) -> None:
        routes, disp, kids = self.start(10, 11, 12)
        self.assign(disp, kids[0], kids[1])  # kids[0] will hold the fork job, kids[1] a legitimate one
        self.now += od.DISCOVERY_SECONDS + 1
        routes[fork_route("org1/a")] = {"run_workflows_from_fork_pull_requests": True}
        self.fork_run(routes, [self.fork_job(20, f"linux-ephr-{kids[0].proc.pid}-1")])
        disp.scan_cycle()
        self.assertIn("org1/a", disp.directory.current.refused_security)
        self.assertIn("org1/a", disp.last_guards)  # not served, so the scan was the guard's
        self.assertEqual(sorted(self.sent), self.terms(kids[0], kids[2]))  # the holder and the unassigned one
        self.assertFalse(kids[1].terminating)  # a long-assigned child on a non-fork job finishes
        self.assertEqual(len(self.spawned), 3)

    def test_fork_run_appearing_while_children_idle_out_after_a_non_security_drop_stops_the_holder(self) -> None:
        routes, disp, kids = self.start(10, 11, 12)
        self.assign(disp, kids[0], kids[1])
        routes["repos/org1/a"] = repo_json("org1/a", archived=True)  # a non-security drop
        for _ in range(2):  # long-assigned children on non-fork jobs are NOT stopped, cycle after cycle
            self.now += 30
            disp.scan_cycle()
            self.assertEqual(self.sent, [])
            self.assertFalse(any(k.terminating for k in kids))
            self.assertIn("org1/a", disp.last_guards)
        self.assertNotIn("org1/a", disp.directory.current.refused_security)
        self.fork_run(routes, [self.fork_job(20, f"linux-ephr-{kids[0].proc.pid}-1")])  # a fork PR is run now
        self.now += 30
        disp.scan_cycle()
        self.assertEqual(sorted(self.sent), self.terms(kids[0], kids[2]))
        self.assertFalse(kids[1].terminating)
        self.assertIn("org1/a", disp.directory.current.refused_security)  # fork evidence is a security matter

    def test_guard_stops_when_the_children_are_gone_and_costs_nothing_without_children(self) -> None:
        routes, disp, kids = self.start(10)
        routes["repos/org1/a"] = repo_json("org1/a", archived=True)
        self.now += 30
        disp.scan_cycle()
        self.assertEqual(list(disp.last_guards), ["org1/a"])
        kids[0].proc.code = od.IDLE_EXIT_CODE
        disp.sched.observe()
        before = len(disp.client.calls)
        self.now += 30
        disp.scan_cycle()
        self.assertEqual(disp.last_guards, {})
        self.assertEqual(len(disp.client.calls), before)  # nothing served, nothing to guard: no GitHub call

    def test_guard_does_not_repeat_the_run_scan_a_served_scan_just_did(self) -> None:
        routes, disp, kids = self.start(10, 11)
        self.fork_run(routes, [self.fork_job(20, f"linux-ephr-{kids[0].proc.pid}-1")])
        self.now += 30
        disp.scan_cycle()  # the served scan found the fork run itself
        self.assertEqual(disp.last_guards, {})
        self.assertEqual(len([c for c in disp.client.calls if c.startswith(self.jobs_route(2))]), 1)

    # -- (3) a fork job with no runner name yet is read conservatively

    def test_a_fork_job_running_on_no_named_runner_stops_every_child(self) -> None:
        for mode in ("served scan", "discovery-first refusal"):
            with self.subTest(mode=mode):
                self.sent.clear()
                routes, disp, kids = self.start(10, 11, 12)
                self.assign(disp, kids[0], kids[1])
                self.now += od.DISCOVERY_SECONDS + 1 if mode == "discovery-first refusal" else 30
                if mode == "discovery-first refusal":
                    routes[fork_route("org1/a")] = {"run_workflows_from_fork_pull_requests": True}
                self.fork_run(routes, [self.fork_job(20, None, "in_progress")])
                disp.scan_cycle()
                self.assertEqual(sorted(self.sent), self.terms(*kids))  # the assigned ones too
                self.assertTrue(all(k.terminating for k in kids))

    def test_a_fork_job_still_queued_is_read_again_next_cycle_and_its_taker_is_stopped(self) -> None:
        routes, disp, kids = self.start(10, 11)
        self.assign(disp, kids[0])  # kids[0]: legitimate job, observed long ago; kids[1]: waiting
        self.fork_run(routes, [self.fork_job(20, None, "queued")])
        self.now += 30
        disp.scan_cycle()
        self.assertTrue(disp.last_scans["org1/a"].fork_pending)
        self.assertEqual(self.sent, self.terms(kids[1]))  # the waiting one: it could take the fork job
        self.assertFalse(kids[0].terminating)
        self.fork_run(routes, [self.fork_job(20, f"linux-ephr-{kids[0].proc.pid}-1")])  # now it names its runner
        self.now += 30
        disp.scan_cycle()
        self.assertIn("org1/a", disp.last_guards)  # the repository is unserved but has a child: guarded
        self.assertEqual(sorted(self.sent), self.terms(kids[0], kids[1]))

    # -- (2) a security drop also stops recently assigned children

    def test_a_child_whose_assignment_was_first_seen_at_the_drop_is_stopped_with_a_fork_job_queued(self) -> None:
        # The race: the fork job was read while queued (no runner), then a child took it, and
        # its state said job-running before the stop pass refreshed it.
        routes, disp, kids = self.start(10, 11)
        self.fork_run(routes, [self.fork_job(20, None, "queued")])
        self.write_state(kids[1], "job-running", vm=f"linux-ephr-{kids[1].proc.pid}-1")  # not observed yet
        self.now += 30
        disp.scan_cycle()
        self.assertEqual(sorted(self.sent), self.terms(*kids))
        self.assertTrue(disp.last_scans["org1/a"].fork_pending)

    def test_a_security_drop_stops_children_assigned_within_the_lag_and_spares_older_ones(self) -> None:
        routes, disp, kids = self.start(10, 11, 12)
        self.assign(disp, kids[0])  # observed at 1000
        self.now += 90
        self.assign(disp, kids[1])  # observed at 1090
        self.now += 10  # the drop is detected at 1100: 1090 is within the lag, 1000 is not
        routes["repos/org1/a"] = repo_json("org1/a", visibility="public", private=False)
        disp.scan_cycle()
        self.assertEqual(sorted(self.sent), self.terms(kids[1], kids[2]))  # recent + waiting
        self.assertFalse(kids[0].terminating)
        self.assertGreaterEqual(od.SECURITY_ASSIGN_LAG_SECONDS, 10.0)
        self.assertGreaterEqual(od.SECURITY_ASSIGN_LAG_SECONDS, 2 * 5)  # twice the runner's 5 s assignment poll

    def test_the_cut_off_is_the_first_detection_not_the_latest_cycle(self) -> None:
        routes, disp, kids = self.start(10, 11)
        self.assign(disp, kids[0])
        routes["repos/org1/a"] = repo_json("org1/a", visibility="public", private=False)
        self.now += 30
        disp.scan_cycle()  # drop detected at 1030; kids[0] (1000) spared, kids[1] stopped
        self.assertEqual(self.sent, self.terms(kids[1]))
        for _ in range(3):  # later guard cycles must not start counting kids[0] as "recent"
            self.now += 30
            disp.scan_cycle()
        self.assertEqual(self.sent, self.terms(kids[1]))
        self.assertFalse(kids[0].terminating)

    # -- the guard reading itself

    def test_guard_repo_reads_runs_only_and_reports_fork_evidence(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")], {1: [job_json(10, ["linux-a64"])]})
        client = FakeGh(routes)
        scan = od.Scanner(client).guard_repo("org1/a")
        self.assertEqual((scan.error, scan.dropped, scan.dropped_security), (None, None, False))
        self.assertNotIn(self.jobs_route(1), client.calls)  # the jobs of an ordinary run are never listed
        routes["repos/org1/a"] = repo_json("org1/a", visibility="public", private=False)
        scan = od.Scanner(FakeGh(routes)).guard_repo("org1/a")
        self.assertTrue(scan.dropped_security)
        self.fork_run(routes, [self.fork_job(20, "r-1"), self.fork_job(21, None, "queued"),
                               self.fork_job(22, None, "in_progress")])
        scan = od.Scanner(FakeGh(routes)).guard_repo("org1/a")
        self.assertEqual((scan.fork_runners, scan.fork_pending, scan.fork_unknown_runner), (["r-1"], True, True))
        routes[self.RUNS] = od.GhError("HTTP 502", status=502)
        scan = od.Scanner(FakeGh(routes)).guard_repo("org1/a")
        self.assertIn("502", scan.error or "")

    # -- M4: an owner listing that failed is "unknown", not "refused"

    def test_a_failed_owner_listing_stops_only_strictly_waiting_children(self) -> None:
        # M4/K3: after a restart nothing is remembered as served, so a failed owner listing
        # leaves the repository unknown, not refused: assigned children (even ones assigned
        # moments ago) keep their jobs, but a child still waiting is cheap to stop and could
        # take a job nobody vetted.
        for error in (od.GhError("HTTP 502", status=502),
                      od.GhError("rate limited", status=403, rate_limited=True, retry_after=60.0)):
            with self.subTest(error=str(error)):
                self.sent.clear()
                routes = scan_routes("org1/a", [run_json(1, "org1/a")], {})
                disp = self.make_dispatcher(routes)
                waiting = disp.sched.start("org1/a", "linux-a64")  # as adopted after a restart
                busy = disp.sched.start("org1/a", "linux-a64")
                self.assign(disp, busy)  # assigned this very tick: inside any lag window
                routes["orgs/org1/repos?type=private"] = error
                disp.scan_cycle()
                self.assertIn("org1", disp.directory.current.errors)
                self.assertNotIn("org1/a", disp.directory.current.refused)
                self.assertEqual(self.sent, self.terms(waiting))
                self.assertFalse(busy.terminating)
                self.assertIn("org1/a", disp.last_guards)  # the fork-head guard still watches it

    def test_a_failed_owner_listing_still_lets_the_guard_stop_a_fork_job_holder(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")], {})
        disp = self.make_dispatcher(routes)
        holder = disp.sched.start("org1/a", "linux-a64")
        legit = disp.sched.start("org1/a", "linux-a64")
        self.assign(disp, holder, legit)
        self.now += 100
        routes["orgs/org1/repos?type=private"] = od.GhError("HTTP 502", status=502)
        self.fork_run(routes, [self.fork_job(20, f"linux-ephr-{holder.proc.pid}-1")])
        disp.scan_cycle()
        self.assertEqual(self.sent, self.terms(holder))

    def test_a_repository_that_turned_public_still_has_its_fork_job_holder_found(self) -> None:
        # K1: every run of a public repository reports private=False; that must not hide the
        # fork-headed run, or a long-assigned child holding its job would run it to the end.
        routes, disp, kids = self.start(10, 11, 12)
        self.assign(disp, kids[0], kids[1])
        self.now += 60  # both assignments are far older than the security lag
        public = {"full_name": "org1/a", "private": False}
        routes[self.RUNS] = {"workflow_runs": [run_json(1, "org1/a", repository=public),
                                               run_json(2, "org1/a", "in_progress", head="stranger/a",
                                                        repository=public)]}
        routes[self.jobs_route(2)] = {"jobs": [self.fork_job(20, f"linux-ephr-{kids[0].proc.pid}-1")]}
        disp.scan_cycle()
        self.assertEqual(sorted(self.sent), self.terms(kids[0], kids[2]))  # holder + waiting child
        self.assertFalse(kids[1].terminating)

    def test_public_metadata_and_public_runs_still_find_the_fork_job_holder(self) -> None:
        # J3: the realistic path: GitHub reports the repository public in its metadata too,
        # so scan_repo drops it on the metadata and the guard, in the same cycle, finds the holder.
        routes, disp, kids = self.start(10, 11, 12)
        self.assign(disp, kids[0], kids[1])
        self.now += 60
        public = {"full_name": "org1/a", "private": False}
        routes["repos/org1/a"] = repo_json("org1/a", private=False, visibility="public")
        routes[self.RUNS] = {"workflow_runs": [run_json(1, "org1/a", repository=public),
                                               run_json(2, "org1/a", "in_progress", head="stranger/a",
                                                        repository=public)]}
        routes[self.jobs_route(2)] = {"jobs": [self.fork_job(20, f"linux-ephr-{kids[0].proc.pid}-1")]}
        disp.scan_cycle()
        self.assertIn("org1/a", disp.last_guards)
        self.assertEqual(sorted(self.sent), self.terms(kids[0], kids[2]))
        self.assertFalse(kids[1].terminating)

    def test_guard_repo_on_a_public_repository_reads_the_fork_runners(self) -> None:
        public = {"full_name": "org1/a", "private": False}
        routes = {"repos/org1/a": repo_json("org1/a"),
                  self.RUNS: {"workflow_runs": [run_json(1, "org1/a", repository=public),
                                                run_json(2, "org1/a", "in_progress", head="stranger/a",
                                                         repository=public)]},
                  self.jobs_route(2): {"jobs": [self.fork_job(20, "linux-ephr-42-1")]}}
        result = od.Scanner(FakeGh(routes)).guard_repo("org1/a")
        self.assertTrue(result.dropped_security)
        self.assertEqual(result.fork_runners, ["linux-ephr-42-1"])

    def test_a_public_repository_without_fork_runs_is_still_a_security_drop(self) -> None:
        public = {"full_name": "org1/a", "private": False}
        routes = {"repos/org1/a": repo_json("org1/a"),
                  self.RUNS: {"workflow_runs": [run_json(1, "org1/a", repository=public)]}}
        result = od.Scanner(FakeGh(routes)).guard_repo("org1/a")
        self.assertTrue(result.dropped_security)
        self.assertEqual(result.dropped, "run reports a public repository")
        self.assertEqual(result.fork_runners, [])

    def test_a_repository_missing_from_a_good_listing_is_still_stopped(self) -> None:
        routes, disp, kids = self.start(10)
        routes["orgs/org1/repos?type=private"] = []
        self.now += od.DISCOVERY_SECONDS + 1
        disp.scan_cycle()
        self.assertEqual(self.sent, self.terms(kids[0]))


class RunLoopTests(SchedulerHarness):
    """Dispatcher.run(): the loop that must never die while children are alive."""

    def setUp(self) -> None:
        super().setUp()
        self.sleeps = 0
        self.hooks: Dict[int, Callable[[], None]] = {}

    def disp(self, routes: Dict[str, Any], **kw: Any) -> od.Dispatcher:
        disp = self.make_dispatcher(routes, **kw)

        def sleep(seconds: float) -> None:
            self.sleeps += 1
            self.now += 50.0
            hook = self.hooks.get(self.sleeps)
            if hook:
                hook()
            if self.sleeps > 200:
                raise AssertionError("run() did not return")

        disp.sleep = sleep
        return disp

    def test_sigterm_during_a_scan_starts_no_children_and_shuts_down(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")], {1: [job_json(10, ["linux-a64"])]})
        disp = self.disp(routes)
        disp.client.on_get = lambda path: disp.request_stop(signal.SIGTERM) if path.endswith("per_page=100") and "/runs" in path else None
        code = disp.run()
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertEqual(self.spawned, [])  # the scan in flight did not spawn
        self.assertTrue(disp.sched.stopping)
        self.assertFalse(json.loads(Path(self.root, "state", "dispatcher.json").read_text())["running"])

    def test_request_stop_stops_admission_immediately(self) -> None:
        disp = self.disp(scan_routes("org1/a", [], {}))
        self.assertFalse(disp.sched.stopping)
        disp.request_stop(signal.SIGINT)
        self.assertTrue(disp.sched.stopping)
        self.assertEqual(disp.sched.plan([self.job(1, "linux-a64")], self.now), [])

    def test_sigterm_signals_every_running_child_before_returning(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")], {1: [job_json(10, ["linux-a64"])]})
        disp = self.disp(routes)

        def stop_and_let_children_exit() -> None:
            disp.request_stop(signal.SIGTERM)
            for _, _, _, proc in self.spawned:
                proc.code = 143

        self.hooks[1] = stop_and_let_children_exit
        self.assertEqual(disp.run(), 128 + signal.SIGTERM)
        self.assertEqual(len(self.spawned), 1)
        pid = self.spawned[0][3].pid
        self.assertEqual(self.sent, [(pid, signal.SIGTERM)])  # exited within the grace: no SIGKILL
        self.assertEqual(disp.sched.children, {})

    def test_an_exception_in_a_tick_is_logged_and_the_loop_survives(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")], {1: [job_json(10, ["linux-a64"])]})
        disp = self.disp(routes)
        real = od.scan_all
        calls = {"n": 0}

        def flaky(scanner: Any, repos: Any, workers: int = 8) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("scan blew up")
            return real(scanner, repos, workers)

        self.hooks[3] = lambda: disp.request_stop(signal.SIGTERM)
        with mock.patch.object(od, "scan_all", flaky):
            code = disp.run()
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertGreaterEqual(disp.tick_errors, 1)
        self.assertGreaterEqual(calls["n"], 2)  # it scanned again after the failure
        self.assertEqual(len(self.spawned), 1)  # and started the child the failure had delayed

    def test_a_scan_that_keeps_raising_counts_toward_exit_75_but_waits_for_children(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")], {1: [job_json(10, ["linux-a64"])]})
        disp = self.disp(routes)
        real = od.scan_all
        broken = {"on": False}

        def maybe(scanner: Any, repos: Any, workers: int = 8) -> Any:
            if broken["on"]:
                raise RuntimeError("scan blew up")
            return real(scanner, repos, workers)

        def break_scans() -> None:
            broken["on"] = True

        def children_finish() -> None:
            for _, _, _, proc in self.spawned:
                proc.code = 0

        self.hooks[1] = break_scans
        self.hooks[40] = children_finish
        with mock.patch.object(od, "scan_all", maybe):
            code = disp.run()
        self.assertEqual(code, 75)
        self.assertGreaterEqual(self.sleeps, 40)  # it kept running (children alive) until the hook ended them
        self.assertEqual(disp.sched.children, {})

    def test_exit_75_is_returned_only_when_no_children_remain(self) -> None:
        routes = scan_routes("org1/a", [run_json(1, "org1/a")], {1: [job_json(10, ["linux-a64"])]})
        disp = self.disp(routes)

        def github_down() -> None:
            routes["repos/org1/a"] = od.GhError("network down")

        def child_finishes() -> None:
            for _, _, _, proc in self.spawned:
                proc.code = 0

        self.hooks[1] = github_down
        self.hooks[30] = child_finishes
        self.assertEqual(disp.run(), 75)
        self.assertGreaterEqual(self.sleeps, 30)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(disp.sched.children, {})

    def test_startup_failures_do_not_stop_the_dispatcher(self) -> None:
        routes = scan_routes("org1/a", [], {})

        def broken_tart(argv: List[str]) -> str:
            raise RuntimeError("tart exploded")

        disp = self.disp(routes, tart=broken_tart)
        self.hooks[1] = lambda: disp.request_stop(signal.SIGTERM)
        self.assertEqual(disp.run(), 128 + signal.SIGTERM)

    def test_restart_adopts_a_live_child_and_never_exceeds_max_jobs(self) -> None:
        name = "tartci-owners-a111111"
        directory = Path(self.root, "state", name)
        directory.mkdir(parents=True)
        (directory / od.CHILD_META).write_text(json.dumps(
            {"name": name, "lane": "linux-a64", "repo": "org1/a", "slot": 1, "token": "t" * 24, "spawned_at": 900.0,
             "pid": 7777, "pid_started": fake_started(7777)}))
        (directory / f"{name}.state.json").write_text(json.dumps(
            {"vm": "vm-1", "phase": "job-running", "supervisor_pid": "7777", "repo": "org1/a",
             "supervisor_pid_started_at": fake_started(7777)}))
        routes = scan_routes("org1/a", [run_json(1, "org1/a")],
                             {1: [job_json(10, ["linux-a64"]), job_json(11, ["linux-a64"])]})
        disp = self.disp(routes, alive=lambda pid, started="": str(pid) == "7777", max_jobs=1)
        self.hooks[2] = lambda: disp.request_stop(signal.SIGTERM)
        disp.sched.shutdown = lambda grace=0: None  # type: ignore[assignment]  # not our process: just stop looping
        with mock.patch.object(od.AdoptedProc, "poll", lambda self: None):  # pid 7777 is a stand-in
            code = disp.run()
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertEqual(self.spawned, [])  # the adopted child holds the only slot
        self.assertIn(name, disp.sched.children)


class AdmissionGateTests(unittest.TestCase):
    def test_runs_pool_functions_through_bash_and_fails_closed(self) -> None:
        seen: List[List[str]] = []

        def run(argv: List[str], **_kw: Any) -> Any:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0 if len(seen) == 1 else 1, "", "")

        self.assertTrue(od.pool_admission_open("/opt/tartci", run=run))
        self.assertFalse(od.pool_admission_open("/opt/tartci", run=run))
        script = seen[0][2]
        self.assertIn("providers/common/pool.lib.sh", script)
        self.assertIn("tartci_pool_admission_open", script)
        self.assertIn("tartci_pool_lock_absent", script)

        def boom(argv: List[str], **_kw: Any) -> Any:
            raise OSError("no bash")

        self.assertFalse(od.pool_admission_open("/opt/tartci", run=boom))


class ShutdownTests(SchedulerHarness):
    def test_shutdown_terminates_children_then_kills_the_group_after_the_grace(self) -> None:
        sched = self.sched()
        child = sched.start("o/r", "linux-a64")
        sched.shutdown(grace=3)
        pid = child.proc.pid
        self.assertEqual(self.sent[0], (pid, signal.SIGTERM))
        self.assertEqual(self.sent[1:], [(pid, ("group", signal.SIGKILL))])  # by process group, not pid
        self.assertGreaterEqual(self.now, 1003.0)  # it really waited out the grace first
        self.assertTrue(sched.stopping)

    def test_children_that_exit_inside_the_grace_are_never_killed(self) -> None:
        sched = self.sched()
        child = sched.start("o/r", "linux-a64")
        sleeps = {"n": 0}

        def sleep(seconds: float) -> None:
            sleeps["n"] += 1
            self.now += seconds
            if sleeps["n"] == 2:
                child.proc.code = 143

        sched.sleep = sleep
        sched.shutdown(grace=60)
        self.assertEqual(self.sent, [(child.proc.pid, signal.SIGTERM)])
        self.assertEqual(sched.children, {})
        self.assertLess(self.now, 1010.0)

    def test_grace_zero_goes_straight_to_the_group_kill(self) -> None:
        sched = self.sched()
        sched.start("o/r", "linux-a64")
        sched.shutdown(grace=0)
        self.assertEqual([entry[1] for entry in self.sent], [signal.SIGTERM, ("group", signal.SIGKILL)])


class ProcessGroupTests(unittest.TestCase):
    def test_kill_group_signals_the_group_and_never_our_own(self) -> None:
        proc = mock.Mock(pid=4321)
        proc.pgid = 4321
        with mock.patch.object(od.os, "killpg") as killpg, mock.patch.object(od.os, "kill") as kill:
            REAL_KILL_GROUP(proc, signal.SIGKILL)
            killpg.assert_called_once_with(4321, signal.SIGKILL)
            kill.assert_not_called()
        ours = mock.Mock(pid=os.getpid())
        ours.pgid = os.getpgrp()
        with mock.patch.object(od.os, "killpg") as killpg, mock.patch.object(od.os, "kill") as kill:
            REAL_KILL_GROUP(ours, signal.SIGKILL)
            killpg.assert_not_called()  # our own group is never signalled
            kill.assert_called_once()

    def test_a_real_child_tree_dies_with_the_final_kill(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            pidfile = os.path.join(root, "grandchild.pid")
            config = od.parse_config({"owners": ["o"]})

            def spawn(argv: List[str], env: Dict[str, str], log_path: str) -> Any:
                script = f"sleep 300 & echo $! > {pidfile}; wait"
                return od.spawn_process(["/bin/sh", "-c", script], {"PATH": "/usr/bin:/bin"}, log_path)

            sched = od.Scheduler(config, root, "/opt/tartci", spawn=spawn, admission=lambda: True, home=root,
                                 base_env={})
            child = sched.start("o/r", "linux-a64")
            self.addCleanup(lambda: REAL_KILL_GROUP(child.proc, signal.SIGKILL))
            deadline = time.time() + 10
            while not os.path.exists(pidfile) and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.1)
            grandchild = int(Path(pidfile).read_text().strip())
            os.kill(grandchild, 0)  # alive: the shell's own child, in the child's group
            # A SIGTERM to the shell alone would orphan it. grace=0 exercises SIGTERM + group kill.
            sched.shutdown(grace=0)
            self.assertEqual(sched.children, {})
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    os.kill(grandchild, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            with self.assertRaises(ProcessLookupError):
                os.kill(grandchild, 0)


class IdentityLocaleTests(unittest.TestCase):
    """M2: `ps -o lstart=` renders through the caller's locale and zone; identity must not."""

    C_RENDERING = "Sun Nov 22 20:10:42 2026"
    GB_RENDERING = "Sun 22 Nov 20:10:42 2026"

    def test_ps_runs_under_a_pinned_locale_and_zone_whatever_the_dispatcher_has(self) -> None:
        seen: List[Dict[str, str]] = []

        def fake_run(argv: List[str], **kw: Any) -> Any:
            seen.append(kw["env"])
            return subprocess.CompletedProcess(argv, 0, f"  {self.C_RENDERING}\n", "")

        for locale, zone in (("en_GB.UTF-8", "Asia/Tokyo"), ("fr_FR.UTF-8", "America/Los_Angeles"), ("C", "")):
            with mock.patch.dict(os.environ, {"LC_ALL": locale, "LANG": locale, "LC_TIME": locale, "TZ": zone}), \
                    mock.patch.object(od.subprocess, "run", fake_run):
                self.assertEqual(od.process_started_at(os.getpid()), self.C_RENDERING)
        self.assertEqual(len(seen), 3)
        for env in seen:
            self.assertEqual((env["LC_ALL"], env["LANG"]), ("C", "C"))
            self.assertNotIn("TZ", env)
            self.assertIn("PATH", env)  # still a working environment for ps

    def test_the_real_ps_answer_does_not_depend_on_the_callers_locale_or_zone(self) -> None:
        def under(locale: str, zone: str) -> Optional[str]:
            with mock.patch.dict(os.environ, {"LC_ALL": locale, "LANG": locale, "TZ": zone}):
                return od.process_started_at(os.getpid())

        first = under("en_GB.UTF-8", "Asia/Tokyo")
        if first is None:
            self.skipTest("ps -o lstart is not available here")
        self.assertEqual(under("de_DE.UTF-8", "America/Los_Angeles"), first)
        self.assertEqual(under("C", "UTC"), first)
        plain = {k: v for k, v in os.environ.items() if k not in ("TZ", "LC_ALL", "LANG", "LC_TIME")}
        plain["LC_ALL"] = "C"  # what a launchd-started runner.sh (no TZ, C locale) records
        direct = subprocess.run(["ps", "-p", str(os.getpid()), "-o", "lstart="], capture_output=True, text=True,
                                env=plain).stdout
        self.assertEqual(first, " ".join(direct.split()))

    def test_a_child_has_no_locale_or_zone_so_runner_sh_records_the_same_rendering(self) -> None:
        sched_env = od.child_env(od.parse_config({"owners": ["o"]}), "/r", od.LANES["linux-a64"], "tartci-owners-a1", 1,
                                 {"PATH": "/usr/bin", "LANG": "en_GB.UTF-8", "LC_ALL": "en_GB.UTF-8",
                                  "LC_TIME": "en_GB.UTF-8", "TZ": "Asia/Tokyo"}, "/home/u")
        self.assertEqual([k for k in sched_env if k in ("LANG", "TZ") or k.startswith("LC_")], [])

    def test_start_times_compare_across_renderings(self) -> None:
        self.assertTrue(od.same_start(self.GB_RENDERING, self.C_RENDERING))
        self.assertTrue(od.same_start(self.C_RENDERING, "Sun  Nov 22  20:10:42 2026"))  # whitespace only
        self.assertTrue(od.same_start("dim. 22 nov. 20:10:42 2026", self.C_RENDERING))  # another language's names
        for other in ("Sun Nov 22 20:10:43 2026", "Sun Nov 23 20:10:42 2026", "Sun Nov 22 20:10:42 2025", "",
                      "Thu Jan  1 00:00:00 1970"):
            self.assertFalse(od.same_start(self.GB_RENDERING, other), other)
        self.assertFalse(od.same_start("garbage", self.C_RENDERING))
        self.assertTrue(od.same_start("garbage", "garbage"))
        self.assertIsNone(od.start_key("20:10:42 2026"))  # no day: not a usable key

    def test_an_identity_recorded_under_another_locale_still_adopts_the_live_process(self) -> None:
        with mock.patch.object(od, "process_started_at", return_value=self.C_RENDERING), \
                mock.patch.object(od.os, "kill"):
            self.assertTrue(od.same_process(4242, self.GB_RENDERING))
            self.assertTrue(od.pid_alive(4242, self.GB_RENDERING))
            self.assertFalse(od.same_process(4242, "Sun 22 Nov 20:10:43 2026"))


class GhEnvTests(unittest.TestCase):
    """M3: the dispatcher's own gh calls run as the children's do."""

    TOKENS = {"GH_TOKEN": "ghp_SECRET_1", "GITHUB_TOKEN": "ghs_SECRET_2", "GH_ENTERPRISE_TOKEN": "ghe_SECRET_3",
              "GITHUB_ENTERPRISE_TOKEN": "ghe_SECRET_4"}
    BASE = {"PATH": "/usr/bin", "HOME": "/home/u", "GH_CONFIG_DIR": "/home/u/.config/gh", "TART_HOME": "/elsewhere"}

    def test_gh_env_drops_tokens_and_keeps_the_keyring_login_paths(self) -> None:
        env = od.gh_env(dict(self.BASE, **self.TOKENS))
        for name in self.TOKENS:
            self.assertNotIn(name, env)
        self.assertEqual(env, {"PATH": "/usr/bin", "HOME": "/home/u", "GH_CONFIG_DIR": "/home/u/.config/gh"})
        with mock.patch.dict(os.environ, self.TOKENS):
            self.assertFalse(set(self.TOKENS) & set(od.gh_env()))

    def test_gh_client_passes_its_env_and_inherits_none_by_accident(self) -> None:
        seen: List[Dict[str, Any]] = []

        def run(argv: List[str], **kw: Any) -> Any:
            seen.append(kw)
            return subprocess.CompletedProcess(argv, 0, "HTTP/2 200\r\n\r\n{}", "")

        scrubbed = od.gh_env(dict(self.BASE, **self.TOKENS))
        with mock.patch.dict(os.environ, self.TOKENS):
            od.GhClient("gh", run=run, env=scrubbed).get("user")
            od.GhClient("gh", run=run).get("user")
        self.assertEqual(seen[0]["env"], scrubbed)
        self.assertNotIn("GH_TOKEN", seen[0]["env"])
        self.assertNotIn("env", seen[1])  # a client built without one is the plain `subprocess.run` default

    def test_gh_auth_status_is_checked_under_the_scrubbed_env_and_reports_a_clear_error(self) -> None:
        calls: List[Tuple[List[str], Dict[str, str]]] = []

        def fake(rc: int, err: str) -> Callable[..., Any]:
            def run(argv: List[str], **kw: Any) -> Any:
                calls.append((argv, kw["env"]))
                return subprocess.CompletedProcess(argv, rc, "", err)
            return run

        env = od.gh_env(dict(self.BASE, **self.TOKENS))
        self.assertIsNone(od.check_gh_auth("gh", env, run=fake(0, "")))
        problem = od.check_gh_auth("/opt/gh", env, run=fake(1, "You are not logged into any GitHub hosts.\nmore"))
        self.assertIn("auth status", problem or "")
        self.assertIn("not logged into", problem or "")
        self.assertNotIn("more", problem or "")
        self.assertEqual(calls[1][0], ["/opt/gh", "auth", "status"])
        for _, seen in calls:
            self.assertFalse(set(self.TOKENS) & set(seen))

        def missing(argv: List[str], **kw: Any) -> Any:
            raise FileNotFoundError(2, "No such file")

        self.assertIn("cannot run", od.check_gh_auth("gh", env, run=missing) or "")

    def test_cmd_run_builds_the_client_from_the_scrubbed_env_and_logs_a_failed_auth_check(self) -> None:
        captured: Dict[str, Any] = {}
        logged: List[str] = []

        class StubDispatcher:
            def __init__(self, *args: Any, **kw: Any) -> None:
                pass

            def run(self) -> int:
                return 0

            def request_stop(self, signum: int) -> None:
                pass

        def fake_client(gh_cli: str = "gh", **kw: Any) -> FakeGh:
            captured["client_env"] = kw.get("env")
            return FakeGh({})

        def fake_auth(gh_cli: str, env: Dict[str, str], run: Any = None) -> str:
            captured["auth_env"] = env
            return "`gh auth status` failed without GH_TOKEN/GITHUB_TOKEN (rc=1): not logged in"

        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, self.TOKENS), \
                mock.patch.object(od, "load_config", return_value=od.parse_config({"owners": ["o"]})), \
                mock.patch.object(od, "GhClient", fake_client), mock.patch.object(od, "Dispatcher", StubDispatcher), \
                mock.patch.object(od, "check_gh_auth", fake_auth), mock.patch.object(od.signal, "signal"), \
                mock.patch.object(od, "log_line", logged.append):
            code = od.cmd_run(mock.Mock(config="x", root=os.path.join(root, "owners")))
        self.assertEqual(code, 0)
        for key in ("client_env", "auth_env"):
            self.assertFalse(set(self.TOKENS) & set(captured[key]), key)
            self.assertIn("HOME", captured[key])
        self.assertTrue(any("auth status" in line and "gh auth login" in line for line in logged), logged)

    def test_dry_run_also_uses_the_scrubbed_env(self) -> None:
        captured: Dict[str, Any] = {}

        def fake_client(gh_cli: str = "gh", **kw: Any) -> FakeGh:
            captured["env"] = kw.get("env")
            return FakeGh({})

        with mock.patch.dict(os.environ, self.TOKENS), \
                mock.patch.object(od, "load_config", return_value=od.parse_config({"owners": ["o"]})), \
                mock.patch.object(od, "GhClient", fake_client), mock.patch.object(od, "check_gh_auth", return_value=None), \
                mock.patch("builtins.print"):
            od.cmd_dry_run(mock.Mock(config="x", scans=1, json=False))
        self.assertFalse(set(self.TOKENS) & set(captured["env"]))

    def test_dry_run_prints_the_child_vm_size(self) -> None:
        cfg = od.parse_config({"owners": ["o"], "macos_vm_cores": 4, "macos_vm_mem_mb": 10240})
        for as_json in (False, True):
            out = io.StringIO()
            with mock.patch.object(od, "load_config", return_value=cfg), \
                    mock.patch.object(od, "GhClient", lambda gh_cli="gh", **kw: FakeGh({})), \
                    mock.patch.object(od, "check_gh_auth", return_value=None), contextlib.redirect_stdout(out):
                od.cmd_dry_run(mock.Mock(config="x", scans=1, json=as_json))
            if as_json:
                self.assertEqual(json.loads(out.getvalue())["vm_size"], cfg.vm_size_view())
            else:
                self.assertIn("child VM size: macos cores=4 mem_mb=10240; "
                              "linux cores=host profile mem_mb=derived from cores\n", out.getvalue())

    def test_tart_gets_a_working_scrubbed_environment(self) -> None:
        seen: List[Dict[str, str]] = []

        def fake_run(argv: List[str], **kw: Any) -> Any:
            seen.append(kw["env"])
            return subprocess.CompletedProcess(argv, 0, "[]", "")

        with mock.patch.dict(os.environ, dict(self.BASE, **self.TOKENS)), mock.patch.object(od.subprocess, "run", fake_run):
            od.tart_runner(["list"])
        env = seen[0]
        self.assertFalse(set(self.TOKENS) & set(env))
        self.assertEqual((env["PATH"], env["HOME"]), ("/usr/bin", "/home/u"))
        self.assertEqual(env["TART_HOME"], "/home/u/.tart")  # ~ follows the HOME the tart call runs with


class UnverifiedLiveChildTests(unittest.TestCase):
    """M5: a live pid with no recorded start time is kept, never deleted or signalled."""

    def reap(self, root: str, tart_calls: List[List[str]]) -> List[str]:
        def tart(argv: List[str]) -> str:
            tart_calls.append(argv)
            return "[]"
        return od.reap_leftovers(root, tart, born=lambda vm: None)  # the REAL pid_alive, as in production

    def make(self, root: str, name: str, **meta: Any) -> Path:
        directory = Path(root, "state", name)
        directory.mkdir(parents=True)
        (directory / od.CHILD_META).write_text(json.dumps({"name": name, "token": "t" * 24, **meta}))
        return directory

    def test_a_live_child_without_a_recorded_start_time_keeps_its_dir_and_is_never_signalled(self) -> None:
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        with tempfile.TemporaryDirectory() as root:
            live = self.make(root, "tartci-owners-a1", pid=sleeper.pid)  # identify() failed at spawn
            dead = self.make(root, "tartci-owners-a2", pid=2_999_999)  # no such process: an ordinary leftover
            ended = self.make(root, "tartci-owners-a3", pid=sleeper.pid, ended=True)
            calls: List[List[str]] = []
            with mock.patch.object(od.os, "kill", wraps=os.kill) as kill:
                self.reap(root, calls)
            self.assertTrue(live.exists())
            self.assertFalse(dead.exists())
            self.assertFalse(ended.exists())  # a retired record names no live child, whatever its pid is now
            self.assertEqual([c for c in kill.call_args_list if c.args[1] != 0], [])  # only liveness probes
            self.assertIsNone(sleeper.poll())
            self.assertEqual([c for c in calls if c[0] in ("stop", "delete")], [])

    def test_the_same_holds_when_the_state_file_is_gone_or_names_the_pid(self) -> None:
        sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        with tempfile.TemporaryDirectory() as root:
            live = self.make(root, "tartci-owners-a1", pid=sleeper.pid)
            Path(live, "vm-1.state.json").write_text(json.dumps({"vm": "vm-1", "supervisor_pid": str(sleeper.pid)}))
            self.reap(root, [])
            self.assertTrue(live.exists())
            self.assertTrue((live / "vm-1.state.json").exists())

    def test_pid_exists_counts_a_process_that_is_not_ours(self) -> None:
        with mock.patch.object(od.os, "kill", side_effect=PermissionError(1, "not permitted")):
            self.assertTrue(od._pid_exists(4242))
        with mock.patch.object(od.os, "kill", side_effect=ProcessLookupError(3, "no such process")):
            self.assertFalse(od._pid_exists(4242))
        for bad in (0, 1, -5, "", None, "abc"):
            self.assertFalse(od._pid_exists(bad), bad)


class RestartAssignmentTests(SchedulerHarness):
    """K2 and K4: what a restart, or a crash right after a spawn, may assume."""

    def test_an_adopted_assigned_child_keeps_the_age_of_its_assignment(self) -> None:
        name = "tartci-owners-a222222"
        directory = Path(self.root, "state", name)
        directory.mkdir(parents=True)
        (directory / od.CHILD_META).write_text(json.dumps(
            {"name": name, "lane": "linux-a64", "repo": "o/r", "slot": 1, "token": "t" * 24, "spawned_at": 100.0,
             "pid": 8888, "pid_started": fake_started(8888)}))
        state = directory / f"{name}.state.json"
        state.write_text(json.dumps({"vm": "linux-ephr-8888-1", "phase": "job-running", "supervisor_pid": "8888"}))
        os.utime(state, (400.0, 400.0))  # written long before this restart (now = 1000)
        sched = self.sched()
        adopted = sched.adopt(alive=lambda pid, started="": int(pid) == 8888)
        self.assertEqual([c.name for c in adopted], [name])
        self.assertEqual(adopted[0].assigned_at, 400.0)
        # a security drop detected right after the restart spares it (no fork evidence names it)
        self.assertEqual(sched.stop_unassigned("o/r", recent_since=self.now - od.SECURITY_ASSIGN_LAG_SECONDS), [])
        self.assertEqual(self.sent, [])

    def test_the_pid_is_on_disk_before_the_start_time_is_looked_up(self) -> None:
        sched = self.sched()
        seen: List[Dict[str, Any]] = []

        def identify(pid: Any) -> str:
            seen.extend(json.loads(p.read_text()) for p in Path(self.root, "state").glob("*/" + od.CHILD_META))
            return fake_started(pid)

        sched.identify = identify  # type: ignore[assignment]
        child = sched.start("o/r", "linux-a64")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].get("pid"), child.proc.pid)
        self.assertNotIn("pid_started", seen[0])


class PidTokenLocaleTests(unittest.TestCase):
    """K5: the macOS cap's pid token must not depend on the caller's locale or zone."""

    def test_the_token_is_the_same_under_any_locale_and_zone(self) -> None:
        lib = Path(__file__).resolve().parent.parent / "providers" / "tart-macos" / "macos-vm-cap.lib.sh"
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        tokens = set()
        for extra in ({"LC_ALL": "C"}, {"LANG": "en_GB.UTF-8", "LC_ALL": "en_GB.UTF-8"},
                      {"LANG": "de_DE.UTF-8", "LC_ALL": "de_DE.UTF-8", "TZ": "Asia/Tokyo"}):
            env = dict(os.environ, **extra)
            out = subprocess.run(["bash", "-c", f'source "{lib}"; tartci_pid_token {proc.pid}'],
                                 env=env, capture_output=True, text=True, check=True).stdout.strip()
            self.assertTrue(out)
            tokens.add(out)
        self.assertEqual(len(tokens), 1, tokens)


if __name__ == "__main__":
    unittest.main()
