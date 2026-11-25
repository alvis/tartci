#!/usr/bin/env python3
"""Push discovery for `tartci owners` (docs/owner-pool.md, "Push discovery").

Supervised `gh webhook forward` processes deliver `workflow_job` events to a receiver on
127.0.0.1. An event is only a HINT: it makes the dispatcher scan that one repository with the
same Scanner every scan uses, and the scan (private, not a fork run, labels) decides what is
served. Nothing is polled: events, the catch-up of each forwarder that comes up, and local
retries are the only reasons the dispatcher calls GitHub. Nothing in a payload is trusted for
admission.

    Target       one forwarder per organization in `owners`, one per `repos` entry whose owner
                 is not one of them (a personal account is served only repository by repository)
    Receiver     127.0.0.1 only; X-Hub-Signature-256 checked against a random secret made at
                 each dispatcher start; a path per forwarder; bodies capped; bodies never logged
    Supervisor   starts each forwarder in its own session with the forwarder login's token as
                 GH_TOKEN (and nowhere else), marks it up on the signed `ping` of its new hook,
                 restarts it when it exits (GitHub drops it every 1-2 h), backs off on HTTP 422
                 ("Hook already exists"), deletes a stale hook only when it is one this pool
                 recorded and its name is `cli`, and restarts a stalled one (stopped, or its
                 socket in CLOSE_WAIT) found by local checks that cost no API call
                 Restarts that repeat (quick exits, forced restarts) climb a backoff ladder;
                 an org whose account type could not be read waits until it is read
    Persistence  <root>/state/forwarders.json: live forwarder pids and their group members (with
                 start times) and the hook ids their pings reported, so a restarted dispatcher
                 (or `uninstall`) can stop an orphan and the stale-hook rule survives a restart

The measured behaviour this relies on (cli/gh-webhook v0.2.0) is in docs/owner-pool.md.
"""
from __future__ import annotations

import collections
import dataclasses
import hashlib
import hmac
import http.server
import json
import os
import queue
import re
import secrets
import signal
import subprocess
import threading
import time
import zlib
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import owners_dispatch as od

TOKEN_PATTERN = re.compile(r"gh[opsu]_[A-Za-z0-9_]+")
EVENTS = "workflow_job"
MAX_BODY_BYTES = 1 << 20  # a workflow_job payload is ~10-30 KB; GitHub's own cap is 25 MB
PING_TIMEOUT_SECONDS = 30.0  # no ping by then, but the process lives: assume up, catch up anyway
RESTART_DELAY_SECONDS = 5.0  # GitHub removes a dead forwarder's hook within ~4 s (measured)
BACKOFF_STEPS = (10.0, 20.0, 40.0, 80.0, 120.0)  # 422, or an exit soon after starting
FAST_EXIT_SECONDS = 60.0  # an exit this soon after starting (or after coming up) backs off too
FORCED_RESTART_WINDOW_SECONDS = 600.0  # forced restarts (stall, hook gone) this close together back off
DOWN_LOG_EVERY_SECONDS = 600.0  # a target that cannot run is logged as down at most this often
DOWN_AFTER_SECONDS = 600.0  # a target not up for this long (any reason) is logged as down
STALE_HOOK_AFTER_SECONDS = 60.0  # continuous 422s before a recorded `cli` hook is deleted
STALE_HOOK_RETRY_SECONDS = 600.0
LIVENESS_EVERY_SECONDS = 20.0  # one `ps` and one `lsof` for every forwarder
HOOK_CHECK_SECONDS = 1800.0  # REST: does an up forwarder's hook still exist
STOP_GRACE_SECONDS = 3.0
ROTATE_STAGGER_MAX_SECONDS = 300.0  # spread planned restarts so targets do not rotate together
LOG_MAX_BYTES = 1 << 20
HOOKS_KEPT = 20  # recorded hook ids per target
STATE_FILE = "forwarders.json"
STATES = ("checking", "starting", "up", "rotating", "backing-off", "stalled", "refused", "stopped")


def mask(text: Any) -> str:
    """`text` with anything shaped like a GitHub token replaced."""
    return TOKEN_PATTERN.sub("gh*_***", str(text))


class PushError(Exception):
    pass


# ----------------------------------------------------------------------- the login


def read_forwarder_token(config_dir: str) -> str:
    """The OAuth token of the forwarder login in `<config_dir>/hosts.yml` (github.com). It is read
    in-process, held in memory and handed only to forwarders (and the hook checks) as GH_TOKEN.
    The error never contains the token. Parses the two layouts `gh` writes: the per-user
    `users: <login>: oauth_token:` map (the active `user:` wins) and a host-level `oauth_token:`."""
    path = os.path.join(os.path.expanduser(config_dir), "hosts.yml")
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as error:
        raise PushError(f"cannot read the forwarder login {path}: {error.strerror or error}") from None
    stack: List[Tuple[int, str]] = []
    host_token = ""
    user_tokens: Dict[str, str] = {}
    active = ""
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#") or ":" not in raw:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key, _, value = raw.strip().partition(":")
        key, value = key.strip().strip("'\""), value.strip().strip("'\"")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        keys = [name for _, name in stack]
        if not value:
            stack.append((indent, key))
            continue
        if not keys or keys[0] != "github.com":
            continue
        if key == "oauth_token" and keys == ["github.com"]:
            host_token = value
        elif key == "oauth_token" and len(keys) == 3 and keys[1] == "users":
            user_tokens[keys[2]] = value
        elif key == "user" and keys == ["github.com"]:
            active = value
    token = user_tokens.get(active) or host_token or (next(iter(user_tokens.values())) if len(user_tokens) == 1 else "")
    if not token:
        raise PushError(f"no github.com oauth_token in {path}: run the forwarder login (docs/owner-pool.md)")
    return token


def forwarder_binary(home: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The gh-webhook extension's own executable, when installed (it reads GH_TOKEN itself, so it
    runs without the ~40 MB `gh` parent), else None (then `gh webhook forward` is used)."""
    env = os.environ if env is None else env
    data = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    path = os.path.join(data, "gh", "extensions", "gh-webhook", "gh-webhook")
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


# ----------------------------------------------------------------------- targets


@dataclasses.dataclass(frozen=True)
class Target:
    kind: str  # "org" or "repo"
    name: str  # organization login, or owner/repo

    @property
    def key(self) -> str:
        """The receiver path segment and log name of this target's forwarder."""
        return f"{self.kind}-" + re.sub(r"[^A-Za-z0-9._-]", "_", self.name.replace("/", "__")).lower()

    @property
    def label(self) -> str:
        return f"{self.kind} {self.name}"

    def covers(self, full: str) -> bool:
        full = str(full or "").lower()
        if self.kind == "org":
            return full.split("/")[0] == self.name.lower() and "/" in full
        return full == self.name.lower()

    def hooks_path(self) -> str:
        return f"orgs/{self.name}/hooks" if self.kind == "org" else f"repos/{self.name}/hooks"

    def flag(self) -> List[str]:
        return ["--org", self.name] if self.kind == "org" else ["--repo", self.name]


def targets_for(cfg: "od.Config") -> List[Target]:
    """D15 registration: one forwarder per owner in `owners` (each must be an organization:
    `check_owner_types` refuses a personal account) and one per `repos` entry whose owner is not
    in `owners` (an org already in `owners` covers its repositories). Excluded repositories get none."""
    owners = {owner.lower() for owner in cfg.owners}
    excluded = {name.lower() for name in cfg.exclude_repos}
    found = [Target("org", owner) for owner in cfg.owners]
    found += [Target("repo", full) for full in cfg.repos
              if full.split("/")[0].lower() not in owners and full.lower() not in excluded]
    return found


def check_owner_types(client: Any, owners: Sequence[str]) -> Dict[str, str]:
    """Raise ConfigError when an owner is a personal (User) account: push serves a personal
    account's repositories only when they are listed one by one in `repos` (one forwarder each),
    so the pool never runs one forwarder per personal repository behind the operator's back.
    Owners whose type cannot be read now are returned (owner -> error): their org forwarder is
    not started until the Supervisor has read the type (`owner_type`), and discovery refuses a
    personal account in the meantime."""
    unknown: Dict[str, str] = {}
    for owner in owners:
        try:
            body = client.get(f"users/{owner}").body
        except od.GhError as error:
            unknown[owner] = str(error)
            continue
        kind = body.get("type") if isinstance(body, dict) else None
        if kind == "User":
            raise od.ConfigError(
                f"owners: {owner} is a personal account. A personal account's private repositories are "
                f"served only when listed one by one in `repos` (one forwarder each); "
                f"move {owner} from `owners` to `repos` entries")
        if kind != "Organization":
            unknown[owner] = f"unexpected account type {kind!r}"
    return unknown


def owner_type_reader(client: Any) -> Callable[[str], Optional[str]]:
    """`owner_type` for the Supervisor: the account type GitHub reports (one REST call as the
    main login), or None when the answer has no type. Raises GhError when it cannot be read."""
    def read(owner: str) -> Optional[str]:
        body = client.get(f"users/{owner}").body
        kind = body.get("type") if isinstance(body, dict) else None
        return str(kind) if kind else None
    return read


# ----------------------------------------------------------------------- receiver


@dataclasses.dataclass(frozen=True)
class Event:
    key: str  # the forwarder (receiver path) it came through
    event: str  # X-GitHub-Event
    action: str = ""
    repo: str = ""
    private: Optional[bool] = None
    labels: Tuple[str, ...] = ()
    job_id: int = 0
    runner_name: str = ""
    hook_id: Optional[int] = None
    hook_name: str = ""
    received_at: float = 0.0


def _int_or_none(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class Receiver:
    """The local end of every forwarder. `handle` is the whole policy (and what the tests drive);
    the HTTP server only feeds it. Accepted events wait in a queue the dispatcher drains on its
    own thread, so no GitHub call or child start ever runs on a request thread."""

    def __init__(self, secret: str, port: int = 0, max_body: int = MAX_BODY_BYTES,
                 clock: Callable[[], float] = time.time) -> None:
        self._secret = secret.encode()
        self.port = port
        self.max_body = max_body
        self.clock = clock
        self.events: "queue.Queue[Event]" = queue.Queue()
        self._keys: set = set()
        self._lock = threading.Lock()
        self.stats: Dict[str, int] = collections.Counter()
        self._server: Optional[http.server.ThreadingHTTPServer] = None

    def allow(self, key: str) -> None:
        with self._lock:
            self._keys.add(key)

    def url(self, key: str) -> str:
        return f"http://127.0.0.1:{self.port}/f/{key}"

    def _count(self, name: str) -> None:
        with self._lock:
            self.stats[name] += 1

    def handle(self, path: str, headers: Dict[str, str], body: bytes) -> int:
        """The HTTP status for one delivery; a valid one is queued as an Event. Headers are
        matched case-insensitively. Nothing of the body is ever logged."""
        lower = {str(k).lower(): str(v) for k, v in headers.items()}
        match = re.fullmatch(r"/f/([A-Za-z0-9._-]+)", path.split("?")[0])
        with self._lock:
            known = bool(match) and match.group(1) in self._keys
        if not known or match is None:
            self._count("unknown_path")
            return 404
        signature = lower.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            self._count("bad_signature")
            return 401
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._count("malformed")
            return 400
        if not isinstance(payload, dict):
            self._count("malformed")
            return 400
        kind = lower.get("x-github-event", "")
        key = match.group(1)
        now = self.clock()
        if kind == "ping":
            hook = payload.get("hook") if isinstance(payload.get("hook"), dict) else {}
            self.events.put(Event(key, "ping", hook_id=_int_or_none(payload.get("hook_id"))
                                  or _int_or_none(lower.get("x-github-hook-id")),
                                  hook_name=str(hook.get("name") or ""), received_at=now))
        elif kind == "workflow_job":
            repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
            job = payload.get("workflow_job") if isinstance(payload.get("workflow_job"), dict) else {}
            labels = job.get("labels") if isinstance(job.get("labels"), list) else []
            private = repository.get("private")
            self.events.put(Event(
                key, "workflow_job", action=str(payload.get("action") or ""),
                repo=str(repository.get("full_name") or ""),
                private=private if isinstance(private, bool) else None,
                labels=tuple(str(label).lower() for label in labels if str(label).strip())[:32],
                job_id=_int_or_none(job.get("id")) or 0, runner_name=str(job.get("runner_name") or ""),
                hook_id=_int_or_none(lower.get("x-github-hook-id")), received_at=now))
        else:
            self._count("ignored")
            return 200
        self._count("accepted")
        return 200

    def drain(self) -> List[Event]:
        found: List[Event] = []
        while True:
            try:
                found.append(self.events.get_nowait())
            except queue.Empty:
                return found

    def start(self) -> None:
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "tartci-owners"
            sys_version = ""
            timeout = 10  # a local client that stalls mid-request cannot pin a thread

            def log_message(self, *args: Any) -> None:  # never log requests (paths, headers or bodies)
                pass

            def _reply(self, code: int) -> None:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                self._reply(404)

            def do_POST(self) -> None:  # noqa: N802
                length = self.headers.get("Content-Length")
                if length is None or not length.isdigit():
                    receiver._count("no_length")
                    self._reply(411)
                    return
                if int(length) > receiver.max_body:
                    receiver._count("too_large")
                    self._reply(413)
                    return
                body = self.rfile.read(int(length))
                self._reply(receiver.handle(self.path, dict(self.headers.items()), body))

        server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        server.daemon_threads = True
        self.port = int(server.server_address[1])
        self._server = server
        threading.Thread(target=server.serve_forever, name="owners-receiver", daemon=True).start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


# ----------------------------------------------------------------------- GitHub hook API


class HookApi:
    """The rare REST calls about a forwarder's own hook, made as the FORWARDER login (its token as
    GH_TOKEN, GH_CONFIG_DIR its own dir): read one hook, delete one hook. The main login never
    gets hook scopes and never makes these calls."""

    def __init__(self, gh_cli: str, token: Callable[[], str], config_dir: str,
                 run: Callable[..., Any] = subprocess.run, base_env: Optional[Dict[str, str]] = None) -> None:
        self.gh_cli = gh_cli
        self._token = token
        self.config_dir = os.path.expanduser(config_dir)
        self._run = run
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.requests = 0

    def _env(self) -> Dict[str, str]:
        return forwarder_env(self.base_env, self._token(), self.config_dir)

    def _call(self, method: str, path: str) -> Tuple[int, Any]:
        argv = [self.gh_cli, "api", "-i", "-X", method, path]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=60, env=self._env())
        except (OSError, subprocess.SubprocessError) as error:
            raise od.GhError(f"gh api {method} {path} failed: {mask(error)}") from None
        self.requests += 1
        status, _, body = od.parse_gh_i(proc.stdout or "")
        if status == 0:
            detail = ((proc.stderr or "").strip().splitlines() or ["no response"])[-1]
            raise od.GhError(f"gh api {method} {path} failed: {mask(detail)[:200]}")
        try:
            payload = json.loads(body) if body.strip() else None
        except ValueError:
            payload = None
        return status, payload

    def get_hook(self, target: Target, hook_id: int) -> Optional[Dict[str, Any]]:
        """The hook, or None when GitHub says it is gone (404)."""
        status, payload = self._call("GET", f"{target.hooks_path()}/{int(hook_id)}")
        if status == 404:
            return None
        if status >= 400 or not isinstance(payload, dict):
            raise od.GhError(f"HTTP {status} reading hook {hook_id} of {target.label}", status=status)
        return payload

    def delete_hook(self, target: Target, hook_id: int) -> bool:
        status, _ = self._call("DELETE", f"{target.hooks_path()}/{int(hook_id)}")
        return status in (204, 404)


def forwarder_env(base_env: Dict[str, str], token: str, config_dir: str) -> Dict[str, str]:
    """A forwarder's environment: PATH and HOME, the forwarder login's token and config dir, and
    nothing else (no inherited tokens, no TARTCI_*). It is built fresh for each forwarder and
    never merged into the dispatcher's own environment, which children inherit from."""
    env = {key: base_env[key] for key in ("PATH", "HOME", "TMPDIR", "USER", "LOGNAME") if key in base_env}
    env.update({"GH_TOKEN": token, "GH_CONFIG_DIR": os.path.expanduser(config_dir),
                "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"})
    return env


# ----------------------------------------------------------------------- local liveness


def inspect_groups(pgids: Iterable[int], run: Callable[..., Any] = subprocess.run) -> Dict[int, Dict[str, Any]]:
    """For each process group: is any member alive, is one stopped (state T), its member pids with
    their start times (read by the same `ps` call, so a pid cannot be reused in between), and the
    TCP states of its non-loopback sockets (None when lsof could not say). One `ps` and one `lsof`
    for all groups; no GitHub call. lsof exits 1 when nothing matched (no output at all: no TCP
    socket, []) and when it failed (a message on stderr: unknown, None)."""
    groups = sorted({int(pgid) for pgid in pgids if int(pgid) > 1})
    found: Dict[int, Dict[str, Any]] = {g: {"alive": False, "stopped": False, "tcp": None, "pids": [], "started": {}}
                                        for g in groups}
    if not groups:
        return found
    try:
        proc = run(["ps", "-axo", "pid=,pgid=,stat=,lstart="], capture_output=True, text=True, timeout=15,
                   env=od.stable_time_env())
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1].isdigit() and int(parts[1]) in found:
                group = found[int(parts[1])]
                group["alive"] = True
                if parts[0].isdigit():
                    group["pids"].append(int(parts[0]))
                    if len(parts) > 3:
                        group["started"][int(parts[0])] = " ".join(parts[3:])
                if parts[2].startswith("T"):
                    group["stopped"] = True
    except (OSError, subprocess.SubprocessError):
        return {g: {"alive": True, "stopped": False, "tcp": None, "pids": [], "started": {}} for g in groups}  # unknown
    try:
        proc = run(["lsof", "-nP", "-a", "-iTCP", "-g", ",".join(str(g) for g in groups), "-F", "gnT"],
                   capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return found
    if proc.returncode not in (0, 1):
        return found  # failed: unknown
    if proc.returncode == 1 and not (proc.stdout or "").strip() and (proc.stderr or "").strip():
        return found  # exit 1 with a message: lsof failed, which is not "no socket"
    for group in groups:
        found[group]["tcp"] = []
    current: Optional[int] = None
    remote = ""
    for line in (proc.stdout or "").splitlines():
        tag, value = line[:1], line[1:]
        if tag == "g":
            current = int(value) if value.isdigit() else None
        elif tag == "n":
            remote = value.split("->", 1)[1] if "->" in value else ""
        elif tag == "T" and value.startswith("ST=") and current in found and remote:
            host = remote.rsplit(":", 1)[0].strip("[]")
            if not (host.startswith("127.") or host in ("::1", "localhost")):
                found[current]["tcp"].append(value[3:])
    return found


def stall_reason(info: Dict[str, Any]) -> str:
    """Why a live forwarder's group looks stalled ('' when it looks healthy or cannot be told)."""
    if info.get("stopped"):
        return "a forwarder process is stopped (state T)"
    tcp = info.get("tcp")
    if tcp is None:
        return ""
    if "CLOSE_WAIT" in tcp:
        return "its GitHub socket is in CLOSE_WAIT"
    if "ESTABLISHED" not in tcp:
        return "it has no ESTABLISHED GitHub socket"
    return ""


# ----------------------------------------------------------------------- supervisor


@dataclasses.dataclass
class Forwarder:
    target: Target
    state: str = "starting"
    proc: Any = None
    pid_started: str = ""
    started_at: float = 0.0  # when the current process was started
    up_since: Optional[float] = None
    first_started_at: Optional[float] = None
    restarts: int = 0
    rotations: int = 0  # planned restarts (rotate_seconds)
    last_error: str = ""
    last_exit: Optional[int] = None
    last_event_at: Optional[float] = None
    last_catchup_at: Optional[float] = None
    hook_id: Optional[int] = None
    next_start_at: float = 0.0
    backoff_index: int = 0
    conflict_since: Optional[float] = None  # first 422 of the current streak
    stale_checked_at: float = 0.0
    suspect: str = ""  # the stall reason seen at the previous liveness check
    last_hook_check: float = 0.0
    rotate_at: Optional[float] = None  # planned restart of the current process (None: not planned)
    events: int = 0
    log_path: str = ""
    forced_index: int = 0  # backoff step of forced restarts (stall, hook gone) that repeat
    last_forced_at: Optional[float] = None
    down_logged_at: Optional[float] = None  # the last "target is down" log line
    not_up_since: Optional[float] = None  # when it was last seen not up (None while up)
    type_ok: bool = True  # org targets: the account type was read as Organization
    members: Dict[int, str] = dataclasses.field(default_factory=dict)  # other group pids -> start time


@dataclasses.dataclass
class PushSignals:
    established: List[Target] = dataclasses.field(default_factory=list)  # catch up on these now
    events: List[Event] = dataclasses.field(default_factory=list)  # workflow_job events, already routed


def _log_tail(path: str, limit: int = 16384) -> List[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    marker = text.rfind("--- tartci owners: forwarder start")
    return [line.strip() for line in text[marker if marker >= 0 else 0:].splitlines() if line.strip()]


def classify_exit(lines: Sequence[str]) -> Tuple[str, str]:
    """(kind, one-line error) of a forwarder's exit from its log: kind is "conflict" (HTTP 422, the
    hook already exists), "auth" (401/403/404: the forwarder login lacks access) or "exit"."""
    error = ""
    for index, line in enumerate(lines):
        if line.startswith("Error:"):
            error = line
            follow = lines[index + 1] if index + 1 < len(lines) else ""
            if follow and not follow.startswith("Usage:"):
                error = f"{line} {follow}"
    text = " ".join(lines)
    if "Hook already exists" in text or "HTTP 422" in error:
        return "conflict", mask(error or "HTTP 422: Hook already exists")[:300]
    if re.search(r"HTTP (401|403|404)", error):
        return "auth", mask(error)[:300]
    return "exit", mask(error or (lines[-1] if lines else "exited"))[:300]


class Supervisor:
    """Runs one forwarder per target and turns what happens to them into signals for the
    dispatcher: `established` (a forwarder just came up: scan its repositories once, since
    events from before are lost) and routed `workflow_job` events. Call `poll` every tick;
    it never raises for one forwarder's trouble."""

    def __init__(self, cfg: "od.Config", root: str, targets: Sequence[Target], *,
                 receiver: Receiver, token: Callable[[], str], api: Any,
                 spawn: Callable[..., Any] = od.spawn_process,
                 inspect: Callable[[Iterable[int]], Dict[int, Dict[str, Any]]] = inspect_groups,
                 identify: Callable[[Any], Optional[str]] = od.process_started_at,
                 kill: Optional[Callable[[int, int], None]] = None,
                 clock: Callable[[], float] = time.time,
                 home: str = os.path.expanduser("~"),
                 base_env: Optional[Dict[str, str]] = None,
                 binary: Optional[str] = None,
                 owner_type: Optional[Callable[[str], Optional[str]]] = None,
                 unverified: Iterable[str] = ()) -> None:
        self.cfg, self.root = cfg, root
        self.owner_type = owner_type
        self.receiver, self._token, self.api = receiver, token, api
        self.spawn, self.inspect, self.identify, self.clock = spawn, inspect, identify, clock
        self._kill = kill or _killpg
        self.home = home
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.binary = binary
        self.secret = receiver._secret.decode()
        self.forwarders: Dict[str, Forwarder] = {t.key: Forwarder(t) for t in targets}
        pending = {str(owner).lower() for owner in unverified}
        for fwd in self.forwarders.values():
            receiver.allow(fwd.target.key)
            fwd.log_path = os.path.join(root, "logs", "forwarders", f"{fwd.target.key}.log")
            if fwd.target.kind == "org" and fwd.target.name.lower() in pending:
                fwd.type_ok, fwd.state = False, "checking"  # never assume an organization
        self.hooks: Dict[str, List[int]] = {}
        self.stopping = False
        self.next_liveness = 0.0
        self.stats: Dict[str, int] = collections.Counter()
        self._load_hooks()

    # -- persistence
    @property
    def state_path(self) -> str:
        return os.path.join(self.root, "state", STATE_FILE)

    def _load_hooks(self) -> None:
        data = od.read_json(self.state_path)
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if isinstance(hooks, dict):
            for key, ids in hooks.items():
                if isinstance(ids, list):
                    self.hooks[str(key)] = [int(i) for i in ids if isinstance(i, int) and i > 0][-HOOKS_KEPT:]

    def save(self) -> None:
        live = {}
        for key, fwd in self.forwarders.items():
            if fwd.proc is not None:
                live[key] = {"pid": fwd.proc.pid, "pgid": _pgid(fwd.proc), "pid_started": fwd.pid_started,
                             "target": fwd.target.label,
                             "members": {str(pid): started for pid, started in sorted(fwd.members.items())}}
        try:
            od.ensure_private_dir(os.path.dirname(self.state_path))
            od.write_private_json(self.state_path, {"live": live, "hooks": self.hooks})
        except OSError:
            pass

    def _record_hook(self, key: str, hook_id: int) -> None:
        ids = [i for i in self.hooks.get(key, []) if i != hook_id] + [hook_id]
        self.hooks[key] = ids[-HOOKS_KEPT:]

    # -- process control
    def _start(self, fwd: Forwarder, now: float) -> None:
        try:
            token = self._token()
        except PushError as error:
            self._fail(fwd, now, "auth", mask(error))
            return
        argv = ([self.binary] if self.binary else [self.cfg.gh_cli, "webhook"]) + [
            "forward", *fwd.target.flag(), "--events", EVENTS, "--url", self.receiver.url(fwd.target.key),
            "--secret", self.secret]
        env = forwarder_env(self.base_env, token, self.cfg.forwarder_gh_config_dir)
        try:
            log_dir = os.path.dirname(fwd.log_path)
            od.ensure_private_dir(log_dir)
            try:
                if os.path.getsize(fwd.log_path) > LOG_MAX_BYTES:
                    os.unlink(fwd.log_path)
            except OSError:
                pass
            fd = os.open(fwd.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(f"--- tartci owners: forwarder start {time.strftime('%Y-%m-%dT%H:%M:%S')} "
                             f"{fwd.target.label}\n")
            proc = self.spawn(argv, env, fwd.log_path)
        except Exception as error:  # EMFILE, a missing gh...: back off, never crash the dispatcher
            self._fail(fwd, now, "exit", f"cannot start: {mask(error)}", quick=True)
            return
        if fwd.first_started_at is None:
            fwd.first_started_at = now
        elif fwd.state != "rotating":  # a planned restart is counted by `_rotate`, not as a restart
            fwd.restarts += 1
        fwd.proc, fwd.started_at, fwd.state = proc, now, "starting"
        fwd.up_since, fwd.suspect, fwd.last_exit, fwd.rotate_at = None, "", None, None
        fwd.pid_started = self.identify(proc.pid) or ""
        fwd.members = {}
        self.stats["starts"] += 1
        self.save()
        od.log_line(f"push: starting forwarder for {fwd.target.label} pid={proc.pid}")

    def _fail(self, fwd: Forwarder, now: float, kind: str, error: str, quick: Optional[bool] = None) -> None:
        """A start failed, or the process ended: schedule the next start. A quick exit (one that
        came within FAST_EXIT_SECONDS of the start, or of coming up: a forwarder that pings and
        dies again and again asks for a catch-up scan each time) climbs the backoff ladder, as
        do 422s and auth failures; only a forwarder that ran for a while comes straight back."""
        fwd.last_error = error
        if kind == "conflict":
            fwd.conflict_since = fwd.conflict_since or now
        else:
            fwd.conflict_since = None
        if quick is None:
            ran_from = fwd.up_since if fwd.up_since is not None else fwd.started_at
            quick = bool(fwd.started_at) and now - ran_from < FAST_EXIT_SECONDS
        if kind in ("conflict", "auth") or quick:
            delay = BACKOFF_STEPS[min(fwd.backoff_index, len(BACKOFF_STEPS) - 1)]
            fwd.backoff_index += 1
        else:  # a forwarder that ran (GitHub drops them every 1-2 h): straight back
            delay = RESTART_DELAY_SECONDS
            fwd.backoff_index = 0
        fwd.state = "backing-off"
        fwd.next_start_at = now + delay
        fwd.up_since = None
        od.log_line(f"push: forwarder for {fwd.target.label} {kind}: {error}; next start in {delay:.0f}s")
        if kind == "auth":
            self._log_down(fwd, now, error)

    def _watch_down(self, fwd: Forwarder, now: float) -> None:
        """Whatever keeps a target from coming up (422 for ever, a missing binary, exits), once it
        has not been up for DOWN_AFTER_SECONDS it is logged as down like a login error is."""
        if fwd.state in ("up", "stopped") or self.stopping:
            return
        if fwd.not_up_since is None:
            fwd.not_up_since = now
        elif now - fwd.not_up_since >= DOWN_AFTER_SECONDS:
            self._log_down(fwd, now, f"not up for {(now - fwd.not_up_since) / 60:.0f} min, {fwd.state}"
                           + (f": {fwd.last_error}" if fwd.last_error else ""))

    def _log_down(self, fwd: Forwarder, now: float, why: str) -> None:
        """The loud line for a target that cannot run, at most once per DOWN_LOG_EVERY_SECONDS:
        nothing is polled, so nothing of that target is served meanwhile."""
        if fwd.down_logged_at is not None and now - fwd.down_logged_at < DOWN_LOG_EVERY_SECONDS:
            return
        fwd.down_logged_at = now
        od.log_line(f"push: ERR forwarder for {fwd.target.label} is DOWN ({mask(why)}): NOTHING is served "
                    f"for {fwd.target.label} until this is fixed (see `tartci owners status`)")

    def _stop(self, fwd: Forwarder, wait: bool = True) -> None:
        """SIGCONT then SIGTERM the forwarder's whole group (SIGTERM to `gh` alone orphans its
        gh-webhook child), wait briefly, then SIGKILL the group."""
        proc = fwd.proc
        if proc is None:
            return
        pgid = _pgid(proc)
        for sig in (signal.SIGCONT, signal.SIGTERM):
            self._kill(pgid, sig)
        deadline = time.monotonic() + (STOP_GRACE_SECONDS if wait else 0.0)
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self._kill(pgid, signal.SIGKILL)
        if proc.poll() is None and hasattr(proc, "wait"):
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
        fwd.proc = None
        fwd.pid_started = ""

    def _restart(self, fwd: Forwarder, now: float, why: str, state: str = "stalled") -> None:
        """A forced restart (stalled, or its hook gone). One now and then comes back after
        RESTART_DELAY_SECONDS; forced restarts that repeat within FORCED_RESTART_WINDOW_SECONDS
        climb the backoff ladder, so a misfiring trigger cannot turn into a catch-up scan of the
        whole target every minute."""
        repeat = fwd.last_forced_at is not None and now - fwd.last_forced_at < FORCED_RESTART_WINDOW_SECONDS
        if repeat:
            delay = BACKOFF_STEPS[min(fwd.forced_index, len(BACKOFF_STEPS) - 1)]
            fwd.forced_index += 1
        else:
            delay, fwd.forced_index = RESTART_DELAY_SECONDS, 0
        fwd.last_forced_at = now
        od.log_line(f"push: restarting forwarder for {fwd.target.label}: {why}; next start in {delay:.0f}s")
        self._stop(fwd)
        fwd.last_error = why
        fwd.state, fwd.up_since = state, None
        fwd.next_start_at = now + delay
        self.stats["restarts_forced"] += 1
        self.save()

    def _rotate_at(self, fwd: Forwarder, now: float) -> Optional[float]:
        """When an up forwarder is restarted on purpose: every `rotate_seconds`, plus a fixed
        per-target stagger (up to 10% of the period, at most 5 min) so the targets do not all
        drop out and catch up in the same minute."""
        period = float(getattr(self.cfg, "rotate_seconds", 0) or 0)
        if period <= 0:
            return None
        span = min(ROTATE_STAGGER_MAX_SECONDS, period * 0.1)
        stagger = (zlib.crc32(fwd.target.key.encode()) % 1000) / 1000.0 * span
        return now + period + stagger

    def _rotate(self, fwd: Forwarder, now: float) -> None:
        """The planned restart: stop the group (GitHub removes its hook within ~4 s), start a new
        one after RESTART_DELAY_SECONDS; its ping marks it up again and asks for the catch-up
        scan, which serves whatever queued in between. Not an error, and no backoff."""
        od.log_line(f"push: rotating forwarder for {fwd.target.label} (planned, every "
                    f"{self.cfg.rotate_seconds}s); catching up once it is back")
        self._stop(fwd)
        fwd.state, fwd.up_since, fwd.rotate_at = "rotating", None, None  # the ladder was reset by staying up
        fwd.next_start_at = now + RESTART_DELAY_SECONDS
        fwd.rotations += 1
        self.stats["rotations"] += 1
        self.save()

    # -- the tick
    def poll(self) -> PushSignals:
        now = self.clock()
        signals = PushSignals()
        for event in self.receiver.drain():
            self._route(event, now, signals)
        for fwd in self.forwarders.values():
            try:
                self._step(fwd, now, signals)
                self._watch_down(fwd, now)
            except Exception as error:  # one forwarder's trouble never stops the others
                fwd.last_error = mask(f"{type(error).__name__}: {error}")
                od.log_line(f"push: ERR supervising {fwd.target.label}: {fwd.last_error}")
        if not self.stopping and now >= self.next_liveness:
            self.next_liveness = now + LIVENESS_EVERY_SECONDS
            try:
                self._liveness(now)
            except Exception as error:
                od.log_line(f"push: ERR liveness check: {mask(error)}")
        return signals

    def _route(self, event: Event, now: float, signals: PushSignals) -> None:
        fwd = self.forwarders.get(event.key)
        if fwd is None:
            return
        if event.event == "ping":
            if fwd.proc is None or fwd.proc.poll() is not None:
                return  # a late ping of a forwarder already gone
            if event.hook_id:
                fwd.hook_id = event.hook_id
                if event.hook_name in ("", "cli"):
                    self._record_hook(fwd.target.key, event.hook_id)
            self._established(fwd, now, signals, "ping")
            return
        if not fwd.target.covers(event.repo):
            self.stats["foreign"] += 1  # a repository this forwarder does not cover: dropped
            return
        fwd.last_event_at = now
        fwd.events += 1
        if fwd.state == "starting":  # an event proves the hook delivers
            self._established(fwd, now, signals, "event")
        signals.events.append(event)

    def _established(self, fwd: Forwarder, now: float, signals: PushSignals, how: str) -> None:
        if fwd.state == "up":
            return
        fwd.state, fwd.up_since = "up", now
        fwd.conflict_since, fwd.suspect = None, ""  # backoff_index resets once it STAYS up (_step)
        fwd.not_up_since, fwd.down_logged_at = None, None  # a later outage is logged at once
        fwd.last_hook_check = now
        fwd.rotate_at = self._rotate_at(fwd, now)
        if how != "ping":
            fwd.last_error = f"no ping within {PING_TIMEOUT_SECONDS:.0f}s; assumed up" if how == "timeout" else ""
        else:
            fwd.last_error = ""
        self.save()
        signals.established.append(fwd.target)
        od.log_line(f"push: forwarder for {fwd.target.label} is up ({how}, hook {fwd.hook_id or '?'}); catching up")

    def _step(self, fwd: Forwarder, now: float, signals: PushSignals) -> None:
        if fwd.proc is not None:
            code = fwd.proc.poll()
            if code is not None:
                self._stop(fwd, wait=False)  # the leader is gone; take any orphan in its group with it
                fwd.last_exit = int(code)
                kind, error = classify_exit(_log_tail(fwd.log_path))
                self._fail(fwd, now, kind, f"rc={code}: {error}")
                self.save()
                return
            if fwd.state == "starting" and now - fwd.started_at >= PING_TIMEOUT_SECONDS:
                self._established(fwd, now, signals, "timeout")
            if fwd.state == "up" and fwd.backoff_index and fwd.up_since is not None \
                    and now - fwd.up_since >= FAST_EXIT_SECONDS:
                fwd.backoff_index = 0  # it stayed up: the next exit is an ordinary one
            if (fwd.state == "up" and not self.stopping and fwd.rotate_at is not None
                    and now >= fwd.rotate_at):
                self._rotate(fwd, now)
                return
            if (fwd.state == "up" and fwd.hook_id and not self.stopping
                    and now - fwd.last_hook_check >= HOOK_CHECK_SECONDS):
                self._check_hook(fwd, now)
            return
        if self.stopping or fwd.state == "refused":
            return
        if not fwd.type_ok:
            if now >= fwd.next_start_at:
                self._check_type(fwd, now)
            if not fwd.type_ok:
                return
        if fwd.conflict_since is not None and now - fwd.conflict_since >= STALE_HOOK_AFTER_SECONDS \
                and now - fwd.stale_checked_at >= STALE_HOOK_RETRY_SECONDS:
            fwd.stale_checked_at = now
            self._delete_stale(fwd)
        if now >= fwd.next_start_at:
            self._start(fwd, now)

    def _check_type(self, fwd: Forwarder, now: float) -> None:
        """An org target whose account type could not be read at startup: read it (one REST call
        as the main login) before its forwarder may start, retrying on the backoff ladder. An
        Organization starts now; a personal account or any other type is refused (never assumed to be
        an org); only a failed read (or an answer without a type) is retried."""
        if self.owner_type is None:  # nothing can read it: never assumed to be an organization
            fwd.state = "refused"
            fwd.last_error = f"the account type of {fwd.target.name} is unknown and cannot be read"
            od.log_line(f"push: ERR {fwd.last_error}; its forwarder is not started")
            self._log_down(fwd, now, fwd.last_error)
            return
        try:
            kind = self.owner_type(fwd.target.name)
            error = "" if kind else "no account type in the answer"
        except Exception as caught:  # GhError, a timeout...: try again later
            kind, error = None, mask(caught)
        if kind == "Organization":
            fwd.type_ok, fwd.state, fwd.backoff_index, fwd.last_error = True, "starting", 0, ""
            fwd.next_start_at = now
            od.log_line(f"push: {fwd.target.name} is an organization; starting its forwarder")
            return
        if kind == "User":
            fwd.state = "refused"
            fwd.last_error = (f"{fwd.target.name} is a personal account: list its private "
                              "repositories in `repos` instead")
            od.log_line(f"push: ERR {fwd.last_error}; its forwarder is not started")
            self._log_down(fwd, now, fwd.last_error)
            return
        if kind:  # an answer, but no type an org forwarder can serve (a Bot, a Mannequin...): asking again cannot help
            fwd.state = "refused"
            fwd.last_error = f"{fwd.target.name} is an account of type {kind!r}, not an organization"
            od.log_line(f"push: ERR {fwd.last_error}; its forwarder is not started")
            self._log_down(fwd, now, fwd.last_error)
            return
        delay = BACKOFF_STEPS[min(fwd.backoff_index, len(BACKOFF_STEPS) - 1)]
        fwd.backoff_index += 1
        fwd.state, fwd.next_start_at = "checking", now + delay
        fwd.last_error = f"cannot read the account type of {fwd.target.name}: {error or kind}"
        od.log_line(f"push: {fwd.last_error}; its forwarder waits (not assumed to be an organization); "
                    f"next check in {delay:.0f}s")
        self._log_down(fwd, now, fwd.last_error)

    def _check_hook(self, fwd: Forwarder, now: float) -> None:
        """The rare REST check (every HOOK_CHECK_SECONDS, or on suspicion) that the hook of an up
        forwarder still exists. A 404 restarts it; a failed check changes nothing."""
        fwd.last_hook_check = now
        self.stats["hook_checks"] += 1
        try:
            hook = self.api.get_hook(fwd.target, int(fwd.hook_id or 0))
        except od.GhError as error:
            od.log_line(f"push: cannot check the hook of {fwd.target.label}: {mask(error)}")
            return
        if hook is None:
            self._restart(fwd, now, f"its hook {fwd.hook_id} is gone on GitHub")

    def _delete_stale(self, fwd: Forwarder) -> None:
        """After ~60 s of "Hook already exists": delete the conflicting hook ONLY when it is one this
        pool recorded (from a ping) for this target and GitHub still names it `cli`. A hook this pool
        never saw (someone's own `gh webhook forward`) is never touched: the target stays backing off."""
        recorded = list(reversed(self.hooks.get(fwd.target.key, [])))
        if not recorded:
            od.log_line(f"push: {fwd.target.label} has a conflicting hook this pool never recorded; not deleting it")
            return
        for hook_id in recorded:
            try:
                hook = self.api.get_hook(fwd.target, hook_id)
            except od.GhError as error:
                od.log_line(f"push: cannot read hook {hook_id} of {fwd.target.label}: {mask(error)}")
                continue
            if hook is None:
                self.hooks[fwd.target.key] = [i for i in self.hooks.get(fwd.target.key, []) if i != hook_id]
                continue
            if hook.get("name") != "cli":
                od.log_line(f"push: hook {hook_id} of {fwd.target.label} is not a `cli` hook; not deleting it")
                continue
            try:
                deleted = self.api.delete_hook(fwd.target, hook_id)
            except od.GhError as error:
                od.log_line(f"push: cannot delete hook {hook_id} of {fwd.target.label}: {mask(error)}")
                continue
            if deleted:
                self.stats["stale_deleted"] += 1
                self.hooks[fwd.target.key] = [i for i in self.hooks.get(fwd.target.key, []) if i != hook_id]
                od.log_line(f"push: deleted stale hook {hook_id} of {fwd.target.label} (recorded by this pool, name cli)")
                fwd.next_start_at = self.clock()  # try again now
        self.save()

    def _liveness(self, now: float) -> None:
        """Group members are recorded for every running forwarder (one that has not pinged yet
        too); the stall checks apply to an up one."""
        live = {key: fwd for key, fwd in self.forwarders.items() if fwd.proc is not None}
        if not live:
            return
        report = self.inspect([_pgid(fwd.proc) for fwd in live.values()])
        for fwd in live.values():
            info = report.get(_pgid(fwd.proc))
            if info is None or not info.get("alive"):
                continue  # the exit itself is collected by poll()
            self._note_members(fwd, info.get("pids") or [], info.get("started") or {})
            if fwd.state != "up":
                continue
            if info.get("tcp") is None and not info.get("stopped"):
                continue  # lsof could not say: neither a strike nor a clean bill
            reason = stall_reason(info)
            if info.get("stopped"):
                self._restart(fwd, now, reason)
            elif reason and fwd.suspect:
                self._restart(fwd, now, f"{reason} (twice in a row)")
            elif reason:
                fwd.suspect = reason  # once: maybe a socket being replaced; confirm next time, and ask GitHub
                if fwd.hook_id:
                    self._check_hook(fwd, now)
            else:
                fwd.suspect = ""

    def _note_members(self, fwd: Forwarder, pids: Iterable[int], started: Dict[int, str]) -> None:
        """Record the start time of each other member of the forwarder's group (the gh-webhook
        child of `gh webhook forward`), so `stop_recorded` can stop an orphaned child whose
        `gh` parent died along with the dispatcher. The start time comes from the same `ps`
        reading that put the pid in the group; a pid without one is not recorded, so it is
        never signalled."""
        leader = fwd.proc.pid
        current = {int(pid) for pid in pids if int(pid) != leader}
        members = {pid: when for pid, when in fwd.members.items() if pid in current}
        for pid in sorted(current - set(members)):
            when = started.get(pid)
            if when:
                members[pid] = when
        if members != fwd.members:
            fwd.members = members
            self.save()

    # -- queries
    def note_catchup(self, target: Target, when: float) -> None:
        fwd = self.forwarders.get(target.key)
        if fwd is not None:
            fwd.last_catchup_at = when

    def covering(self, full: str) -> Optional[Target]:
        for fwd in self.forwarders.values():
            if fwd.target.covers(full):
                return fwd.target
        return None

    def shutdown(self) -> None:
        """Stop every forwarder (their hooks disappear within seconds) and the receiver."""
        self.stopping = True
        for fwd in self.forwarders.values():
            if fwd.proc is not None:
                self._stop(fwd)
            fwd.state = "stopped"
        self.save()
        self.receiver.close()

    def summary(self) -> Dict[str, Any]:
        now = self.clock()
        rows = []
        for fwd in self.forwarders.values():
            rows.append({
                "target": fwd.target.label, "kind": fwd.target.kind, "name": fwd.target.name,
                "state": fwd.state, "pid": fwd.proc.pid if fwd.proc is not None else None,
                "uptime_s": int(now - fwd.up_since) if fwd.up_since is not None else None,
                "restarts": fwd.restarts, "rotations": fwd.rotations, "last_error": mask(fwd.last_error) if fwd.last_error else "",
                "last_exit": fwd.last_exit, "last_event_at": fwd.last_event_at,
                "last_catchup_at": fwd.last_catchup_at, "hook_id": fwd.hook_id, "events": fwd.events,
                "retry_in_s": int(max(0.0, fwd.next_start_at - now))
                if fwd.proc is None and fwd.state not in ("refused", "stopped") else None,
                "rotate_in_s": int(max(0.0, fwd.rotate_at - now))
                if fwd.state == "up" and fwd.rotate_at is not None else None,
            })
        return {"receiver": f"127.0.0.1:{self.receiver.port}", "forwarders": rows,
                "mode": "gh-webhook" if self.binary else "gh webhook forward",
                "receiver_stats": dict(self.receiver.stats), "stats": dict(self.stats),
                "hook_api_requests": getattr(self.api, "requests", 0)}


def _pgid(proc: Any) -> int:
    return int(getattr(proc, "pgid", None) or proc.pid)


def _killpg(pgid: int, sig: int) -> None:
    """Signal a forwarder's process group, never our own (or init's)."""
    try:
        if pgid > 1 and pgid != os.getpgrp():
            os.killpg(pgid, sig)
    except (OSError, ProcessLookupError):
        pass


def _killpid(pid: int, sig: int) -> None:
    """Signal one verified process, never ourselves (or init)."""
    try:
        if pid > 1 and pid != os.getpid():
            os.kill(pid, sig)
    except (OSError, ProcessLookupError):
        pass


def _stop_one(pid: int, started: str, alive: Callable[[Any, str], bool], kill: Callable[[int, int], None],
              sleep: Callable[[float], None]) -> None:
    for sig in (signal.SIGCONT, signal.SIGTERM):
        kill(pid, sig)
    for _ in range(30):
        if not alive(pid, started):
            break
        sleep(0.1)
    if alive(pid, started):
        kill(pid, signal.SIGKILL)


def stop_recorded(root: str, log: Callable[[str], None] = lambda message: None,
                  alive: Callable[[Any, str], bool] = od.same_process,
                  kill: Callable[[int, int], None] = _killpg,
                  sleep: Callable[[float], None] = time.sleep,
                  kill_pid: Callable[[int, int], None] = _killpid) -> List[str]:
    """Stop forwarders a previous dispatcher left running (it crashed, or was SIGKILLed after
    launchd's 60 s exit timeout): every recorded forwarder whose pid AND start time still match
    (its whole group is signalled). When the leader is gone (`gh` died, its gh-webhook child
    lives on holding the hook), each recorded group member whose pid AND start time still match
    is stopped one by one. A pid that cannot be verified is never signalled. Returns what was
    stopped."""
    path = os.path.join(root, "state", STATE_FILE)
    data = od.read_json(path)
    live = data.get("live") if isinstance(data, dict) else None
    stopped: List[str] = []
    if not isinstance(live, dict):
        return stopped
    for key, rec in sorted(live.items()):
        if not isinstance(rec, dict):
            continue
        pid, started = rec.get("pid"), str(rec.get("pid_started") or "")
        if not pid or not started or not alive(pid, started):
            members = rec.get("members") if isinstance(rec.get("members"), dict) else {}
            orphans = [(int(member), str(when)) for member, when in sorted(members.items())
                       if str(member).isdigit() and int(member) > 1 and when and alive(int(member), str(when))]
            for member, when in orphans:
                _stop_one(member, when, alive, kill_pid, sleep)
            if orphans:
                listed = ", ".join(str(member) for member, _ in orphans)
                stopped.append(f"{rec.get('target') or key} (orphaned pid {listed})")
                log(f"push: stopped an orphaned forwarder child of {rec.get('target') or key} (pid {listed})")
            continue
        pgid = int(rec.get("pgid") or pid)
        for sig in (signal.SIGCONT, signal.SIGTERM):
            kill(pgid, sig)
        for _ in range(30):
            if not alive(pid, started):
                break
            sleep(0.1)
        kill(pgid, signal.SIGKILL)
        stopped.append(f"{rec.get('target') or key} (pid {pid})")
        log(f"push: stopped a leftover forwarder for {rec.get('target') or key} (pid {pid})")
    if isinstance(data, dict):
        data["live"] = {}
        try:
            od.write_private_json(path, data)
        except OSError:
            pass
    return stopped


def build(cfg: "od.Config", root: str, home: Optional[str] = None,
          base_env: Optional[Dict[str, str]] = None, client: Any = None,
          unverified: Iterable[str] = ()) -> Supervisor:
    """The real supervisor for `tartci owners run`: a started receiver, the forwarder login, the
    gh-webhook binary when installed. `cmd_run` stops a previous run's orphans (`stop_recorded`)
    before anything else; `unverified` owners (type unreadable at startup) wait for `client` to
    read their type before their org forwarder starts."""
    home = home or os.path.expanduser("~")
    env = dict(os.environ if base_env is None else base_env)
    receiver = Receiver(secrets.token_hex(32), port=cfg.forwarder_port)
    receiver.start()
    config_dir = cfg.forwarder_gh_config_dir

    def token() -> str:  # re-read at each start, so a later login fixes a running pool
        return read_forwarder_token(config_dir)

    api = HookApi(cfg.gh_cli, token, config_dir, base_env=env)
    return Supervisor(cfg, root, targets_for(cfg), receiver=receiver, token=token, api=api,
                      home=home, base_env=env, binary=forwarder_binary(home, env),
                      owner_type=owner_type_reader(client) if client is not None else None,
                      unverified=unverified)
