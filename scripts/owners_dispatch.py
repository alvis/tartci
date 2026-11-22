#!/usr/bin/env python3
"""tartci owners: serve queued jobs from the private repositories of configured
owners on one host (see docs/owner-pool.md).

    tartci owners run       the dispatcher: scan, start `tartci serve --once` children
    tartci owners dry-run   print what would be served, start nothing, mint nothing
    tartci owners status    repositories, children, rate headroom, commit

The dispatcher decides *which repository and lane needs a runner next*. Everything
after that is tartci: lease admission, JIT registration, a throwaway VM, one job.
GitHub assigns jobs by label; this never assigns or cancels one.

Seams left for later changes: `Directory.refresh` / `Scanner.scan_repo` are the only
GitHub readers, so a push receiver can mark repositories for an immediate scan by
calling `Dispatcher.request_scan(repo)`; `install`/`uninstall` only need `main`'s
subcommand table.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import dataclasses
import datetime as dt
import glob
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- lanes

SELF_HOSTED = "self-hosted"


@dataclasses.dataclass(frozen=True)
class Lane:
    label: str
    os: str  # the `tartci serve <os>` argument
    rosetta: bool = False
    default_golden: str = ""


LANES: Dict[str, Lane] = {
    "macos-a64": Lane("macos-a64", "macos", False, "owners-macos"),
    "linux-a64": Lane("linux-a64", "linux", False, "owners-linux"),
    "linux-x64": Lane("linux-x64", "linux", True, "owners-linux"),
}

MACOS_HARD_MAX = 2  # Apple's two-guest limit; tartci enforces it again inside each child
DISCOVERY_SECONDS = 900
ASSIGN_LAG_SECONDS = 15  # GitHub's job list can trail the runner's own "running job"
# A security drop also stops children whose assignment was OBSERVED within this long before the
# drop was detected: such a child may have taken the very job that made the repository unsafe.
# Our view of an assignment trails GitHub's: the runner polls its log every 5 s for "Running job:"
# (providers/common/runner-assignment.lib.sh, poll=5 in the linux and macOS runners) before it
# writes `job-running`, the dispatcher reads that on its next 1 s tick, and the scan that detects
# the drop can itself take several seconds. 15 s = three polls, comfortably more than one
# poll + one tick + a scan.
SECURITY_ASSIGN_LAG_SECONDS = 15.0
SUSTAINED_FAILURE_SECONDS = 180
CHILD_TERM_GRACE_SECONDS = 150
MAX_RATE_WAIT_SECONDS = 3600.0  # the longest Retry-After / reset wait honoured
IDLE_EXIT_CODE = 124  # tartci's assignment (idle) timeout: no job was ever taken
IDLE_BACKOFF_SECONDS = 30.0  # fixed and gentle: an idle-out is not a denial
RETENTION_SECONDS = 7 * 86400  # ended children's state dirs and logs
RETENTION_KEEP = 100  # ...and at most this many of the newest
PRUNE_EVERY_SECONDS = 3600
RATE_FLOOR_FRACTION = 0.02  # stop calling GitHub below this share of the limit
RATE_WIDEN_BELOW = 0.25  # widen the poll interval below this headroom
RUN_STATUSES = ("pending", "queued", "in_progress")  # runs whose jobs are scanned for work to serve
# Runs whose head repository is checked: every run that is not finished. A fork's run waiting
# for approval (action_required) or a gate (waiting, requested) is as much evidence that the
# fork-pull-request setting is on as a running one, and approval would queue its jobs.
# `completed` runs are inert (a re-run flips the status back, which the next scan sees).
GUARD_STATUSES = RUN_STATUSES + ("action_required", "waiting", "requested")
RUNS_PER_PAGE = 100
# The child VM sizes owners.toml may set, per OS: (cores key, its variable), (memory key, its
# variable). The variables are the lanes' documented size overrides (providers/common/
# vm-lease.lib.sh). A key left unset puts nothing in the child's environment, so the lane sizes
# the VM as for any serve: cores from the host profile, memory derived from the granted cores.
VM_SIZE_KEYS: Dict[str, Tuple[Tuple[str, str], Tuple[str, str]]] = {
    "macos": (("macos_vm_cores", "TARTCI_MACOS_VM_CORES"), ("macos_vm_mem_mb", "TARTCI_MACOS_VM_MEM_MB")),
    "linux": (("linux_vm_cores", "TARTCI_LINUX_VM_CORES"), ("linux_vm_mem_mb", "TARTCI_LINUX_VM_MEM_MB")),
}
VM_CORES_RANGE = (1, 64)
VM_MEM_MB_RANGE = (2048, 524288)  # MB; refuses a size given in GB (10) or a core count by mistake
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


def lane_for_labels(labels: Iterable[str]) -> Optional[str]:
    """The lane a job's labels select, or None when no pool runner could take it.

    A pool runner carries `self-hosted` plus exactly one lane label, so a job is
    served only when its labels (case-insensitive) are a subset of
    {self-hosted, <lane>} for exactly one lane. A job that also asks for another
    label could never be taken by a pool runner: booting a VM for it would idle
    until the assignment timeout, every time.
    """
    wanted = {str(label).strip().lower() for label in labels if str(label).strip()}
    lanes = wanted & set(LANES)
    if len(lanes) != 1:
        return None
    lane = next(iter(lanes))
    if wanted - {SELF_HOSTED, lane}:
        return None
    return lane


# ------------------------------------------------------------------------- config


class ConfigError(ValueError):
    pass


@dataclasses.dataclass
class Config:
    owners: List[str]
    repos: List[str]
    exclude_repos: List[str]
    max_jobs: int = 4
    max_macos: int = MACOS_HARD_MAX
    poll_seconds: int = 10
    reconcile_seconds: int = 60  # validated now; read by the push layer (a later change)
    push: bool = True
    golden: Dict[str, str] = dataclasses.field(default_factory=dict)
    gh_cli: str = "gh"
    vm_size: Dict[str, int] = dataclasses.field(default_factory=dict)  # only the keys owners.toml sets

    def golden_for(self, lane: str) -> str:
        return self.golden.get(lane) or LANES[lane].default_golden

    def vm_env(self, os_name: str) -> Dict[str, str]:
        """The size variables a child of `os_name` gets: one per key owners.toml sets."""
        return {variable: str(self.vm_size[key]) for key, variable in VM_SIZE_KEYS[os_name]
                if key in self.vm_size}

    def vm_size_view(self) -> Dict[str, Dict[str, Optional[int]]]:
        """Per OS, the configured cores and mem_mb (None: the lane's default applies)."""
        return {os_name: {"cores": self.vm_size.get(cores_key), "mem_mb": self.vm_size.get(mem_key)}
                for os_name, ((cores_key, _), (mem_key, _)) in VM_SIZE_KEYS.items()}


def format_vm_size(view: Dict[str, Dict[str, Optional[int]]]) -> str:
    """One line for `status` and `dry-run`: what each OS's children request."""
    parts = []
    for os_name in VM_SIZE_KEYS:
        size = view.get(os_name) or {}
        cores = size.get("cores")
        mem_mb = size.get("mem_mb")
        parts.append(f"{os_name} cores={cores if cores else 'host profile'} "
                     f"mem_mb={mem_mb if mem_mb else 'derived from cores'}")
    return "child VM size: " + "; ".join(parts)


def _str_list(data: Dict[str, Any], key: str, pattern: "re.Pattern[str]") -> List[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be a list of strings")
    for item in value:
        if not pattern.match(item):
            raise ConfigError(f"{key}: invalid entry {item!r}")
    return list(dict.fromkeys(value))


def _int(data: Dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{key} must be an integer between {low} and {high}")
    return value


def _bool(data: Dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):  # "false" (a string) must not turn into True
        raise ConfigError(f"{key} must be true or false")
    return value


def parse_config(data: Dict[str, Any]) -> Config:
    known = {"owners", "repos", "exclude_repos", "max_jobs", "max_macos", "poll_seconds",
             "reconcile_seconds", "push", "golden", "gh_cli"}
    known.update(key for pairs in VM_SIZE_KEYS.values() for key, _ in pairs)
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown key(s): {', '.join(unknown)}")
    golden_raw = data.get("golden", {})
    if not isinstance(golden_raw, dict):
        raise ConfigError("[golden] must be a table")
    golden: Dict[str, str] = {}
    for lane, name in golden_raw.items():
        if lane not in LANES:
            raise ConfigError(f"[golden]: unknown lane {lane!r} (lanes: {', '.join(LANES)})")
        if not isinstance(name, str) or not name or not re.match(r"^[A-Za-z0-9._:/@-]+$", name):
            raise ConfigError(f"[golden] {lane}: invalid golden name")
        golden[lane] = name
    vm_size: Dict[str, int] = {}
    for (cores_key, _), (mem_key, _) in VM_SIZE_KEYS.values():
        for key, (low, high) in ((cores_key, VM_CORES_RANGE), (mem_key, VM_MEM_MB_RANGE)):
            if key in data:
                vm_size[key] = _int(data, key, low, low, high)
    cfg = Config(
        owners=_str_list(data, "owners", OWNER_RE),
        repos=_str_list(data, "repos", REPO_RE),
        exclude_repos=_str_list(data, "exclude_repos", REPO_RE),
        max_jobs=_int(data, "max_jobs", 4, 1, 16),
        max_macos=_int(data, "max_macos", MACOS_HARD_MAX, 0, MACOS_HARD_MAX),
        poll_seconds=_int(data, "poll_seconds", 10, 1, 3600),
        reconcile_seconds=_int(data, "reconcile_seconds", 60, 1, 3600),
        push=_bool(data, "push", True),
        golden=golden,
        gh_cli=str(data.get("gh_cli", "gh")),
        vm_size=vm_size,
    )
    if not cfg.owners and not cfg.repos:
        raise ConfigError("nothing to serve: list at least one owner or repo")
    return cfg


def default_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "tartci", "owners.toml")


def load_config(path: str) -> Config:
    try:
        import tomllib  # type: ignore[import-not-found]  # 3.11+; tartci_toml_exec provides it
    except ImportError as error:  # pragma: no cover - only under a bare 3.9
        raise ConfigError("reading owners.toml needs Python 3.11+ (run through `tartci owners`)") from error
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as error:
        raise ConfigError(f"config not found: {path}") from error
    except (OSError, ValueError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error
    return parse_config(data)


# ------------------------------------------------------------------ GitHub client


class GhError(Exception):
    def __init__(self, message: str, status: Optional[int] = None,
                 rate_limited: bool = False, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.status = status
        self.rate_limited = rate_limited
        self.retry_after = retry_after


@dataclasses.dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: Any
    from_cache: bool = False


@dataclasses.dataclass
class RateState:
    remaining: Optional[int] = None
    limit: Optional[int] = None
    reset: Optional[float] = None

    def headroom(self) -> Optional[float]:
        if self.remaining is None or not self.limit:
            return None
        return self.remaining / float(self.limit)

    def floor(self) -> int:
        return max(25, int((self.limit or 0) * RATE_FLOOR_FRACTION))


def adaptive_interval(base: float, rate: RateState, now: float) -> Tuple[float, str]:
    """The next reconcile interval: `base` while headroom is healthy, wider below
    25% of the limit, and a wait until the reset when the allowance is nearly spent
    (back off before the limit, never into it)."""
    if rate.remaining is None or not rate.limit:
        return float(base), ""
    if rate.remaining <= rate.floor():
        wait = (rate.reset - now) if rate.reset else 60.0
        return min(max(wait, float(base)), 900.0), "rate limit nearly spent; waiting for reset"
    headroom = rate.headroom() or 1.0
    if headroom < RATE_WIDEN_BELOW:
        factor = max(2.0, RATE_WIDEN_BELOW / max(headroom, 0.02))
        return min(base * factor, 300.0), f"rate headroom {headroom:.0%}; interval widened"
    return float(base), ""


_SPLIT = re.compile(r"\r?\n\r?\n")


def parse_gh_i(output: str) -> Tuple[int, Dict[str, str], str]:
    """Split `gh api -i` output into (status, lower-cased headers, body)."""
    pieces = _SPLIT.split(output, maxsplit=1)
    head = pieces[0]
    body = pieces[1] if len(pieces) > 1 else ""
    lines = head.splitlines()
    status = 0
    if lines:
        parts = lines[0].split()
        if len(parts) >= 2 and parts[1].isdigit():
            status = int(parts[1])
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
    return status, headers, body


class GhClient:
    """`gh api` with conditional requests, rate tracking and a floor.

    The token never leaves `gh`; nothing here prints a header or body other than
    GitHub's own `message` on failure."""

    def __init__(self, gh_cli: str = "gh", timeout: float = 60.0,
                 run: Callable[..., Any] = subprocess.run,
                 clock: Callable[[], float] = time.time,
                 env: Optional[Dict[str, str]] = None) -> None:
        self.gh_cli = gh_cli
        self.timeout = timeout
        self.env = env  # None: inherit ours; the dispatcher passes gh_env() (no tokens, as the children)
        self._run = run
        self._clock = clock
        self._lock = threading.Lock()
        self._etags: Dict[str, Tuple[str, Any]] = {}
        self._blocked_until = 0.0  # GitHub told us to wait (Retry-After / reset)
        self.rate = RateState()
        self.requests = 0
        self.not_modified = 0

    # Fakes override only this.
    def get(self, path: str, conditional: bool = False) -> Response:
        floor_wait = 0.0
        with self._lock:
            now = self._clock()
            if (self.rate.remaining is not None and self.rate.limit
                    and self.rate.remaining <= self.rate.floor()
                    and (self.rate.reset or 0) > now):
                floor_wait = max(0.0, (self.rate.reset or now) - now)
            floor_wait = max(floor_wait, self._blocked_until - now)
            known = self._etags.get(path) if conditional else None
        if floor_wait > 0:
            raise GhError("GitHub asked us to wait (or the allowance is nearly spent); not calling it",
                          status=403, rate_limited=True, retry_after=min(floor_wait, MAX_RATE_WAIT_SECONDS))
        argv = [self.gh_cli, "api", "-i", path]
        if known:
            argv += ["-H", f"If-None-Match: {known[0]}"]
        try:
            extra = {} if self.env is None else {"env": self.env}
            proc = self._run(argv, capture_output=True, text=True, timeout=self.timeout, **extra)
        except subprocess.TimeoutExpired as error:
            raise GhError(f"gh api timed out after {self.timeout:.0f}s ({path})") from error
        except OSError as error:
            raise GhError(f"cannot run {self.gh_cli}: {error}") from error
        status, headers, body = parse_gh_i(proc.stdout or "")
        with self._lock:
            self.requests += 1
            self._note_rate(headers)
        if status == 304 and known:
            with self._lock:
                self.not_modified += 1
            return Response(200, headers, known[1], from_cache=True)
        if status == 0:
            detail = (proc.stderr or "").strip().splitlines()[-1:] or ["no response"]
            raise GhError(f"gh api failed ({path}): {detail[0][:200]}")
        try:
            payload = json.loads(body) if body.strip() else None
        except ValueError:
            payload = None
        if status >= 400:
            message = payload.get("message", "") if isinstance(payload, dict) else ""
            lowered = message.lower()
            limited = status in (403, 429) and (
                "rate limit" in lowered or headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in headers)
            wait = 0.0
            if limited:
                now = self._clock()
                wait = _float(headers.get("retry-after"), 0.0)
                reset = _float(headers.get("x-ratelimit-reset"), 0.0)
                if wait <= 0 and headers.get("x-ratelimit-remaining") == "0" and reset > now:
                    wait = reset - now
                wait = min(wait if wait > 0 else 60.0, MAX_RATE_WAIT_SECONDS)
                with self._lock:
                    self._blocked_until = max(self._blocked_until, now + wait)
            raise GhError(f"HTTP {status} on {path}: {message[:200]}", status=status,
                          rate_limited=limited, retry_after=wait)
        if conditional and "etag" in headers:
            with self._lock:
                if len(self._etags) > 4096:
                    self._etags.clear()
                self._etags[path] = (headers["etag"], payload)
        return Response(status, headers, payload)

    def _note_rate(self, headers: Dict[str, str]) -> None:
        remaining = headers.get("x-ratelimit-remaining")
        limit = headers.get("x-ratelimit-limit")
        reset = headers.get("x-ratelimit-reset")
        resource = headers.get("x-ratelimit-resource", "core")
        if resource != "core":
            return
        if remaining is not None and remaining.isdigit():
            self.rate.remaining = int(remaining)
        if limit is not None and limit.isdigit():
            self.rate.limit = int(limit)
        if reset is not None and reset.isdigit():
            self.rate.reset = float(reset)

    def pages(self, path: str, key: Optional[str] = None, max_pages: int = 30) -> List[Any]:
        """Every item of a paginated listing (unconditional: used at discovery rate)."""
        items: List[Any] = []
        sep = "&" if "?" in path else "?"
        for page in range(1, max_pages + 1):
            body = self.get(f"{path}{sep}per_page=100&page={page}").body
            chunk = body.get(key, []) if key and isinstance(body, dict) else body
            if not isinstance(chunk, list):
                raise GhError(f"unexpected listing shape for {path}")
            items.extend(chunk)
            if len(chunk) < 100:
                return items
        raise GhError(f"listing {path} exceeds {max_pages} pages")


def _float(value: Optional[str], default: float) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


# ------------------------------------------------------------------ repositories


@dataclasses.dataclass(frozen=True)
class Repo:
    full_name: str
    owner: str


@dataclasses.dataclass
class RepoSet:
    served: Dict[str, Repo] = dataclasses.field(default_factory=dict)
    refused: Dict[str, str] = dataclasses.field(default_factory=dict)  # full name -> reason
    refused_security: set = dataclasses.field(default_factory=set)  # keys of `refused` that are security refusals
    errors: Dict[str, str] = dataclasses.field(default_factory=dict)  # owner/repo -> listing error
    rate_limited: set = dataclasses.field(default_factory=set)  # keys of `errors` caused by a rate limit
    retry_after: float = 0.0  # the longest wait GitHub asked for during this listing
    fetched_at: float = 0.0


def refusal(repo: Dict[str, Any], need_admin: bool = True) -> Optional[Tuple[str, bool]]:
    """(why a repository is not served, whether that is a SECURITY refusal), or None.
    `visibility == "private"` is the test, not the `private` flag, which is also true
    for internal repositories. Only "not private" is a security matter: runners still
    waiting for the repository must be stopped at once, because the repository may now
    run untrusted code. Archived, disabled and lost admin rights only mean it should
    not get new runners; the waiting ones expire on their own."""
    visibility = str(repo.get("visibility") or ("private" if repo.get("private") is True else "public"))
    if visibility != "private":
        return f"not private (visibility={visibility})", True
    if repo.get("archived"):
        return "archived", False
    if repo.get("disabled"):
        return "disabled", False
    if need_admin:
        permissions = repo.get("permissions")
        if not isinstance(permissions, dict) or permissions.get("admin") is not True:
            return "no admin rights (cannot mint a JIT runner config)", False
    return None


def refusal_reason(repo: Dict[str, Any], need_admin: bool = True) -> Optional[str]:
    found = refusal(repo, need_admin)
    return found[0] if found else None


def _transient(error: GhError) -> bool:
    """A failure that says nothing about the repository itself: rate limit, outage, timeout."""
    return error.rate_limited or error.status is None or error.status == 0 or error.status >= 500


class Directory:
    """The served repository set: private, non-archived repositories of the
    configured owners plus explicit extras, minus exclusions."""

    def __init__(self, client: Any, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg
        self.current = RepoSet()
        self._me: Optional[str] = None

    def me(self) -> str:
        if self._me is None:
            body = self.client.get("user").body
            login = body.get("login") if isinstance(body, dict) else None
            if not login:
                raise GhError("cannot determine the authenticated GitHub login")
            self._me = str(login)
        return self._me

    def _owner_repos(self, owner: str) -> List[Dict[str, Any]]:
        info = self.client.get(f"users/{owner}").body
        kind = info.get("type") if isinstance(info, dict) else None
        if kind == "Organization":
            return self.client.pages(f"orgs/{owner}/repos?type=private")
        if kind == "User":
            if owner.lower() != self.me().lower():
                raise GhError(f"private repositories of user {owner} are not listable by {self.me()}")
            listing = self.client.pages("user/repos?affiliation=owner&visibility=private")
            return [r for r in listing
                    if str((r.get("owner") or {}).get("login", "")).lower() == owner.lower()]
        raise GhError(f"unknown owner type for {owner}")

    def _fork_setting_ok(self, full: str) -> Optional[str]:
        """None when "Run workflows from fork pull requests" is off, else why not.
        Raises GhError when the setting cannot be read. (This endpoint was verified
        live to answer 200 for a personal private repository, so a 403/404 here means
        a real access problem, not "personal repositories have no such setting".)"""
        body = self.client.get(f"repos/{full}/actions/permissions/fork-pr-workflows-private-repos").body
        if not isinstance(body, dict) or body.get("run_workflows_from_fork_pull_requests") is not False:
            return "\"Run workflows from fork pull requests\" is on (or unreadable)"
        return None

    @staticmethod
    def _note_error(new: RepoSet, key: str, error: GhError) -> None:
        new.errors[key] = str(error)
        if error.rate_limited:
            new.rate_limited.add(key)
            new.retry_after = max(new.retry_after, error.retry_after or 60.0)

    def drop(self, full: str, reason: str, security: bool = True) -> None:
        """Stop serving `full` until the next discovery re-checks it. `security` says
        the reason is one that must also stop its waiting runners (the default: an
        unknown reason is treated as one)."""
        self.current.served.pop(full, None)
        self.current.refused[full] = reason
        (self.current.refused_security.add if security else self.current.refused_security.discard)(full)

    def refresh(self) -> RepoSet:
        """Re-list everything. An owner whose listing fails keeps its previous
        entries (visibility is rechecked on every scan anyway) and is named in
        `errors`; it is never treated as an empty owner."""
        previous = self.current
        new = RepoSet(fetched_at=time.time())
        excluded = {name.lower() for name in self.cfg.exclude_repos}
        candidates: Dict[str, Dict[str, Any]] = {}
        for owner in self.cfg.owners:
            try:
                for item in self._owner_repos(owner):
                    candidates[str(item.get("full_name", "")).lower()] = item
            except GhError as error:
                self._note_error(new, owner, error)
                for full, repo in previous.served.items():
                    if repo.owner.lower() == owner.lower():
                        new.served[full] = repo
        for full in self.cfg.repos:
            if full.lower() in candidates:
                continue
            try:
                body = self.client.get(f"repos/{full}").body
            except GhError as error:
                if _transient(error):
                    self._note_error(new, full, error)
                    if full in previous.served:  # a blip never unserves a repository
                        new.served[full] = previous.served[full]
                        continue
                new.refused[full] = f"cannot read repository ({error})"
                continue
            candidates[full.lower()] = body if isinstance(body, dict) else {}
        for key, item in sorted(candidates.items()):
            full = str(item.get("full_name") or key)
            if key in excluded:
                continue
            found = refusal(item)
            reason, security = found if found else (None, False)
            if reason is None:
                try:
                    reason = self._fork_setting_ok(full)
                    security = reason is not None  # on, or unreadable-as-on: fork runs may reach a runner
                except GhError as error:
                    if _transient(error):
                        self._note_error(new, full, error)
                        if full in previous.served:  # keep serving; read the setting again next time
                            new.served[full] = previous.served[full]
                            continue
                    reason = f"cannot read the fork-pull-request setting ({error})"
                    security = False  # a 403/404 reading it is an access problem, not evidence it is on
            if reason is not None:
                new.refused[full] = reason
                if security:
                    new.refused_security.add(full)
                new.served.pop(full, None)
                continue
            new.served[full] = Repo(full, str(full.split("/")[0]))
        self.current = new
        return new


# ----------------------------------------------------------------------- scanning


@dataclasses.dataclass(frozen=True)
class QueuedJob:
    repo: str
    run_id: int
    job_id: int
    name: str
    labels: Tuple[str, ...]
    lane: Optional[str]
    created_at: str
    url: str = ""


@dataclasses.dataclass
class RepoScan:
    repo: str
    jobs: List[QueuedJob] = dataclasses.field(default_factory=list)  # served: one lane each
    skipped: List[QueuedJob] = dataclasses.field(default_factory=list)  # queued, labels not served
    error: Optional[str] = None  # a failed scan is an error, never "0 queued"
    rate_limited: bool = False  # `error` is a GitHub rate limit, not unreachability
    retry_after: float = 0.0  # seconds GitHub asked us to wait
    dropped: Optional[str] = None  # repository no longer eligible (turned public, fork-headed run, archived...)
    dropped_security: bool = False  # ...and it is a security matter: stop its waiting runners now
    # A fork-headed run: the runner registrations its jobs were given to (any may be a child
    # of ours that already took a fork job), or `fork_jobs_unreadable` when they could not be read.
    fork_runners: List[str] = dataclasses.field(default_factory=list)
    fork_jobs_unreadable: bool = False
    # A fork-run job that is in_progress but names no runner yet (GitHub fills `runner_name` a
    # moment after it assigns): it may be on ANY child, so every child of the repository stops.
    fork_unknown_runner: bool = False
    # A fork-run job still queued (no runner yet): it may be taken at any moment. Informational;
    # the guard re-reads it every cycle for as long as the repository has children.
    fork_pending: bool = False
    runs_checked: bool = False  # the repository's runs were read for a fork head (the guard has run)


MAX_FORK_RUNS_READ = 10  # fork-headed runs whose jobs are read; beyond that, fail safe


class Scanner:
    """Per-repository scan following scripts/queue_scan.py's rules: runs that are
    pending, queued or in_progress; the queued jobs inside them."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._clean_jobs: Dict[Tuple[str, int], Tuple[str, List[Dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def _runs(self, full: str) -> List[Dict[str, Any]]:
        """Every run of the newest page, whatever its status (the caller serves the
        active ones but checks all of them for a fork head). A full page can bury an
        old active run, so each non-terminal status is also asked for directly."""
        base = f"repos/{full}/actions/runs?per_page={RUNS_PER_PAGE}"
        body = self.client.get(base, conditional=True).body
        runs = body.get("workflow_runs", []) if isinstance(body, dict) else None
        if not isinstance(runs, list):
            raise GhError(f"workflow_runs is not a list for {full}")
        found: Dict[int, Dict[str, Any]] = {}
        loose: List[Dict[str, Any]] = []
        for run in runs:
            if isinstance(run, dict):
                (found.__setitem__(run["id"], run) if isinstance(run.get("id"), int) else loose.append(run))
        if len(runs) < RUNS_PER_PAGE:
            return list(found.values()) + loose
        for status in GUARD_STATUSES:
            page = self.client.get(f"{base}&status={status}", conditional=True).body
            for run in (page.get("workflow_runs", []) if isinstance(page, dict) else []):
                if isinstance(run, dict) and isinstance(run.get("id"), int):
                    found[run["id"]] = run
        return list(found.values()) + loose

    def _list_jobs(self, full: str, run_id: int) -> List[Dict[str, Any]]:
        """Every job of a run's latest attempt, read fresh (never from the cache)."""
        jobs: List[Dict[str, Any]] = []
        for page in range(1, 4):
            body = self.client.get(
                f"repos/{full}/actions/runs/{run_id}/jobs?filter=latest&per_page=100&page={page}").body
            chunk = body.get("jobs", []) if isinstance(body, dict) else None
            if not isinstance(chunk, list):
                raise GhError(f"jobs is not a list for run {run_id}")
            jobs.extend(j for j in chunk if isinstance(j, dict))
            if len(chunk) < 100:
                break
        return jobs

    def _prune(self, full: str, active: set) -> None:
        """Forget cached job lists of runs that are no longer active."""
        with self._lock:
            for key in [k for k in self._clean_jobs if k[0] == full and k[1] not in active]:
                del self._clean_jobs[key]
            if len(self._clean_jobs) > 4096:
                self._clean_jobs.clear()

    def _jobs(self, full: str, run: Dict[str, Any]) -> List[Dict[str, Any]]:
        run_id = int(run["id"])
        stamp = f"{run.get('updated_at')}|{run.get('status')}|{run.get('run_attempt')}"
        with self._lock:
            cached = self._clean_jobs.get((full, run_id))
        if cached and cached[0] == stamp:
            return cached[1]
        jobs = self._list_jobs(full, run_id)
        # Only a run with nothing waiting is safe to reuse: a queued job's status
        # must be read fresh every scan.
        if not any(str(j.get("status", "")).lower() in ("queued", "waiting", "pending") for j in jobs):
            with self._lock:
                self._clean_jobs[(full, run_id)] = (stamp, jobs)
        return jobs

    def _read_fork_runners(self, full: str, fork_runs: List[Dict[str, Any]], result: RepoScan) -> None:
        """Which runners were given the jobs of fork-headed runs. Fails safe: when any
        listing fails (or there are too many runs to read), `fork_jobs_unreadable` makes
        the dispatcher stop EVERY child of the repository, assigned or not."""
        if len(fork_runs) > MAX_FORK_RUNS_READ:
            result.fork_jobs_unreadable = True
            return
        for run in fork_runs:
            try:
                jobs = self._list_jobs(full, int(run["id"]))
            except (GhError, KeyError, TypeError, ValueError):
                result.fork_jobs_unreadable = True
                return
            for job in jobs:
                runner = str(job.get("runner_name") or "").strip()
                if runner:
                    if runner not in result.fork_runners:
                        result.fork_runners.append(runner)
                    continue
                status = str(job.get("status", "")).lower()
                if status == "in_progress":
                    result.fork_unknown_runner = True  # running somewhere we cannot name
                elif status in ("queued", "waiting", "pending"):
                    result.fork_pending = True  # not taken yet: read again next cycle

    def _check_runs(self, full: str, result: RepoScan) -> Optional[List[Dict[str, Any]]]:
        """The fork-head guard, shared by `scan_repo` and `guard_repo`: read the repository's
        runs and look for one that is public or comes from a fork. Fills `result` (dropped,
        fork_*) and returns None when there is such a run; otherwise the runs to serve."""
        all_runs = self._runs(full)
        result.runs_checked = True
        runs = [r for r in all_runs if r.get("status") in RUN_STATUSES]
        self._prune(full, {int(r["id"]) for r in runs if isinstance(r.get("id"), int)})
        fork_runs: List[Dict[str, Any]] = []
        public = False
        for run in all_runs:
            if run.get("status") == "completed":
                continue  # inert; a re-run flips the status back and is checked then
            repository = run.get("repository") or {}
            head = run.get("head_repository") or {}
            if repository.get("private") is False:
                public = True  # keep looking: a public repository is exactly where fork runs land
            head_name = str(head.get("full_name", "")).lower()
            if not head_name or head_name != str(repository.get("full_name", full)).lower():
                fork_runs.append(run)
        if public and not fork_runs:
            result.dropped, result.dropped_security = "run reports a public repository", True
            result.jobs, result.skipped = [], []
            return None
        if fork_runs:
            # A fork-headed (or head-less) run is only possible while "Run workflows
            # from fork pull requests" is on, and GitHub gives any matching queued job
            # to any runner of the repository, so a runner started for another job
            # could take it, and one that is already busy may have. Refuse the repository
            # (do not just skip the run), and find which runners hold its jobs.
            result.dropped = (f"run {fork_runs[0].get('id')} comes from a fork or an unknown head repository "
                              "(the fork-pull-request setting is on)")
            result.dropped_security = True
            result.jobs, result.skipped = [], []
            self._read_fork_runners(full, fork_runs, result)
            return None
        return runs

    def guard_repo(self, full: str) -> RepoScan:
        """The fork-head guard alone, for a repository that is NOT served but still has
        children (refused or dropped for any reason, children not yet gone). No jobs are
        listed and nothing is ever started. `dropped_security` means evidence exists NOW
        that the repository is public or has a fork-headed run; `fork_runners` and friends
        say which children hold such a job."""
        result = RepoScan(full)
        try:
            meta = self.client.get(f"repos/{full}", conditional=True).body
            found = refusal(meta if isinstance(meta, dict) else {}, need_admin=False)
            if found is not None and found[1]:
                result.dropped, result.dropped_security = found
            self._check_runs(full, result)  # a fork run's reason replaces the visibility one
        except GhError as error:
            result.error = str(error)
            result.rate_limited = error.rate_limited
            result.retry_after = error.retry_after
        except Exception as error:
            result.error = f"{type(error).__name__}: {error}"
        return result

    def scan_repo(self, full: str) -> RepoScan:
        result = RepoScan(full)
        try:
            # Visibility is rechecked on every scan (a conditional GET is a free 304
            # when nothing changed): a repository that turned public is dropped now.
            meta = self.client.get(f"repos/{full}", conditional=True).body
            found = refusal(meta if isinstance(meta, dict) else {}, need_admin=False)
            if found is not None:
                result.dropped, result.dropped_security = found
                return result
            runs = self._check_runs(full, result)
            if runs is None:
                return result
            for run in runs:
                for job in self._jobs(full, run):
                    if str(job.get("status", "")).lower() != "queued":
                        continue
                    labels = tuple(str(label).lower() for label in (job.get("labels") or []) if str(label))
                    queued = QueuedJob(
                        repo=full, run_id=int(run["id"]), job_id=int(job.get("id") or 0),
                        name=str(job.get("name", "")), labels=labels, lane=lane_for_labels(labels),
                        created_at=str(job.get("created_at") or run.get("created_at") or ""),
                        url=str(job.get("html_url", "")))
                    (result.jobs if queued.lane else result.skipped).append(queued)
        except GhError as error:
            result.error = str(error)
            result.rate_limited = error.rate_limited
            result.retry_after = error.retry_after
            result.jobs, result.skipped = [], []
        except Exception as error:  # a scan that cannot be read is an error, never "0 queued"
            result.error = f"{type(error).__name__}: {error}"
            result.jobs, result.skipped = [], []
        return result


def scan_all(scanner: Scanner, repos: Sequence[str], workers: int = 8,
             scan: Optional[Callable[[str], RepoScan]] = None) -> Dict[str, RepoScan]:
    """`scan` (default `scanner.scan_repo`) over every repository; `scanner.guard_repo`
    for the repositories that are no longer served."""
    scan = scan or scanner.scan_repo
    if workers <= 1 or len(repos) <= 1:
        return {full: scan(full) for full in repos}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(zip(repos, pool.map(scan, repos)))


# -------------------------------------------------------------- host admission gates


def pool_admission_open(tartci_root: str, run: Callable[..., Any] = subprocess.run) -> bool:
    """tartci's own two gates, sourced from providers/common/pool.lib.sh: pool
    participation/state, and the transition lock. Fail closed on any error."""
    script = ('source "$1/providers/common/pool.lib.sh" && '
              "tartci_pool_admission_open && tartci_pool_lock_absent")
    try:
        proc = run(["bash", "-c", script, "owners-admission", tartci_root],
                   capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# ------------------------------------------------------------------------ children

TERMINAL_STATE_FILE = ".state.json"
CHILD_META = "owners-child.json"  # written at spawn into the child's state dir (identity, slot, token)
CHILD_PREFIX = "tartci-owners-"
LANE_LETTER = {"macos-a64": "m", "linux-a64": "a", "linux-x64": "x"}
LETTER_LANE = {letter: label for label, letter in LANE_LETTER.items()}

# What a child inherits from the operator's environment is an ALLOWLIST. The lanes
# read dozens of TARTCI_*/PULP_* variables that change which jobs they serve, which
# host paths they mount into guests, which credentials they use and how they size
# VMs (a stray TARTCI_JIT_GH_CLI, TARTCI_ACTIVE_VM_LEASE_ID, TARTCI_HOST_CI_CACHE...);
# a denylist would miss the next one. The dispatcher sets every variable a child
# needs itself (child_env). Tokens never pass: children authenticate through the
# `gh` login, found via HOME / GH_CONFIG_DIR / XDG_CONFIG_HOME, as the dispatcher
# does. SSH_AUTH_SOCK and proxy settings are deliberately not forwarded.
#
# LANG, LC_* and TZ are deliberately NOT forwarded either. runner.sh records its own
# `supervisor_pid_started_at` from `ps -o lstart=`, which renders through the caller's locale
# and time zone (en_GB: `Sun 22 Nov 20:10:42 2026`, C: `Sun Nov 22 20:10:42 2026`); the
# dispatcher compares that record against its own `ps` (STABLE_TIME_ENV: C, no TZ), so a child
# must render in C too. A child with no locale variables does, exactly like a launchd-started
# lane. Pinning `LC_ALL=C` in the child would also work for `ps`, but the lanes' ssh uses the
# system ssh_config, whose `SendEnv LANG LC_*` (macOS default) would carry it into the guest
# and run every job in the C locale; leaving the variables unset sends nothing.
ENV_ALLOW_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM",
    "GH_CONFIG_DIR", "XDG_CONFIG_HOME",
    "TARTCI_MACOS_VM_CAP",  # host-wide cap; the dispatcher also honours it
})
FETCHCONTENT_ENV = "PULP_SHARED_FETCHCONTENT_SOURCE_DIR"  # providers/tart-macos/runner.sh
# What `ps -o lstart` is read under (mirrors scripts/leases.py `_STABLE_TIME_ENV`): the C
# locale and the system time zone (TZ unset), which is what a child started this way renders in.
STABLE_TIME_ENV: Dict[str, Optional[str]] = {"LC_ALL": "C", "LANG": "C", "TZ": None}


def scrub_env(base_env: Dict[str, str]) -> Dict[str, str]:
    """Only the allowlisted variables of `base_env`."""
    return {key: value for key, value in base_env.items() if key in ENV_ALLOW_NAMES}


def stable_time_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """`base_env` (default: ours) with the locale and time zone pinned for `ps -o lstart`."""
    env = dict(os.environ if base_env is None else base_env)
    for key, value in STABLE_TIME_ENV.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def gh_env(base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment of the dispatcher's own `gh` calls: the children's (scrub_env), so a
    GH_TOKEN/GITHUB_TOKEN in the operator's shell cannot make discovery work where the
    children, which have no token, would fail to mint a JIT config forever. `gh` finds the
    keyring login through HOME / GH_CONFIG_DIR / XDG_CONFIG_HOME, as it does for them."""
    return scrub_env(dict(os.environ if base_env is None else base_env))


def check_gh_auth(gh_cli: str, env: Dict[str, str],
                  run: Callable[..., Any] = subprocess.run) -> Optional[str]:
    """None when `gh auth status` succeeds under `env` (the scrubbed one), else a one-line
    reason. The children authenticate the same way, so this failing means every JIT mint
    would fail too."""
    try:
        proc = run([gh_cli, "auth", "status"], capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        return f"cannot run `{gh_cli} auth status`: {error}"
    if proc.returncode == 0:
        return None
    detail = ((proc.stderr or proc.stdout or "").strip().splitlines() or ["no output"])[0][:200]
    return f"`{gh_cli} auth status` failed without GH_TOKEN/GITHUB_TOKEN (rc={proc.returncode}): {detail}"


def child_env(cfg: Config, root: str, lane: Lane, name: str, slot: int,
              base_env: Dict[str, str], home: str) -> Dict[str, str]:
    """Environment for one `tartci serve --once` child: the allowlisted part of
    `base_env` (scrub_env) plus everything the child needs, set here. Unique identity
    per child (name, slot, lane id, state dir); explicit state/cache/log dirs, and a
    pool-owned fetchcontent dir on macOS, so no lane falls back to a host path
    (`$HOME/Library/Caches/Pulp/...`) that it would mount into a guest; the pool's
    one VM key; the VM size owners.toml sets, if any (otherwise the lane sizes the VM)."""
    state_dir = os.path.join(root, "state", name)
    env = scrub_env(base_env)
    env.update({
        "TART_HOME": os.path.join(home, ".tart"),
        "TARTCI_VM_SSH_KEY": os.path.join(home, ".config", "tartci", "owners-vm-ed25519"),
        "TARTCI_RUNNER_NAME": name,
        "TARTCI_RUNNER_SLOT": str(slot),
        "TARTCI_QUEUE_LANE_ID": name,
        "TARTCI_RUNNER_IDLE_TIMEOUT_SECS": "120",
        "TARTCI_STATE_DIR": state_dir,
        "TARTCI_MACOS_STATE_DIR": state_dir,
        "TARTCI_LINUX_STATE_DIR": state_dir,
        "TARTCI_CI_CACHE": os.path.join(root, "cache"),
        "TARTCI_MACOS_LOGS": os.path.join(root, "logs", "macos"),
        "TARTCI_LINUX_LOGS": os.path.join(root, "logs", "linux"),
        "TARTCI_SHARED_QUEUE_CACHE": os.path.join(root, "state", "queue-discovery.json"),
        "TARTCI_GH_CLI": cfg.gh_cli,
    })
    env.update(cfg.vm_env(lane.os))
    if lane.os == "macos":
        env[FETCHCONTENT_ENV] = os.path.join(root, "fetchcontent-src")
    else:
        env["TARTCI_LINUX_ROSETTA"] = "1" if lane.rosetta else "0"
    return env


def child_argv(tartci: str, cfg: Config, lane: Lane, repo: str) -> List[str]:
    return [tartci, "serve", lane.os, "--once", "--repo", repo,
            "--labels", f"{SELF_HOSTED},{lane.label}", "--golden", cfg.golden_for(lane.label)]


def ensure_private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def write_private_json(path: str, data: Any) -> None:
    """Atomic write of a 0600 file (created with that mode: never world-readable)."""
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True)
    os.replace(tmp, path)


def read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _state_mtime(state_dir: str) -> Optional[float]:
    """The latest mtime of the lane state files in `state_dir`, or None."""
    times = []
    for path in glob.glob(os.path.join(state_dir, "*" + TERMINAL_STATE_FILE)):
        try:
            times.append(os.path.getmtime(path))
        except OSError:
            continue
    return max(times) if times else None


def read_state_dir(state_dir: str) -> Dict[str, Any]:
    """The newest runner state file under a child's own state dir ({} when none)."""
    newest: Tuple[float, Dict[str, Any]] = (-1.0, {})
    for path in glob.glob(os.path.join(state_dir, "*" + TERMINAL_STATE_FILE)):
        try:
            mtime = os.path.getmtime(path)
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and mtime > newest[0]:
            newest = (mtime, data)
    return newest[1]


def process_started_at(pid: Any) -> Optional[str]:
    """When `pid` started, in the form runner.sh records as `supervisor_pid_started_at`
    (`ps -o lstart=`, whitespace-normalised). None when the process is gone or ps
    cannot say: a caller that needs identity must then treat it as unverified."""
    try:
        number = int(pid)
        if number <= 1:
            return None
        out = subprocess.run(["ps", "-p", str(number), "-o", "lstart="], capture_output=True, text=True,
                             timeout=10, env=stable_time_env()).stdout
    except (TypeError, ValueError, OSError, subprocess.SubprocessError):
        return None
    return " ".join(out.split()) or None


_TIME_OF_DAY = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")


def start_key(started_at: str) -> Optional[Tuple[str, str, str]]:
    """(time of day, day of month, year) of an `lstart` string in ANY rendering (`Sun Nov 22
    20:10:42 2026`, `Sun 22 Nov 20:10:42 2026`, ...), or None when it has no such parts. The
    weekday and month names are left out, as they are what the locale changes; two different
    starts of one pid agreeing on all three would have to be a month apart to the second."""
    words = started_at.split()
    clock = [w for w in words if _TIME_OF_DAY.match(w)]
    years = [w for w in words if len(w) == 4 and w.isdigit()]
    days = [w for w in words if 1 <= len(w) <= 2 and w.isdigit()]
    if len(clock) != 1 or len(years) != 1 or len(days) != 1:
        return None
    return clock[0], days[0], years[0]


def same_start(recorded: str, actual: str) -> bool:
    """Whether two `lstart` strings name the same start: equal after whitespace
    normalisation, or equal in `start_key` (a record written under another locale)."""
    recorded, actual = " ".join(recorded.split()), " ".join(actual.split())
    if recorded == actual:
        return True
    key = start_key(recorded)
    return key is not None and key == start_key(actual)


def pid_alive(pid: Any, started_at: str = "", strict: bool = False) -> bool:
    """Whether `pid` is a live process (and, given `started_at`, the one that
    started then). EPERM means it is not ours: dead for our purposes.

    Lenient by default (the reaper): with no start time, or when ps cannot answer,
    a live pid counts as alive, which only ever makes the reaper keep more. `strict`
    (adoption, anything that signals) fails closed: no recorded start time, or no
    ps answer, is "not verified", so False."""
    try:
        number = int(pid)
        if number <= 1:  # 0 and negatives address process groups, 1 is launchd
            return False
        os.kill(number, 0)
    except (TypeError, ValueError, OSError):
        return False
    if not started_at:
        return not strict
    actual = process_started_at(number)
    if actual is None:
        return not strict
    return same_start(started_at, actual)


def _pid_exists(pid: Any) -> bool:
    """Whether `os.kill(pid, 0)` finds a process (EPERM too: it exists, it is merely not
    ours). The conservative reading, for deciding what NOT to delete."""
    try:
        number = int(pid)
        if number <= 1:
            return False
        os.kill(number, 0)
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True


def same_process(pid: Any, started_at: str) -> bool:
    """`pid` is alive AND started at `started_at`: the identity check that must pass
    before the dispatcher adopts, counts or signals a process it did not spawn."""
    return pid_alive(pid, started_at, strict=True)


class AdoptedProc:
    """A child of a previous dispatcher process, found alive at startup. It is not
    our child any more (launchd inherited it), so only its liveness is observable,
    and a pid alone proves nothing: the pid may have been recycled for an unrelated
    process. Every poll and every signal therefore re-verifies pid AND start time;
    a mismatch (or a missing start time) reads as "exited" and is never signalled."""

    def __init__(self, pid: int, pgid: int, started_at: str = "",
                 alive: Callable[[Any, str], bool] = same_process) -> None:
        self.pid = pid
        self.pgid = pgid
        self.started_at = started_at
        self._alive = alive

    def verified(self) -> bool:
        return bool(self.started_at) and self._alive(self.pid, self.started_at)

    def poll(self) -> Optional[int]:
        return None if self.verified() else 0  # exit status unknown: not a refusal


@dataclasses.dataclass
class Child:
    name: str
    lane: Lane
    repo: str
    slot: int
    proc: Any
    state_dir: str
    log_path: str
    started_at: float
    assigned_at: Optional[float] = None
    vm_seen: bool = False
    phase: str = ""
    adopted: bool = False
    terminating: bool = False  # we sent it SIGTERM (repository dropped): its exit is not a denial
    vm: str = ""  # the VM (= GitHub runner registration) name, once the child's state names it

    @property
    def pair(self) -> Tuple[str, str]:
        return (self.repo, self.lane.label)

    def owns_runner(self, runner_name: str) -> bool:
        """Whether a job's `runner_name` is this child's registration. A runner is
        registered under its VM's name: `linux-ephr-<supervisor pid>-<n>` on Linux,
        `<child name>-<supervisor pid>-<n>` on macOS."""
        runner = str(runner_name or "").strip()
        if not runner:
            return False
        if self.vm and runner == self.vm:
            return True
        return runner.startswith(self.name + "-") or \
            re.fullmatch(rf"linux-ephr-{self.proc.pid}-\d+", runner) is not None

    def covers(self, scan_started: float) -> bool:
        """Whether this child still accounts for one queued job in a scan that began
        at `scan_started`: until it is assigned a job, and for a short lag after (the
        job list can trail the runner's own state)."""
        if self.terminating:
            return False
        return self.assigned_at is None or self.assigned_at >= scan_started - ASSIGN_LAG_SECONDS


def denial_delay(count: int) -> float:
    return float(min(300, 15 * (2 ** max(0, count - 1))))


class Scheduler:
    def __init__(self, cfg: Config, root: str, tartci_root: str, *,
                 spawn: Callable[[List[str], Dict[str, str], str], Any],
                 admission: Callable[[], bool],
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 home: str = os.path.expanduser("~"),
                 base_env: Optional[Dict[str, str]] = None,
                 identify: Callable[[Any], Optional[str]] = process_started_at) -> None:
        self.cfg = cfg
        self.root = root
        self.tartci = os.path.join(tartci_root, "tartci")
        self.spawn = spawn
        self.identify = identify  # pid -> start time: the identity recorded at spawn
        self.admission = admission
        self.clock = clock
        self.sleep = sleep
        self.home = home
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.children: Dict[str, Child] = {}
        self.stopping = False
        self.refusals: Dict[Tuple[str, str], int] = collections.defaultdict(int)
        self.backoff_until: Dict[Tuple[str, str], float] = {}
        self._counter = 0
        macos_cap = cfg.max_macos
        try:
            macos_cap = min(macos_cap, int(self.base_env.get("TARTCI_MACOS_VM_CAP", macos_cap)))
        except ValueError:
            pass
        self.macos_cap = max(0, min(macos_cap, MACOS_HARD_MAX))

    # -- identity
    def _free_slot(self) -> int:
        used = {child.slot for child in self.children.values()}
        return next(n for n in range(1, 100) if n not in used)

    def _new_name(self, lane: Lane) -> str:
        while True:
            self._counter += 1
            name = f"{CHILD_PREFIX}{LANE_LETTER[lane.label]}{int(self.clock()) % 100000:05d}{self._counter}"
            if name not in self.children and not os.path.exists(os.path.join(self.root, "state", name)):
                return name

    # -- planning
    def plan(self, jobs: Sequence[QueuedJob], scan_started: float) -> List[Tuple[str, str]]:
        """(repo, lane) for each child to start now, oldest queued job first.

        D3: a child is started for a (repo, lane) only while queued jobs outnumber
        that pair's children that are not yet assigned a job. Caps and denial
        backoff apply here; tartci's admission, lease store and macOS cap stay
        authoritative inside each child."""
        if self.stopping or not jobs or not self.admission():
            return []
        by_pair: Dict[Tuple[str, str], List[QueuedJob]] = collections.defaultdict(list)
        for job in jobs:
            if job.lane:
                by_pair[(job.repo, job.lane)].append(job)
        covering: Dict[Tuple[str, str], int] = collections.Counter()
        for child in self.children.values():
            if child.covers(scan_started):
                covering[child.pair] += 1
        needs: List[Tuple[str, int, Tuple[str, str]]] = []
        for pair, group in by_pair.items():
            group.sort(key=lambda j: (j.created_at, j.job_id))
            for job in group[covering[pair]:]:
                needs.append((job.created_at, job.job_id, pair))
        needs.sort()
        total = len(self.children)
        macos = sum(1 for c in self.children.values() if c.lane.os == "macos")
        now = self.clock()
        starts: List[Tuple[str, str]] = []
        for _, _, (repo, lane_label) in needs:
            if total >= self.cfg.max_jobs:
                break
            lane = LANES[lane_label]
            if lane.os == "macos" and macos >= self.macos_cap:
                continue
            if self.backoff_until.get((repo, lane_label), 0.0) > now:
                continue
            starts.append((repo, lane_label))
            total += 1
            macos += 1 if lane.os == "macos" else 0
        return starts

    # -- lifecycle
    def start(self, repo: str, lane_label: str) -> Child:
        lane = LANES[lane_label]
        name = self._new_name(lane)
        slot = self._free_slot()
        env = child_env(self.cfg, self.root, lane, name, slot, self.base_env, self.home)
        state_dir = env["TARTCI_STATE_DIR"]
        log_dir = os.path.join(self.root, "logs", "children")
        log_path = os.path.join(log_dir, f"{name}.log")
        now = self.clock()
        try:
            os.makedirs(state_dir, mode=0o700, exist_ok=True)
            os.makedirs(log_dir, mode=0o700, exist_ok=True)
            if FETCHCONTENT_ENV in env:  # a dir the pool owns, so the lane never mounts a host one
                ensure_private_dir(env[FETCHCONTENT_ENV])
            # Identity first, so a child that outlives this process can be adopted:
            # lane, repo, slot and a pool-unique token that the leftover reaper checks.
            meta: Dict[str, Any] = {"name": name, "lane": lane_label, "repo": repo, "slot": slot,
                                    "token": secrets.token_hex(12), "spawned_at": now,
                                    "dispatcher_pid": os.getpid(), "log": log_path}
            write_private_json(os.path.join(state_dir, CHILD_META), meta)
            proc = self.spawn(child_argv(self.tartci, self.cfg, lane, repo), env, log_path)
            meta["pid"] = getattr(proc, "pid", 0)
            # Record the pid at once, so a crash before the start time is known leaves a
            # live child the reaper keeps (it never deletes a dir whose pid is alive).
            write_private_json(os.path.join(state_dir, CHILD_META), meta)
            # A pid alone is not an identity (pids are recycled, and runner.sh deletes its
            # state file on exit): record when the process started, and adopt only a
            # process that still matches. No start time recorded means never adopted.
            started = self.identify(meta["pid"])
            if started:
                meta["pid_started"] = started
            write_private_json(os.path.join(state_dir, CHILD_META), meta)
        except BaseException:
            shutil.rmtree(state_dir, ignore_errors=True)
            raise
        child = Child(name, lane, repo, slot, proc, state_dir, log_path, now)
        self.children[name] = child
        return child

    def note_start_failure(self, repo: str, lane_label: str) -> None:
        """A child could not even be spawned (EMFILE, no such file...): back the pair off."""
        pair = (repo, lane_label)
        self.refusals[pair] += 1
        self.backoff_until[pair] = self.clock() + denial_delay(self.refusals[pair])

    def refresh(self, child: Child) -> None:
        """Learn the child's phase, VM and assignment from its own state dir."""
        state = read_state_dir(child.state_dir)
        if state:
            child.phase = str(state.get("phase", ""))
            if state.get("vm"):
                child.vm_seen = True
                child.vm = str(state["vm"])
            if child.phase == "job-running" and child.assigned_at is None:
                child.assigned_at = self.clock()

    def observe(self) -> List[Tuple[Child, int]]:
        """Poll children: learn assignment and VM state, collect exits."""
        finished: List[Tuple[Child, int]] = []
        for name in list(self.children):
            child = self.children[name]
            self.refresh(child)
            code = child.proc.poll()
            if code is not None:
                finished.append((child, int(code)))
                del self.children[name]
                self._mark_ended(child)
                self._account_exit(child, int(code))
        return finished

    def _mark_ended(self, child: Child) -> None:
        """Retire the child's identity record: the dir stays (retention removes it), but
        with no pid it can never be adopted, nor its pid mistaken for a live child."""
        path = os.path.join(child.state_dir, CHILD_META)
        meta = read_json(path)
        if not isinstance(meta, dict):
            return
        meta.pop("pid", None)
        meta.pop("pid_started", None)
        meta.update({"ended": True, "ended_at": self.clock()})
        try:
            write_private_json(path, meta)
        except OSError:
            pass

    def _account_exit(self, child: Child, code: int) -> None:
        pair = child.pair
        now = self.clock()
        if code == 0 or child.assigned_at is not None:
            self.refusals.pop(pair, None)
            self.backoff_until.pop(pair, None)
            return
        if child.terminating:
            return  # we stopped it: neither a denial nor a served job
        if child.vm_seen and code == IDLE_EXIT_CODE:
            # It got past admission, the lease and the JIT mint, booted, and waited
            # out its assignment timeout (job cancelled, or taken elsewhere). Not a
            # denial: a short fixed pause, no escalation.
            self.refusals.pop(pair, None)
            self.backoff_until[pair] = now + IDLE_BACKOFF_SECONDS
            return
        # Never assigned and exited non-zero without idling out: tartci's admission or
        # lease refused it before any VM state (a denial), or boot/admission failed
        # after. Back off with growth instead of retrying every tick.
        self.refusals[pair] += 1
        self.backoff_until[pair] = now + denial_delay(self.refusals[pair])

    def stop_unassigned(self, repo: str, recent_since: Optional[float] = None) -> List[str]:
        """SIGTERM every child of `repo` that has not been assigned a job, and (given
        `recent_since`) those assigned at or after it (see `stop_children`). Used only
        for SECURITY drops (and repositories whose owner listing failed): the repository
        no longer qualifies (or cannot be vetted), and a waiting runner is
        still registered with GitHub, which may hand it a job from the repository at any
        moment. runner.sh traps TERM and deletes its VM and registration. The state file
        lags GitHub's assignment, so a runner that took a job in the last moments may be
        stopped too; that is intended."""
        return self.stop_children(repo, recent_since=recent_since)

    def stop_children(self, repo: str, runners: Iterable[str] = (), everything: bool = False,
                      recent_since: Optional[float] = None) -> List[str]:
        """SIGTERM children of `repo`: those not yet assigned a job, those whose assignment
        was observed at or after `recent_since` (they may hold the job that made the
        repository unsafe: our view of an assignment lags GitHub's), plus (security drops
        that found a fork-headed run) any child, assigned or not, whose runner registration
        is in `runners`, or every child when `everything` (fail-safe when the run's jobs
        could not be read, or one is running on a runner we cannot name). Other assigned
        children already hold a job they were given while the repository qualified, so they
        finish."""
        names = [str(runner) for runner in runners if runner]
        stopped: List[str] = []
        for child in self.children.values():
            if child.repo != repo or child.terminating:
                continue
            self.refresh(child)
            recent = (recent_since is not None and child.assigned_at is not None
                      and child.assigned_at >= recent_since)
            if not (everything or child.assigned_at is None or recent
                    or any(child.owns_runner(r) for r in names)):
                continue
            child.terminating = True
            _signal(child.proc, signal.SIGTERM)
            stopped.append(child.name)
        return stopped

    def adopt(self, alive: Optional[Callable[..., bool]] = None,
              log: Callable[[str], None] = lambda message: None) -> List[Child]:
        """Take over children of a previous dispatcher process that are still alive.

        They live in their own session and survive our exit, so a restarted
        dispatcher must count them against `max_jobs`, the macOS cap and D3, or it
        would exceed the caps on top of them. They stay in `children` until they exit.

        A child is adopted only when its pid AND its start time match what was recorded
        (the runner's own state file, else the `pid_started` the dispatcher recorded at
        spawn). A bare pid proves nothing (pids are recycled, and runner.sh deletes its
        state file on exit), and an adopted process is later signalled, so no recorded
        start time means not adopted. Ended children are never adopted."""
        alive = alive or same_process
        adopted: List[Child] = []
        for path in sorted(glob.glob(os.path.join(self.root, "state", CHILD_PREFIX + "*"))):
            name = os.path.basename(path)
            if name in self.children or not os.path.isdir(path) or os.path.islink(path):
                continue
            meta = read_json(os.path.join(path, CHILD_META))
            meta = meta if isinstance(meta, dict) and meta.get("name") == name else {}
            if meta.get("ended"):
                continue
            state = read_state_dir(path)
            candidates = [(state.get("supervisor_pid"), str(state.get("supervisor_pid_started_at") or "")),
                          (meta.get("pid"), str(meta.get("pid_started") or ""))]
            verified = next(((pid, started) for pid, started in candidates
                             if pid and started and alive(pid, started)), None)
            if verified is None:
                continue
            pid, started = verified
            number = int(pid)
            lane_label = str(meta.get("lane") or LETTER_LANE.get(name[len(CHILD_PREFIX):][:1], ""))
            repo = str(meta.get("repo") or state.get("repo") or "")
            if lane_label not in LANES or not repo:
                log(f"cannot adopt {name}: unknown lane or repository")
                continue
            used = {c.slot for c in self.children.values()}
            slot = meta.get("slot") if isinstance(meta.get("slot"), int) and meta.get("slot") not in used else None
            if slot is None:
                slot = next(n for n in range(99, 0, -1) if n not in used)
            try:
                pgid = os.getpgid(number)
            except OSError:
                pgid = number
            child = Child(name, LANES[lane_label], repo, int(slot), AdoptedProc(number, pgid, started, alive), path,
                          str(meta.get("log") or ""), float(meta.get("spawned_at") or self.clock()), adopted=True)
            self.refresh(child)
            if child.assigned_at is not None:
                # Assigned before the restart: when is not known, but the state file was
                # last written no earlier than that. Its mtime keeps an hours-old job from
                # looking "just assigned" to a security drop right after the restart.
                child.assigned_at = min(child.assigned_at, _state_mtime(path) or child.assigned_at)
            self.children[name] = child
            adopted.append(child)
            log(f"adopted {name} lane={lane_label} repo={repo} pid={number} phase={child.phase or '-'} "
                f"assigned={child.assigned_at is not None}")
        return adopted

    def shutdown(self, grace: float = CHILD_TERM_GRACE_SECONDS) -> None:
        """SIGTERM every child, give them `grace` seconds to delete their VMs, then
        SIGKILL what is left, by process group so `tart run` and `ssh` go too."""
        self.stopping = True
        for child in self.children.values():
            _signal(child.proc, signal.SIGTERM)
        deadline = self.clock() + grace
        while self.children and self.clock() < deadline:
            self.observe()
            if self.children:
                self.sleep(0.5)
        for child in list(self.children.values()):
            _kill_group(child.proc, signal.SIGKILL)
        for _ in range(20):  # let the kernel finish the kills before we report
            self.observe()
            if not self.children:
                break
            self.sleep(0.1)


def _signal(proc: Any, sig: int) -> None:
    """Signal one child. A process we did not spawn (an adopted child) is signalled
    only after its pid AND start time are verified again right now: a recycled pid
    belongs to someone else's process, which is never ours to stop."""
    if isinstance(proc, AdoptedProc) and not proc.verified():
        return
    try:
        os.kill(proc.pid, sig)
    except (OSError, ProcessLookupError):
        pass


def _kill_group(proc: Any, sig: int) -> None:
    """Signal a child's whole process group (it leads its own session). Falls back
    to the pid, and never signals our own group or an unverified adopted child's."""
    if isinstance(proc, AdoptedProc) and not proc.verified():
        return
    pgid = getattr(proc, "pgid", None) or proc.pid
    try:
        if pgid > 1 and pgid != os.getpgrp():
            os.killpg(pgid, sig)
            return
    except (OSError, ProcessLookupError):
        pass
    _signal(proc, sig)


def spawn_process(argv: List[str], env: Dict[str, str], log_path: str) -> Any:
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)  # born 0600
    try:
        return subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=fd,
                                stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        os.close(fd)


# ----------------------------------------------------------- leftover reaping at start


def vm_birth_time(home: str) -> Callable[[str], Optional[float]]:
    """When a local Tart VM was created (its directory's birth time; None if unknown)."""
    def born(vm: str) -> Optional[float]:
        try:
            info = os.stat(os.path.join(home, ".tart", "vms", vm))
        except OSError:
            return None
        value = getattr(info, "st_birthtime", None)
        return float(value) if value else None
    return born


VM_BORN_SLACK_SECONDS = 900.0  # a VM is cloned while its child is alive and writing state


def _remove_if_done(path: str, dead_files: List[str]) -> None:
    """Remove the handled state files, and the child's dir once none is left."""
    for file in dead_files:
        try:
            os.unlink(file)
        except OSError:
            pass
    if os.path.islink(path) or glob.glob(os.path.join(path, "*" + TERMINAL_STATE_FILE)):
        return
    shutil.rmtree(path, ignore_errors=True)


def reap_leftovers(root: str, tart: Callable[[List[str]], Any], alive: Callable[..., bool] = pid_alive,
                   log: Callable[[str], None] = lambda message: None,
                   born: Optional[Callable[[str], Optional[float]]] = None,
                   protect: Iterable[str] = ()) -> List[str]:
    """Remove VMs a crashed dispatcher left, and the state files that named them.

    A VM is touched only when it is provably this pool's: its state file sits in a
    `tartci-owners-*` dir that carries the spawn-time identity record (name + token),
    the VM name embeds the dead supervisor's pid, and the VM was created inside the
    child's lifetime (not after the state file's last write). That last check is what
    keeps a recycled pid from pointing us at a native lane's `linux-ephr-<pid>-1`.
    macOS clones named for our children (`tartci-owners-<lane><n>-<pid>-<k>`) are
    ours by name. Leftover runner registrations are not touched (tartci's reaper owns
    them)."""
    born = born or vm_birth_time(os.path.expanduser("~"))
    removed: List[str] = []
    protected = set(protect)
    state_root = os.path.join(root, "state")
    existing: set = set()
    try:
        listing = json.loads(tart(["list", "--source", "local", "--format", "json"]) or "[]")
        existing = {str(item.get("Name")) for item in listing if isinstance(item, dict)}
    except (ValueError, OSError, AttributeError):
        listing = []
    for path in sorted(glob.glob(os.path.join(state_root, CHILD_PREFIX + "*"))):
        name = os.path.basename(path)
        if name in protected or os.path.islink(path) or not os.path.isdir(path):
            continue
        meta = read_json(os.path.join(path, CHILD_META))
        identified = (isinstance(meta, dict) and meta.get("name") == name
                      and isinstance(meta.get("token"), str) and bool(meta.get("token")))
        if (isinstance(meta, dict) and not meta.get("ended") and meta.get("pid") and meta.get("pid_started")
                and alive(meta["pid"], str(meta["pid_started"]))):
            continue  # its recorded child is alive (identity verified), adopted or not: not a leftover
        if (isinstance(meta, dict) and not meta.get("ended") and meta.get("pid") and not meta.get("pid_started")
                and _pid_exists(meta["pid"])):
            # Spawned, but its start time was never recorded (identify failed, or a crash
            # between the two meta writes): nothing proves the pid is not still the child,
            # and nothing proves it is. Never delete its dir, never signal it; a leaked dir
            # costs nothing (retention removes it later).
            log(f"keeping {name}: pid {meta['pid']} is alive but its start time was never recorded")
            continue
        handled: List[str] = []
        for file in sorted(glob.glob(os.path.join(path, "*" + TERMINAL_STATE_FILE))):
            data = read_json(file)
            if not isinstance(data, dict):
                continue
            supervisor = data.get("supervisor_pid")
            if alive(supervisor, str(data.get("supervisor_pid_started_at", ""))):
                handled = []  # a live child: leave the whole dir alone
                break
            vm = str(data.get("vm") or "")
            if vm and vm in existing:
                try:
                    state_mtime = os.path.getmtime(file)
                except OSError:
                    state_mtime = 0.0
                created = born(vm)
                spawned = float(meta.get("spawned_at") or 0.0) if isinstance(meta, dict) else 0.0
                ours = (identified and f"-{supervisor}-" in vm and created is not None
                        and spawned - 5.0 <= created <= state_mtime + VM_BORN_SLACK_SECONDS)
                if not ours:
                    log(f"not reaping VM {vm}: it does not match the identity of {name}")
                    handled = []
                    break
                tart(["stop", vm])
                tart(["delete", vm])
                removed.append(vm)
                existing.discard(vm)
                log(f"reaped leftover VM {vm} (supervisor {supervisor} gone)")
            handled.append(file)
        else:
            _remove_if_done(path, handled)
    for vm in sorted(existing):
        match = re.match(r"^tartci-owners-[mxa]\d+-(\d+)-\d+(?:-\d+)?$", vm)
        if match and not alive(match.group(1)):
            tart(["stop", vm])
            tart(["delete", vm])
            removed.append(vm)
            log(f"reaped leftover VM {vm}")
    return removed


def prune_children(root: str, live: Iterable[str], now: float, max_age: float = RETENTION_SECONDS,
                   keep: int = RETENTION_KEEP) -> int:
    """Retention for what ended children leave behind: per-child state dirs and
    logs/children/*.log older than `max_age`, and any beyond the newest `keep`.
    Live children are never touched. Also expires tartci's per-VM log dirs."""
    live_names = set(live)
    removed = 0

    def entries(pattern: str) -> List[Tuple[float, str]]:
        found: List[Tuple[float, str]] = []
        for path in glob.glob(pattern):
            try:
                found.append((os.lstat(path).st_mtime, path))
            except OSError:
                pass
        return sorted(found, reverse=True)

    def drop(path: str) -> bool:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            return True
        except OSError:
            return False

    for pattern in (os.path.join(root, "state", CHILD_PREFIX + "*"),
                    os.path.join(root, "logs", "children", CHILD_PREFIX + "*.log")):
        for index, (mtime, path) in enumerate(entries(pattern)):
            base = os.path.basename(path)
            if base.endswith(".log"):
                base = base[:-4]
            if base in live_names:
                continue
            if index >= keep or now - mtime > max_age:
                removed += drop(path)
    for sub in ("macos", "linux"):
        for mtime, path in entries(os.path.join(root, "logs", sub, "*")):
            if now - mtime > max_age:
                removed += drop(path)
    return removed


def tart_runner(argv: List[str]) -> str:
    env = scrub_env(dict(os.environ))  # PATH, HOME...: what the children have, no tokens
    env["TART_HOME"] = os.path.join(os.path.expanduser("~"), ".tart")  # not an inherited TART_HOME, as child_env
    proc = subprocess.run(["tart"] + argv, capture_output=True, text=True, timeout=300, env=env)
    return proc.stdout


# ---------------------------------------------------------------------- dispatcher


def git_commit(tartci_root: str) -> str:
    try:
        out = subprocess.run(["git", "-C", tartci_root, "rev-parse", "--short=12", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def log_line(message: str) -> None:
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[owners {stamp}] {message}", flush=True)


class Dispatcher:
    def __init__(self, cfg: Config, root: str, tartci_root: str, client: Any,
                 scheduler: Scheduler, scanner: Scanner, directory: Directory,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 tart: Callable[[List[str]], Any] = tart_runner,
                 alive: Optional[Callable[..., bool]] = None,
                 born: Optional[Callable[[str], Optional[float]]] = None) -> None:
        self.cfg, self.root, self.tartci_root = cfg, root, tartci_root
        self.client, self.sched, self.scanner, self.directory = client, scheduler, scanner, directory
        self.clock, self.sleep, self.tart, self.born = clock, sleep, tart, born
        # The reaper keeps what it cannot disprove (lenient); adoption, which ends in
        # signals, needs a verified identity (strict). An injected `alive` serves both.
        self.alive = alive or pid_alive
        self.adopt_alive = alive or same_process
        self.commit = git_commit(tartci_root)
        self.started_at = clock()
        self.next_scan = 0.0
        self.rate_block_until = 0.0  # GitHub's Retry-After: no scan before this, push hint or not
        self.last_discovery = 0.0
        self.last_prune = 0.0
        self.error_since: Optional[float] = None
        self.last_scans: Dict[str, RepoScan] = {}
        self.last_guards: Dict[str, RepoScan] = {}  # fork-head guard readings of unserved repositories with children
        # repository -> when a SECURITY refusal/drop of it was first detected; kept while it has children
        self._holds: Dict[str, float] = {}
        self.interval_note = ""
        self.exit_code: Optional[int] = None
        self.tick_errors = 0
        self._requested: set = set()
        self._stop_signal: Optional[int] = None

    # seam for the push receiver (a later change): scan this repository now
    def request_scan(self, repo: str) -> None:
        self._requested.add(repo)
        self.next_scan = 0.0

    def request_stop(self, signum: int) -> None:
        """Signal handler: stop admitting at once (a scan already in flight must not
        start children), and let `run` shut the children down."""
        self.sched.stopping = True
        self._stop_signal = signum

    # -- failure accounting
    def _check_sustained(self) -> None:
        if self.error_since is None or self.sched.stopping:
            return
        if self.clock() - self.error_since >= SUSTAINED_FAILURE_SECONDS:
            log_line("GitHub unreachable for ~3 min: admitting no children; will exit 75 once none remain")
            self.sched.stopping = True
            self.exit_code = 75

    def _tick_error(self, where: str, error: BaseException) -> None:
        self.tick_errors += 1
        log_line(f"ERR {where} failed: {type(error).__name__}: {error}")

    def _drop(self, full: str, scan: RepoScan) -> None:
        """A repository stopped qualifying: serve it no more (no new children). A SECURITY
        drop also stops the children still waiting for a job at once; any other drop
        lets them idle out on their own (they time out unassigned at ~120 s), because
        "unassigned" lags GitHub's real assignment by a few seconds and stopping a child
        discards its VM, job included."""
        reason, security = str(scan.dropped), scan.dropped_security
        log_line(f"dropping {full}: {reason}" + ("" if security else " (not a security matter)"))
        self.directory.drop(full, reason, security)
        if not security:
            waiting = sum(1 for c in self.sched.children.values() if c.repo == full)
            if waiting:
                log_line(f"leaving {waiting} child(ren) of {full} to expire on their own "
                         "(the fork-head guard keeps watching them)")
            return
        self._security_stop(full, scan)

    def _stop_waiting(self, repo: str) -> None:
        """A SECURITY drop or refusal: stop the children still waiting for a job, and those
        whose assignment was observed within SECURITY_ASSIGN_LAG_SECONDS before the drop was
        first detected (they may hold the job that made `repo` unsafe). The detection time is
        remembered per repository (`_holds`), so a later cycle applies the same cut-off."""
        since = self._holds.setdefault(repo, self.clock())
        for name in self.sched.stop_unassigned(repo, recent_since=since - SECURITY_ASSIGN_LAG_SECONDS):
            log_line(f"stopping {name}: waiting or just-assigned child of {repo}, which is no longer safe to serve")

    def _security_stop(self, full: str, scan: RepoScan) -> None:
        """Everything a security drop stops: waiting and just-assigned children, then the
        children that hold a job of a fork-headed run (by runner registration), or ALL
        children when that cannot be told (jobs unreadable, or a fork job is running on a
        runner GitHub has not named yet)."""
        self._stop_waiting(full)
        if scan.fork_jobs_unreadable or scan.fork_unknown_runner:
            what = "cannot read the jobs of" if scan.fork_jobs_unreadable else "a job is running, on no named runner, in"
            log_line(f"{what} the fork-headed run(s) of {full}: stopping ALL its children, assigned too")
            for name in self.sched.stop_children(full, everything=True):
                log_line(f"stopping {name}: child of {full}, a fork job may have reached it")
        elif scan.fork_runners:
            for name in self.sched.stop_children(full, runners=scan.fork_runners):
                log_line(f"stopping {name}: it holds a job of a fork-headed run of {full}")
        if scan.fork_pending:
            log_line(f"a fork-headed job of {full} is still queued: reading it again next cycle")

    def _owner_listing_failed(self, repo: str) -> bool:
        """The listing of `repo`'s owner (or of `repo` itself) failed in the latest discovery."""
        errors = {key.lower() for key in self.directory.current.errors}
        return repo.lower() in errors or repo.split("/")[0].lower() in errors

    def _stop_unserved(self) -> None:
        """After a discovery: children of a repository that is no longer served. Stopped
        at once unless the repository is refused for a non-security reason (archived,
        admin rights lost, a setting unreadable...): then they expire on their own. A
        repository that merely vanished from the listing may have turned public, so it
        counts as security, unless its owner's listing failed this time (after a restart
        nothing is remembered as served): then it is unknown, not refused: its waiting
        children are stopped, its assigned ones are left to the fork-head guard."""
        current = self.directory.current
        for repo in sorted({c.repo for c in self.sched.children.values() if c.repo not in current.served}):
            if repo in current.refused and repo not in current.refused_security:
                log_line(f"{repo} is no longer served ({current.refused[repo]}): its children expire on their own")
                continue
            if repo not in current.refused and self._owner_listing_failed(repo):
                # Unknown, not refused: assigned children keep their jobs, but a waiting
                # child is cheap to stop and could otherwise take a job we cannot vet.
                for name in self.sched.stop_unassigned(repo):
                    log_line(f"stopping {name}: waiting child of {repo}, whose owner listing failed")
                continue
            self._stop_waiting(repo)

    def _guard_unserved(self, full: str, scan: RepoScan) -> None:
        """Apply the fork-head guard's reading of a repository that is not served but still has
        children. Fresh evidence (a fork-headed run, or the repository turned public) is a
        security matter whatever made the repository unserved, so it stops the same children a
        security drop does; a standing security hold keeps applying that."""
        if scan.error:
            log_line(f"ERR guarding {full}: {scan.error}")
            return
        if scan.dropped_security:
            if full not in self._holds:
                log_line(f"{full} is not served but has children, and now has: {scan.dropped}")
            refused = self.directory.current.refused
            if full in refused:
                self.directory.current.refused_security.add(full)
            self._security_stop(full, scan)
        elif full in self._holds:
            self._security_stop(full, scan)

    # -- one scan cycle
    def scan_cycle(self) -> None:
        started = self.clock()
        rate_wait = 0.0
        listing_hard = False
        if started - self.last_discovery >= DISCOVERY_SECONDS or not self.directory.current.fetched_at:
            current = self.directory.refresh()
            self.last_discovery = started
            for owner, error in current.errors.items():
                log_line(f"ERR listing {owner}: {error}")
            rate_wait = max(rate_wait, current.retry_after)
            listing_hard = bool(set(current.errors) - current.rate_limited)
            self._stop_unserved()
        repos = sorted(self.directory.current.served)
        scans = scan_all(self.scanner, repos)
        self.last_scans = scans
        jobs: List[QueuedJob] = []
        hard = limited = 0
        for full, scan in scans.items():
            if scan.dropped:
                self._drop(full, scan)
            elif scan.error:
                if scan.rate_limited:
                    limited += 1
                    rate_wait = max(rate_wait, scan.retry_after or 60.0)
                else:
                    hard += 1
                log_line(f"ERR scanning {full}: {scan.error}")
            else:
                jobs.extend(scan.jobs)
        # The fork-head guard: a repository that is not served (refused or dropped, for any
        # reason, or simply unknown after a failed listing) but still has children keeps being
        # checked for a fork-headed run every cycle, until its children are gone. A security
        # refusal that came first, or a drop for another reason, must not end the guard.
        guarded = sorted({c.repo for c in self.sched.children.values() if not c.terminating}
                         - set(self.directory.current.served)
                         - {full for full, scan in scans.items() if scan.runs_checked})
        self.last_guards = scan_all(self.scanner, guarded, scan=self.scanner.guard_repo) if guarded else {}
        for full, guard in self.last_guards.items():
            if guard.error and guard.rate_limited:
                rate_wait = max(rate_wait, guard.retry_after or 60.0)
            self._guard_unserved(full, guard)
        with_children = {c.repo for c in self.sched.children.values()}
        for full in [repo for repo in self._holds if repo in self.directory.current.served or repo not in with_children]:
            del self._holds[full]
        # Unreachable means nothing could be read for a reason other than a rate limit:
        # a 403 with Retry-After proves GitHub is up, so it never feeds the exit-75 rule.
        unreachable = (hard > 0 and hard + limited == len(repos)) or (listing_hard and not repos)
        if unreachable:
            self.error_since = self.error_since or started
        else:
            self.error_since = None
        self._check_sustained()
        if self._stop_signal is None:
            for repo, lane in self.sched.plan(jobs, started):
                try:
                    child = self.sched.start(repo, lane)
                except Exception as error:  # EMFILE, ENOENT...: back the pair off, keep the cycle
                    self._tick_error(f"starting a {lane} child for {repo}", error)
                    self.sched.note_start_failure(repo, lane)
                    continue
                log_line(f"start {child.name} lane={lane} repo={repo} "
                         f"({len(self.sched.children)}/{self.cfg.max_jobs} children)")
        interval, note = adaptive_interval(self.cfg.poll_seconds, self.client.rate, self.clock())
        if rate_wait > 0:
            rate_wait = min(rate_wait, MAX_RATE_WAIT_SECONDS)
            self.rate_block_until = self.clock() + rate_wait
            if rate_wait > interval:
                interval, note = rate_wait, f"GitHub asked us to wait {rate_wait:.0f}s; next scan delayed"
        self.interval_note = note
        if note:
            log_line(note)
        self.next_scan = self.clock() + interval

    def _scan_due(self) -> bool:
        now = self.clock()
        return now >= self.next_scan and now >= self.rate_block_until \
            and not (self.sched.stopping and self.exit_code is None)

    def tick(self) -> None:
        """One pass. It never raises: a failure here is logged, and a failed scan
        cycle counts toward the sustained-failure rule; the children keep running."""
        try:
            for child, code in self.sched.observe():
                assigned = "assigned" if child.assigned_at is not None else "never assigned"
                log_line(f"exit {child.name} lane={child.lane.label} rc={code} ({assigned}; log {child.log_path})")
        except Exception as error:
            self._tick_error("observing children", error)
        if self._scan_due():
            try:
                self.scan_cycle()
            except Exception as error:
                self._tick_error("scan cycle", error)
                if self.error_since is None:
                    self.error_since = self.clock()
                self._check_sustained()
                self.next_scan = self.clock() + max(self.cfg.poll_seconds, 10)
        try:
            self.write_snapshot()
            if self.clock() - self.last_prune >= PRUNE_EVERY_SECONDS:
                self.last_prune = self.clock()
                prune_children(self.root, self.sched.children, self.clock())
        except Exception as error:
            self._tick_error("housekeeping", error)

    def startup(self) -> None:
        """Before serving: adopt live children of a previous process, then remove what
        crashed ones left behind. Neither may stop the dispatcher from starting."""
        try:
            self.sched.adopt(alive=self.adopt_alive, log=log_line)
        except Exception as error:
            self._tick_error("adopting children", error)
        try:
            reap_leftovers(self.root, self.tart, alive=self.alive, log=log_line, born=self.born,
                           protect=list(self.sched.children))
            prune_children(self.root, self.sched.children, self.clock())
            self.last_prune = self.clock()
        except Exception as error:
            self._tick_error("reaping leftovers", error)

    def run(self) -> int:
        self.startup()
        log_line(f"dispatcher up commit={self.commit} max_jobs={self.cfg.max_jobs} "
                 f"poll={self.cfg.poll_seconds}s root={self.root} children_adopted={len(self.sched.children)}")
        while True:
            if self._stop_signal is not None:
                log_line(f"signal {self._stop_signal}: stopping children")
                try:
                    self.sched.shutdown()
                except Exception as error:
                    self._tick_error("shutdown", error)
                self.write_snapshot(final=True)
                return 128 + self._stop_signal
            self.tick()
            # Never exit while children run: a sustained-failure exit waits for them.
            if self.exit_code is not None and not self.sched.children:
                self.write_snapshot(final=True)
                return self.exit_code
            self.sleep(1.0)

    # -- status snapshot
    def snapshot(self, final: bool = False) -> Dict[str, Any]:
        current = self.directory.current
        return {
            "pid": os.getpid(), "running": not final, "commit": self.commit,
            "started_at": self.started_at, "updated_at": self.clock(),
            "max_jobs": self.cfg.max_jobs, "macos_cap": self.sched.macos_cap,
            "vm_size": self.cfg.vm_size_view(),
            "admitting": not self.sched.stopping, "tick_errors": self.tick_errors,
            "children": [{"name": c.name, "lane": c.lane.label, "repo": c.repo, "pid": c.proc.pid,
                          "phase": c.phase, "assigned": c.assigned_at is not None, "adopted": c.adopted,
                          "age_s": int(self.clock() - c.started_at)} for c in self.sched.children.values()],
            "repositories": {"served": sorted(current.served), "refused": current.refused,
                             "listing_errors": current.errors},
            "queued": {full: len(s.jobs) for full, s in self.last_scans.items() if s.jobs},
            "guarded": sorted(self.last_guards),  # unserved repositories still watched for fork-headed runs
            "scan_errors": {full: s.error for full, s in self.last_scans.items() if s.error},
            "backoff": {f"{repo}|{lane}": int(until - self.clock())
                        for (repo, lane), until in self.sched.backoff_until.items() if until > self.clock()},
            "rate": {"remaining": self.client.rate.remaining, "limit": self.client.rate.limit,
                     "headroom": self.client.rate.headroom(), "requests": getattr(self.client, "requests", 0),
                     "not_modified": getattr(self.client, "not_modified", 0), "note": self.interval_note},
        }

    def write_snapshot(self, final: bool = False) -> None:
        path = os.path.join(self.root, "state", "dispatcher.json")
        try:
            ensure_private_dir(os.path.dirname(path))
            write_private_json(path, self.snapshot(final))
        except OSError:
            pass


# ------------------------------------------------------------------------- CLI


def default_root() -> str:
    return os.environ.get("TARTCI_OWNERS_ROOT") or os.path.join(os.path.expanduser("~"), ".tartci", "owners")


def tartci_root_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def format_dry_run(cfg: Config, repo_set: RepoSet, scans: Dict[str, RepoScan]) -> Tuple[List[str], int]:
    """Lines for `dry-run`, and the count of failed scans/listings."""
    lines: List[str] = []
    errors = len(repo_set.errors)
    owners = list(cfg.owners)
    extras = [r for r in cfg.repos if r.split("/")[0].lower() not in {o.lower() for o in owners}]
    groups: List[Tuple[str, List[str]]] = [(owner, [f for f in sorted(repo_set.served)
                                                   if f.split("/")[0].lower() == owner.lower()]) for owner in owners]
    if extras:
        groups.append(("(extra repos)", [f for f in sorted(repo_set.served)
                                          if f.split("/")[0].lower() not in {o.lower() for o in owners}]))
    for owner, names in groups:
        refused = [f for f in repo_set.refused if f.split("/")[0].lower() == owner.lower()] if owner in owners else []
        lines.append(f"owner {owner}: {len(names)} private repositories served, {len(refused)} refused")
        if owner in repo_set.errors:
            lines.append(f"  ERR listing {owner}: {repo_set.errors[owner]}")
        for full in refused:
            lines.append(f"  refused {full}: {repo_set.refused[full]}")
        for full in names:
            scan = scans.get(full)
            if scan is None:
                continue
            if scan.error:
                errors += 1
                lines.append(f"  {full}: ERR {scan.error}")
                continue
            if scan.dropped:
                lines.append(f"  {full}: dropped ({scan.dropped})")
                continue
            if not scan.jobs and not scan.skipped:
                continue
            lines.append(f"  {full}: {len(scan.jobs)} queued job(s) to serve")
            for job in sorted(scan.jobs, key=lambda j: (j.created_at, j.job_id)):
                lines.append(f"    would serve job {job.job_id} (run {job.run_id}, {job.name!r}) "
                             f"labels={list(job.labels)} -> lane {job.lane}")
            for job in scan.skipped:
                lines.append(f"    not served job {job.job_id} ({job.name!r}) labels={list(job.labels)}: "
                             "not a subset of {self-hosted, one lane}")
    return lines, errors


def cmd_dry_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    env = gh_env()
    problem = check_gh_auth(cfg.gh_cli, env)
    if problem:
        print(f"ERR {problem}", file=sys.stderr)
    client = GhClient(cfg.gh_cli, env=env)
    directory = Directory(client, cfg)
    scanner = Scanner(client)
    try:
        repo_set = directory.refresh()
    except GhError as error:
        print(f"ERR discovery failed: {error}", file=sys.stderr)
        return 3
    scans: Dict[str, RepoScan] = {}
    for _ in range(max(1, args.scans)):
        scans = scan_all(scanner, sorted(repo_set.served))
    lines, errors = format_dry_run(cfg, repo_set, scans)
    served_jobs = sum(len(s.jobs) for s in scans.values())
    skipped_jobs = sum(len(s.skipped) for s in scans.values())
    lines.append(f"dry-run: {len(repo_set.served)} repositories served, {len(repo_set.refused)} refused, "
                 f"{served_jobs} queued job(s) would be served, {skipped_jobs} queued job(s) not served, "
                 f"{errors} error(s); nothing started, nothing minted")
    lines.append(format_vm_size(cfg.vm_size_view()))
    lines.append(f"requests={client.requests} not_modified={client.not_modified} "
                 f"rate_remaining={client.rate.remaining}/{client.rate.limit}")
    if args.json:
        print(json.dumps({"served": sorted(repo_set.served), "refused": repo_set.refused,
                          "errors": repo_set.errors, "vm_size": cfg.vm_size_view(),
                          "jobs": [dataclasses.asdict(j) for s in scans.values() for j in s.jobs]}, indent=2))
    else:
        print("\n".join(lines))
    return 3 if errors else 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    root = args.root
    tartci_root = tartci_root_dir()
    ensure_private_dir(root)  # holds the state of jobs and their logs: owner only
    for sub in ("state", "logs", "logs/children", "logs/macos", "logs/linux", "fetchcontent-src"):
        ensure_private_dir(os.path.join(root, sub))
    os.makedirs(os.path.join(root, "cache"), exist_ok=True)  # shared with guests; the 0700 root guards it
    # The dispatcher's `gh` runs as its children's does: keyring login, no GH_TOKEN/GITHUB_TOKEN.
    env = gh_env()
    problem = check_gh_auth(cfg.gh_cli, env)
    if problem:
        log_line(f"ERR {problem}; the children authenticate the same way, so no JIT config can be minted "
                 "until `gh auth login` is done for this user (tokens in the environment are not used)")
    client = GhClient(cfg.gh_cli, env=env)
    sched = Scheduler(cfg, root, tartci_root, spawn=spawn_process,
                      admission=lambda: pool_admission_open(tartci_root))
    dispatcher = Dispatcher(cfg, root, tartci_root, client, sched, Scanner(client), Directory(client, cfg))
    signal.signal(signal.SIGTERM, lambda signum, _frame: dispatcher.request_stop(signum))
    signal.signal(signal.SIGINT, lambda signum, _frame: dispatcher.request_stop(signum))
    return dispatcher.run()


def cmd_status(args: argparse.Namespace) -> int:
    path = os.path.join(args.root, "state", "dispatcher.json")
    snap: Optional[Dict[str, Any]] = None
    try:
        with open(path, encoding="utf-8") as handle:
            snap = json.load(handle)
    except (OSError, ValueError):
        snap = None
    alive = bool(snap) and snap.get("running") and pid_alive(snap.get("pid"))  # type: ignore[union-attr]
    if args.json:
        print(json.dumps({"running": alive, "snapshot": snap}, indent=2))
        return 0 if alive else 1
    if not snap:
        print(f"owners dispatcher: no status recorded under {args.root} (not started yet)")
        return 1
    print(f"owners dispatcher: {'running' if alive else 'NOT running'} pid={snap.get('pid')} commit={snap.get('commit')}")
    print(f"  admitting={snap.get('admitting')} max_jobs={snap.get('max_jobs')} macos_cap={snap.get('macos_cap')}")
    if "vm_size" in snap:  # absent from a snapshot written before sizes were configurable
        print(f"  {format_vm_size(snap['vm_size'])}")
    rate = snap.get("rate", {})
    headroom = rate.get("headroom")
    print(f"  rate remaining={rate.get('remaining')}/{rate.get('limit')} "
          f"headroom={'n/a' if headroom is None else format(headroom, '.0%')} "
          f"requests={rate.get('requests')} 304s={rate.get('not_modified')} {rate.get('note') or ''}".rstrip())
    repos = snap.get("repositories", {})
    print(f"  repositories: {len(repos.get('served', []))} served, {len(repos.get('refused', {}))} refused")
    for full, reason in sorted(repos.get("refused", {}).items()):
        print(f"    refused {full}: {reason}")
    for owner, error in sorted(repos.get("listing_errors", {}).items()):
        print(f"    ERR listing {owner}: {error}")
    for full, error in sorted(snap.get("scan_errors", {}).items()):
        print(f"    ERR scan {full}: {error}")
    for full in snap.get("guarded", []):
        print(f"    guarding {full}: not served, children still running")
    for full, count in sorted(snap.get("queued", {}).items()):
        print(f"    queued {full}: {count}")
    children = snap.get("children", [])
    print(f"  children: {len(children)}")
    for child in children:
        print(f"    {child['name']} lane={child['lane']} repo={child['repo']} pid={child['pid']} "
              f"phase={child['phase'] or '-'} assigned={child['assigned']} age={child['age_s']}s")
    for key, seconds in sorted(snap.get("backoff", {}).items()):
        print(f"    backoff {key}: {seconds}s")
    return 0 if alive else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci owners", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=default_config_path(), help="owners.toml (default ~/.config/tartci/owners.toml)")
    parser.add_argument("--root", default=default_root(), help="state/cache/log root (default ~/.tartci/owners)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the dispatcher in the foreground")
    dry = sub.add_parser("dry-run", help="print what would be served; start and mint nothing")
    dry.add_argument("--json", action="store_true")
    dry.add_argument("--scans", type=int, default=1, help="repeat the scan (measures 304 reuse)")
    status = sub.add_parser("status", help="repositories, children, headroom, commit")
    status.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        return {"run": cmd_run, "dry-run": cmd_dry_run, "status": cmd_status}[args.command](args)
    except ConfigError as error:
        print(f"owners: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
