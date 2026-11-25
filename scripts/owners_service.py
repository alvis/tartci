#!/usr/bin/env python3
"""tartci owners install / uninstall: the per-user LaunchAgent that runs the dispatcher
(see docs/owner-pool.md, "Service" and "Security: pinned, no self-update").

    tartci owners install [--rev REV] [--config PATH] [--dry-run]
    tartci owners uninstall [--purge]

What `install` does, in order (and what `--dry-run` only prints):

  1. Refuses unless the config parses, `tart` and `gh` are on PATH, and `gh auth status`
     passes under the scrubbed environment the dispatcher and its children use.
  2. Resolves REV (default `HEAD`) to a full commit of the checkout it runs from, and exports
     that commit's tree with `git archive` into ~/.tartci/owners/app/<full sha>/ (a 0700
     directory of read-only files) with a COMMIT file. The working copy is never exported:
     under jj, `HEAD` is the parent of the working-copy change `@`, so edits that are not yet
     committed are warned about and left out. An existing export of the same commit is reused.
  3. Renders launchd/com.danielraffel.tartci.owners.plist.template with absolute paths: the
     plist runs the export by its full-sha path, not through `current`, so a link flipped under
     a running dispatcher can never mix two commits. Updating is `install --rev NEW`.
  4. `launchctl bootout` any old instance (waiting until it is gone: its children get up to
     150 s to delete their VMs), writes the plist (0644), points `current` at the export (and
     `previous` at the commit it replaces), `launchctl bootstrap`s and kickstarts it, and reports the
     job's state.
  5. Only after a successful bootstrap are older exports pruned, and never the one the installed
     plist runs: an install that fails part way leaves every export the loaded agent may run.

`uninstall` boots the job out (SIGTERM; waits for the dispatcher to exit), checks that no
`tartci-owners-*` process or VM is left (it reports them and never deletes a VM itself), and
removes the plist. State and logs stay unless `--purge`; goldens, the config and the VM key
are never touched.
"""
from __future__ import annotations

import dataclasses
import glob
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import owners_dispatch as od
import python_child
from render_launchd_template import replace_strings

LABEL = "com.danielraffel.tartci.owners"
TEMPLATE_RELATIVE = os.path.join("launchd", f"{LABEL}.plist.template")
KEEP_EXPORTS = 2  # the installed commit and the one it replaced
SYSTEM_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
OPTIONAL_TOOLS = ("sshpass", "jq", "gtimeout")  # the lanes use them when present
# launchd waits ExitTimeOut (the template's) before SIGKILL; wait for the job to be gone a bit longer.
UNLOAD_WAIT_SECONDS = od.CHILD_TERM_GRACE_SECONDS + 90
LOADED_WAIT_SECONDS = 20.0
POOL_SUBDIRS = ("app", "state", "logs", "reap", "cache", "fetchcontent-src")  # what --purge removes (never the root)
VM_LEFTOVER = re.compile(r"^(?:tartci-owners-\S+|linux-ephr-\d+-\d+)$")  # candidates; Linux ones need a recorded pid
LINUX_VM = re.compile(r"^linux-ephr-(\d+)-\d+$")
CHILD_VM_IN_COMMAND = re.compile(r"\btartci-owners-[mxa]\d+")  # `tart run tartci-owners-m123451-4242-1`
_REV_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@^~{}-]*$")


class ServiceError(Exception):
    """A refusal or failure with a message fit for the operator; `code` is the exit status."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


Runner = Callable[..., Any]


@dataclasses.dataclass
class Layout:
    home: str
    root: str  # the pool root, ~/.tartci/owners

    @property
    def app_dir(self) -> str:
        return os.path.join(self.root, "app")

    @property
    def current(self) -> str:
        return os.path.join(self.app_dir, "current")

    @property
    def previous(self) -> str:
        return os.path.join(self.app_dir, "previous")

    @property
    def plist(self) -> str:
        return os.path.join(self.home, "Library", "LaunchAgents", f"{LABEL}.plist")

    @property
    def log_dir(self) -> str:
        return os.path.join(self.home, "Library", "Logs", "tartci")

    def export_dir(self, sha: str) -> str:
        return os.path.join(self.app_dir, sha)


# ------------------------------------------------------------------------------ rendering


def path_dirs(gh_cli: str, which: Callable[[str], Optional[str]]) -> List[str]:
    """The non-system PATH directories the agent needs: the absolute directories of `tart` and
    `gh` (and of the optional tools the lanes use; Python is named by TARTCI_PYTHON). The template appends the system
    directories. launchd's default PATH has none of the Homebrew ones, and a bare `tart` must
    resolve for the dispatcher and every child."""
    found: List[str] = []
    for tool in ("tart", gh_cli):
        located = which(tool)
        if not located:
            raise ServiceError(f"`{tool}` is not on PATH: the pool cannot run without it")
        found.append(os.path.dirname(os.path.abspath(located)))
    for tool in OPTIONAL_TOOLS:
        located = which(tool)
        if located:
            found.append(os.path.dirname(os.path.abspath(located)))
    ordered: List[str] = []
    for directory in found:
        if directory not in ordered and directory not in SYSTEM_PATH_DIRS:
            ordered.append(directory)
    for directory in ordered:
        if not directory.startswith("/") or ":" in directory:
            raise ServiceError(f"cannot put {directory!r} on the agent's PATH")
    return ordered or ["/usr/local/bin"]  # the template appends the system dirs; never an empty entry


def render_plist(template: bytes, values: Dict[str, str]) -> bytes:
    """The agent's plist: the template with `values` substituted (the repo's renderer), refusing a
    value that would be substituted again or leave a placeholder behind."""
    for name, value in values.items():
        if "$" in value or "\n" in value or "\0" in value or not value:
            raise ServiceError(f"cannot put {value!r} in the plist ({name}): it is empty or has a '$', newline or NUL")
    source = re.sub(rb"<!--.*?-->", b"", template, flags=re.DOTALL)
    rendered = replace_strings(plistlib.loads(source), values)

    def leftovers(node: Any) -> List[str]:
        if isinstance(node, str):
            return re.findall(r"\$[A-Z]+", node)
        if isinstance(node, list):
            return [hit for item in node for hit in leftovers(item)]
        if isinstance(node, dict):
            return [hit for item in node.values() for hit in leftovers(item)]
        return []

    left = leftovers(rendered)
    if left:
        raise ServiceError(f"the plist template has unfilled placeholders: {sorted(set(left))}")
    return plistlib.dumps(rendered, sort_keys=False)


# -------------------------------------------------------------------------------- git export


def resolve_commit(checkout: str, rev: str, run: Runner) -> Tuple[str, str]:
    """(full sha, subject) of `rev` in `checkout`."""
    if not _REV_OK.match(rev):
        raise ServiceError(f"not a revision: {rev!r}")
    proc = run(["git", "-C", checkout, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
               capture_output=True, text=True, timeout=30)
    sha = (proc.stdout or "").strip()
    if proc.returncode != 0 or not re.match(r"^[0-9a-f]{40}$", sha):
        raise ServiceError(f"cannot resolve {rev!r} to a commit in {checkout}")
    subject = run(["git", "-C", checkout, "log", "-1", "--format=%s", sha],
                  capture_output=True, text=True, timeout=30)
    return sha, (subject.stdout or "").strip()


def uncommitted_changes(checkout: str, run: Runner) -> List[str]:
    """Paths the working copy has changed, added or left untracked relative to `HEAD`: none of
    them is part of an export of `HEAD`."""
    proc = run(["git", "-C", checkout, "status", "--porcelain"], capture_output=True, text=True, timeout=60)
    return [line[3:] for line in (proc.stdout or "").splitlines() if line.strip()]


def template_at(checkout: str, sha: str, run: Runner) -> bytes:
    """The plist template as the pinned commit has it (never the working copy's)."""
    proc = run(["git", "-C", checkout, "show", f"{sha}:{TEMPLATE_RELATIVE}"], capture_output=True, timeout=30)
    if proc.returncode != 0 or not proc.stdout:
        raise ServiceError(f"commit {sha[:12]} has no {TEMPLATE_RELATIVE}: it predates `tartci owners install`")
    return proc.stdout if isinstance(proc.stdout, bytes) else str(proc.stdout).encode("utf-8")


def _writable(path: str) -> None:
    os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)


def remove_tree(path: str) -> None:
    """Delete a tree the export made read-only (never follows a symlink)."""
    if os.path.islink(path):
        os.unlink(path)
        return
    if not os.path.isdir(path):
        if os.path.lexists(path):
            os.unlink(path)
        return
    for current, dirs, _files in os.walk(path):
        _writable(current)
        for name in dirs:
            child = os.path.join(current, name)
            if not os.path.islink(child):
                _writable(child)
    shutil.rmtree(path)


def _safe_extract(archive: str, dest: str) -> None:
    """Extract a `git archive` tar into `dest`: regular files and directories only, nothing
    outside `dest`. Files become 0444 (0555 when executable); directories 0700."""
    root = os.path.realpath(dest)
    with tarfile.open(archive) as tar:
        for member in tar:
            if member.isdir() and member.name in ("", "."):
                continue
            target = os.path.realpath(os.path.join(dest, member.name))
            if target != root and not target.startswith(root + os.sep):
                raise ServiceError(f"unsafe path in the archive: {member.name!r}")
            if member.isdir():
                os.makedirs(target, mode=0o700, exist_ok=True)
            elif member.isreg():
                os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise ServiceError(f"unreadable archive member: {member.name!r}")
                with open(target, "wb") as out:
                    shutil.copyfileobj(source, out)
                os.chmod(target, 0o555 if member.mode & 0o111 else 0o444)
            else:
                raise ServiceError(f"unsupported entry in the archive: {member.name!r} (links are not exported)")
    for current, dirs, _files in os.walk(dest):
        for name in dirs:
            os.chmod(os.path.join(current, name), 0o700)
    os.chmod(dest, 0o700)


def export_commit(layout: Layout, checkout: str, sha: str, run: Runner) -> str:
    """Export `sha` to app/<sha>/ unless that exact export exists; returns its path."""
    final = layout.export_dir(sha)
    if os.path.isdir(final) and not os.path.islink(final):
        if od.recorded_commit(final) == sha:
            return final
        raise ServiceError(f"{final} exists but does not record commit {sha}: remove it and install again")
    od.ensure_private_dir(layout.root)
    od.ensure_private_dir(layout.app_dir)
    staging = tempfile.mkdtemp(prefix=f".{sha[:12]}.", suffix=".tmp", dir=layout.app_dir)
    archive = os.path.join(staging, "export.tar")
    tree = os.path.join(staging, "tree")
    try:
        proc = run(["git", "-C", checkout, "archive", "--format=tar", "-o", archive, sha],
                   capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise ServiceError(f"git archive {sha[:12]} failed: {(proc.stderr or '').strip()[:200]}")
        os.mkdir(tree, 0o700)
        _safe_extract(archive, tree)
        if not os.path.isfile(os.path.join(tree, "tartci")) or not os.path.isfile(
                os.path.join(tree, "scripts", "owners_dispatch.py")):
            raise ServiceError(f"commit {sha[:12]} has no tartci owners dispatcher (scripts/owners_dispatch.py)")
        with open(os.path.join(tree, od.COMMIT_FILE), "w", encoding="utf-8") as handle:
            handle.write(sha + "\n")
        os.chmod(os.path.join(tree, od.COMMIT_FILE), 0o444)
        os.rename(tree, final)
        return final
    finally:
        remove_tree(staging)


def _flip(link: str, target: str) -> None:
    tmp = f"{link}.{os.getpid()}.tmp"
    try:
        os.unlink(tmp)
    except OSError:
        pass
    os.symlink(target, tmp)
    os.replace(tmp, link)  # atomic, replaces a symlink in place


def point_current(layout: Layout, sha: str) -> None:
    """Make `current` name `sha`, keeping the commit it replaced as `previous`. Flips links only:
    it never deletes an export (`prune_exports` does, after a successful bootstrap). A re-install of
    the current commit changes nothing."""
    old = os.path.basename(os.readlink(layout.current)) if os.path.islink(layout.current) else ""
    if old and old != sha and os.path.isdir(layout.export_dir(old)):
        _flip(layout.previous, old)
    _flip(layout.current, sha)


def prune_exports(layout: Layout) -> List[str]:
    """Remove every export that is not `current`, `previous` or the commit the installed plist runs
    (read from the plist itself, so an install that failed before replacing it can never get the
    export the loaded agent still runs pruned). Returns the pruned commits."""
    keep = set()
    for link in (layout.current, layout.previous):
        if os.path.islink(link):
            keep.add(os.path.basename(os.readlink(link)))
    running = plist_commit(layout)
    if running:
        keep.add(running)
    pruned: List[str] = []
    for path in sorted(glob.glob(os.path.join(layout.app_dir, "*"))):
        name = os.path.basename(path)
        if name in ("current", "previous") or name in keep or not re.match(r"^[0-9a-f]{40}$", name):
            continue
        remove_tree(path)
        pruned.append(name)
    return pruned


# ----------------------------------------------------------------------------------- launchd


class Launchd:
    def __init__(self, uid: int, run: Runner = subprocess.run, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time) -> None:
        self.uid, self.run, self.sleep, self.clock = uid, run, sleep, clock

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{LABEL}"

    def _launchctl(self, *args: str) -> Any:
        return self.run(["launchctl", *args], capture_output=True, text=True, timeout=60)

    def info(self) -> Optional[Dict[str, str]]:
        """`state`, `pid`, `last exit code`, `runs` of the loaded job, or None when it is not loaded."""
        proc = self._launchctl("print", self.target)
        if proc.returncode != 0:
            return None
        info: Dict[str, str] = {}
        for line in (proc.stdout or "").splitlines():
            match = re.match(r"^\t(state|pid|last exit code|runs|exit timeout) = (.+)$", line)
            if match and match.group(1) not in info:
                info[match.group(1)] = match.group(2).strip()
        info.setdefault("state", "loaded")
        return info

    def bootout(self, log: Callable[[str], None]) -> bool:
        """Boot the job out and wait until launchd no longer has it (the dispatcher stops its
        children first). False when it is still there after UNLOAD_WAIT_SECONDS."""
        if self.info() is None:
            return True
        proc = self._launchctl("bootout", self.target)
        if proc.returncode != 0 and self.info() is not None:
            detail = ((proc.stderr or proc.stdout or "").strip().splitlines() or ["no output"])[0][:200]
            log(f"launchctl bootout reported: {detail}")
        deadline = self.clock() + UNLOAD_WAIT_SECONDS
        announced = False
        while self.info() is not None:
            if self.clock() >= deadline:
                return False
            if not announced:
                log("waiting for the dispatcher to stop its children and exit (up to "
                    f"{od.CHILD_TERM_GRACE_SECONDS} s)...")
                announced = True
            self.sleep(2.0)
        return True

    def bootstrap(self, plist: str, log: Callable[[str], None]) -> None:
        proc = self._launchctl("bootstrap", self.domain, plist)
        if proc.returncode != 0:
            detail = ((proc.stderr or proc.stdout or "").strip().splitlines() or ["no output"])[0][:200]
            raise ServiceError(f"launchctl bootstrap {self.domain} failed (rc={proc.returncode}): {detail}")
        # A RunAtLoad launch is speculative and launchd can defer it indefinitely on a busy host;
        # kickstart makes the first run on-demand. A failed kickstart leaves wait_running to report it.
        try:
            self._launchctl("kickstart", self.target)
        except (OSError, subprocess.SubprocessError) as error:
            log(f"launchctl kickstart failed ({type(error).__name__}); waiting for launchd to start it")

    def wait_running(self) -> Optional[Dict[str, str]]:
        deadline = self.clock() + LOADED_WAIT_SECONDS
        info = self.info()
        while (info is None or info.get("state") != "running") and self.clock() < deadline:
            self.sleep(1.0)
            info = self.info()
        return info


# ------------------------------------------------------------------------------------ install


def _log(message: str) -> None:
    print(message, flush=True)


def install(layout: Layout, checkout: str, config_path: str, rev: str, dry_run: bool, *,
            run: Runner = subprocess.run, which: Callable[[str], Optional[str]] = shutil.which,
            python: Optional[str] = None, uid: Optional[int] = None,
            sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
            log: Callable[[str], None] = _log,
            load_config: Callable[[str], Any] = od.load_config,
            forwarder_login: Optional[Callable[[str], Any]] = None) -> int:
    uid = os.getuid() if uid is None else uid
    if uid == 0:
        raise ServiceError("run `tartci owners install` as the user that owns the pool, not as root")
    python = python or python_child.child_python()
    config = os.path.abspath(os.path.expanduser(config_path))
    try:
        cfg = load_config(config)
    except od.ConfigError as error:
        raise ServiceError(f"refusing to install: {error}", code=2)
    # `run` exits 2 without it, and KeepAlive would restart it every ThrottleInterval for ever.
    import owners_push  # noqa: PLC0415

    try:
        (forwarder_login or owners_push.read_forwarder_token)(cfg.forwarder_gh_config_dir)
    except owners_push.PushError as error:
        raise ServiceError(f"refusing to install: the pool needs the forwarder login ({error}). Log it in "
                           "as step 3 of docs/owner-pool.md (\"The forwarder login\") shows", code=2)
    if not os.path.lexists(os.path.join(checkout, ".git")):
        raise ServiceError(f"{checkout} is not a git checkout: install from the checkout, which is what it exports")
    dirs = path_dirs(cfg.gh_cli, which)
    problem = od.check_gh_auth(cfg.gh_cli, od.gh_env(), run=run)  # the scrubbed environment the pool runs under
    if problem:
        raise ServiceError(f"refusing to install: {problem}")
    sha, subject = resolve_commit(checkout, rev, run)
    head, _ = resolve_commit(checkout, "HEAD", run)
    dirty = uncommitted_changes(checkout, run) if head == sha else []
    app = layout.export_dir(sha)
    plist = render_plist(template_at(checkout, sha, run), {
        "HOME": layout.home, "APP": app, "CONFIG": config, "ROOT": layout.root,
        "PATHDIRS": ":".join(dirs), "PYTHON": python,
    })
    log(f"commit   {sha} {subject}")
    if dirty:
        log(f"warning  the working copy has {len(dirty)} uncommitted change(s) (e.g. {dirty[0]}); they are NOT part "
            f"of the pinned export of {sha[:12]}. Commit them first to run them")
    log(f"config   {config} ({len(cfg.owners)} owner(s), {len(cfg.repos)} extra repo(s), max_jobs={cfg.max_jobs})")
    if dry_run:
        log("dry-run: nothing is written, exported, or loaded. It would:")
        log(f"  1. export commit {sha[:12]} to {app} (0700 dir, read-only files, COMMIT file)")
        log(f"  2. launchctl bootout {LABEL} if loaded, wait until it is gone")
        log(f"  3. write {layout.plist} (0644), point {layout.current} at the export (the replaced one stays as "
            "`previous`) and `launchctl bootstrap` + `kickstart` it")
        log("  4. after a successful bootstrap prune older exports (never the one the plist runs)")
        log("plist:")
        log(plist.decode("utf-8").rstrip())
        return 0
    launchd = Launchd(uid, run=run, sleep=sleep, clock=clock)
    before = od.read_json(os.path.join(layout.root, "state", "dispatcher.json"))
    export_commit(layout, checkout, sha, run)
    od.ensure_private_dir(layout.root)
    os.makedirs(layout.log_dir, exist_ok=True)
    os.makedirs(os.path.dirname(layout.plist), exist_ok=True)
    if isinstance(before, dict) and before.get("running") and before.get("children"):
        log(f"note     {len(before['children'])} child(ren) are running; stopping the old dispatcher stops them "
            "(their jobs fail and can be re-run)")
    tmp = f"{layout.plist}.{os.getpid()}.tmp"
    with open(tmp, "wb") as handle:
        handle.write(plist)
    os.chmod(tmp, 0o644)
    try:  # validate before the old agent is stopped
        lint = run(["plutil", "-lint", tmp], capture_output=True, text=True, timeout=30)
        if lint.returncode != 0:
            raise ServiceError(f"plutil rejects the rendered plist: {(lint.stdout or lint.stderr or '').strip()[:200]}")
        if not launchd.bootout(log):
            raise ServiceError(f"the old {LABEL} is still loaded after {UNLOAD_WAIT_SECONDS:.0f} s; "
                               f"not replacing it (see `launchctl print {launchd.target}`)")
        os.replace(tmp, layout.plist)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    point_current(layout, sha)  # links follow the plist, so a failed install leaves them on what still runs
    launchd.bootstrap(layout.plist, log)
    for name in prune_exports(layout):  # only now, and never the plist's own commit
        log(f"pruned   old export {name[:12]}")
    info = launchd.wait_running()
    log(f"plist    {layout.plist}")
    log(f"logs     {layout.log_dir}/owners.out.log and owners.err.log")
    if info is None:
        raise ServiceError(f"{LABEL} is not loaded after bootstrap; see `launchctl print {launchd.target}`")
    summary = ", ".join(f"{key}={value}" for key, value in info.items())
    log(f"launchd  {launchd.target}: {summary}")
    if info.get("state") != "running":
        raise ServiceError(f"{LABEL} loaded but is not running ({summary}); see the logs above")
    log(f"installed commit {sha[:12]}; `tartci owners status` shows it")
    return 0


# ---------------------------------------------------------------------------------- uninstall


def leftover_processes(layout: Layout, run: Runner = subprocess.run,
                       alive: Callable[..., bool] = od.pid_alive) -> List[str]:
    """Processes of pool children that are still running: those the state dirs record with a
    matching start time, and any process whose command line names a `tartci-owners-` VM."""
    found: List[str] = []
    for meta_path in sorted(glob.glob(os.path.join(layout.root, "state", od.CHILD_PREFIX + "*", od.CHILD_META))):
        meta = od.read_json(meta_path)
        if (isinstance(meta, dict) and not meta.get("ended") and meta.get("pid")
                and alive(meta["pid"], str(meta.get("pid_started") or ""))):
            found.append(f"pid {meta['pid']} ({meta.get('name')})")
    proc = run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=30,
               env=od.stable_time_env())
    own = str(os.getpid())
    for line in (proc.stdout or "").splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid != own and CHILD_VM_IN_COMMAND.search(command) and "owners uninstall" not in command:
            entry = f"pid {pid} ({command.strip()[:100]})"
            if not any(entry.startswith(f"pid {pid} ") for entry in found):
                found.append(entry)
    return found


def pool_child_pids(layout: Layout) -> set:
    """Pids of every child the dispatcher recorded at spawn (state/tartci-owners-*/child.json, ended
    or not). A Linux child's VM is `linux-ephr-<its pid>-<n>`, the name every tartci Linux lane uses,
    so the pid is what tells a pool VM from a native lane's."""
    pids = set()
    for meta_path in glob.glob(os.path.join(layout.root, "state", od.CHILD_PREFIX + "*", od.CHILD_META)):
        meta = od.read_json(meta_path)
        if isinstance(meta, dict) and isinstance(meta.get("pid"), int) and meta["pid"] > 0:
            pids.add(str(meta["pid"]))
    return pids


def leftover_vms(layout: Layout, run: Runner = subprocess.run) -> List[str]:
    """Pool VMs still in Tart's local store: `tartci-owners-*` clones, and `linux-ephr-<pid>-<n>` Linux
    clones whose <pid> is a child this pool recorded. Never goldens or base images, and never another
    lane's `linux-ephr-*` VM that merely shares TART_HOME. (A Linux VM of a child whose state was
    already purged, or dropped by state retention, cannot be attributed: it is neither listed here nor
    reaped by `tartci owners run`, so check `tart list` by hand.)"""
    env = od.scrub_env(dict(os.environ))
    env["TART_HOME"] = os.path.join(layout.home, ".tart")
    proc = run(["tart", "list", "--source", "local", "--format", "json"], capture_output=True, text=True,
               timeout=120, env=env)
    try:
        listing = json.loads(proc.stdout or "[]")
    except ValueError:
        raise ServiceError("cannot read `tart list`: cannot confirm that no pool VM is left")
    pids = pool_child_pids(layout)
    found: List[str] = []
    for item in listing:
        name = str(item.get("Name") or "") if isinstance(item, dict) else ""
        if not VM_LEFTOVER.match(name):
            continue
        linux = LINUX_VM.match(name)
        if linux and linux.group(1) not in pids:
            continue
        found.append(name)
    return sorted(found)


POOL_MARKERS = (os.path.join("state", "dispatcher.json"), os.path.join("app", "current"))  # what the pool itself writes


def purgeable_root(layout: Layout) -> Optional[str]:
    """The real path of the pool root if `--purge` may remove things under it, None if there is
    nothing there. Raises ServiceError for a root that is not provably a pool's: `--root` and
    TARTCI_OWNERS_ROOT are the operator's input, and a wrong one must never reach rmtree. It must
    be a directory strictly below HOME (never HOME or ~/.tartci itself) that holds a marker the pool
    writes: the dispatcher's status file (state/dispatcher.json) or an installed export (app/current)."""
    home = os.path.realpath(layout.home)
    root = os.path.realpath(layout.root)
    # macOS volumes are usually case-insensitive and realpath keeps the caller's case, so
    # ~/.TARTCI is ~/.tartci there: compare case-folded on darwin.
    fold = (lambda path: path.casefold()) if sys.platform == "darwin" else (lambda path: path)
    if fold(root) in {fold(os.sep), fold(home), fold(os.path.join(home, ".tartci"))} \
            or not fold(root).startswith(fold(home + os.sep)):
        raise ServiceError(f"refusing to purge {layout.root}: it is not a pool root below {layout.home} "
                           "(never HOME or ~/.tartci itself)")
    if not os.path.lexists(root):
        return None
    if not any(os.path.lexists(os.path.join(root, marker)) for marker in POOL_MARKERS) \
            and not _has_export(root):
        raise ServiceError(f"refusing to purge {layout.root}: it has no {POOL_MARKERS[0]}, {POOL_MARKERS[1]} "
                           "or exported commit, so it does not look like an owner pool's root. "
                           "Check --root / TARTCI_OWNERS_ROOT")
    return root


def _has_export(root: str) -> bool:
    """An app/<40-hex sha>/COMMIT file: what a first install that failed before `current` leaves."""
    return any(re.fullmatch(r"[0-9a-f]{40}", os.path.basename(os.path.dirname(path)))
               for path in glob.glob(os.path.join(root, "app", "*", od.COMMIT_FILE)))


def purge(layout: Layout, log: Callable[[str], None] = _log) -> None:
    """Remove the pool's own app/state/logs/... under its root, and its launchd logs. Never the
    root itself (unless it is left empty), goldens, the config or the VM key."""
    if purgeable_root(layout) is None:
        log(f"nothing to purge: {layout.root} does not exist")
    for name in POOL_SUBDIRS:
        path = os.path.join(layout.root, name)
        if os.path.lexists(path):
            remove_tree(path)
            log(f"purged   {path}")
    for path in sorted(glob.glob(os.path.join(layout.log_dir, "owners.*.log*"))):
        os.unlink(path)
        log(f"purged   {path}")
    try:
        os.rmdir(layout.root)  # only when nothing else is in it
    except OSError:
        pass


def uninstall(layout: Layout, do_purge: bool, *, run: Runner = subprocess.run, uid: Optional[int] = None,
              sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
              log: Callable[[str], None] = _log,
              alive: Callable[..., bool] = od.pid_alive) -> int:
    uid = os.getuid() if uid is None else uid
    if do_purge:
        purgeable_root(layout)  # refuse a wrong --root before anything is stopped
    launchd = Launchd(uid, run=run, sleep=sleep, clock=clock)
    was_loaded = launchd.info() is not None
    if not launchd.bootout(log):
        raise ServiceError(f"{LABEL} is still loaded after {UNLOAD_WAIT_SECONDS:.0f} s; its plist is left in place. "
                           f"See `launchctl print {launchd.target}`")
    log(f"launchd  {LABEL}: {'booted out' if was_loaded else 'was not loaded'}")
    # The dispatcher stops its forwarders on SIGTERM; one it could not (launchd's 60 s cap, a crash) is
    # stopped here, by its recorded pid AND start time, so no hook outlives the pool.
    import owners_push  # noqa: PLC0415

    for entry in owners_push.stop_recorded(layout.root, log=log):
        log(f"forwarder stopped a leftover: {entry}")
    if os.path.lexists(layout.plist):
        os.unlink(layout.plist)
        log(f"plist    removed {layout.plist}")
    processes = leftover_processes(layout, run, alive)
    vms = leftover_vms(layout, run)
    if processes or vms:
        # launchd caps ExitTimeOut at 60 s on macOS (measured: a SIGTERM-ignoring job asking for 210 s was
        # SIGKILLed after 61 s), so the dispatcher can be killed before its 150 s child grace is over. The
        # children it already sent SIGTERM live in their own sessions and keep deleting their VMs: give them
        # the rest of the grace before calling anything a leftover.
        log(f"waiting up to {od.CHILD_TERM_GRACE_SECONDS} s for {len(processes)} child process(es) and "
            f"{len(vms)} VM(s) the dispatcher stopped to finish cleaning up...")
        deadline = clock() + od.CHILD_TERM_GRACE_SECONDS
        while (processes or vms) and clock() < deadline:
            sleep(3.0)
            processes = leftover_processes(layout, run, alive)
            vms = leftover_vms(layout, run)
    status = 0
    for entry in processes:
        log(f"LEFTOVER process {entry}")
        status = 1
    for vm in vms:
        log(f"LEFTOVER VM {vm}")
        status = 1
    if vms or processes:
        log("leftovers remain. Goldens and base images are untouched. `tartci owners run` removes the pool's own "
            "leftover VMs at startup (stop it again with Ctrl-C); a stuck process can be ended with `kill`.")
    else:
        log("no tartci-owners-* process or VM is left")
    if do_purge:
        if status:
            log("not purging: leftovers above still need their state files to be identified")
        else:
            purge(layout, log)
    else:
        log(f"kept {layout.root} (state, logs, pinned exports) and {layout.log_dir}/owners.*.log; --purge removes them")
    return status


# -------------------------------------------------------------------------------------- status


def installed_commit(layout: Layout) -> Optional[str]:
    """The full commit `current` points at (read from its COMMIT file), or None."""
    if not os.path.islink(layout.current):
        return None
    return od.recorded_commit(os.path.join(layout.app_dir, os.readlink(layout.current)))


def plist_commit(layout: Layout) -> Optional[str]:
    """The commit of the export the installed plist runs, or None."""
    try:
        with open(layout.plist, "rb") as handle:
            data = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    for argument in data.get("ProgramArguments") or []:
        if isinstance(argument, str) and argument.endswith("/tartci") and os.sep + "app" + os.sep in argument:
            return od.recorded_commit(os.path.dirname(argument))
    return None


def status_lines(layout: Layout, run: Runner = subprocess.run, uid: Optional[int] = None) -> List[str]:
    """Lines for `tartci owners status`: the pinned commit and the agent's launchd state."""
    uid = os.getuid() if uid is None else uid
    lines: List[str] = []
    pinned = installed_commit(layout)
    running_from = plist_commit(layout)
    if pinned is None and running_from is None:
        lines.append("service: not installed (`tartci owners install`)")
    else:
        lines.append(f"service: pinned commit {(running_from or pinned or '')[:12]} "
                     f"(app/current -> {(pinned or 'missing')[:12]})")
        if pinned and running_from and pinned != running_from:
            lines.append(f"  note: the plist runs {running_from[:12]} but `current` is {pinned[:12]}; "
                         "`tartci owners install` makes them agree")
    info = Launchd(uid, run=run).info()
    if info is not None:
        lines.append(f"  launchd {LABEL}: " + ", ".join(f"{key}={value}" for key, value in info.items()))
    elif pinned or running_from:
        lines.append(f"  launchd {LABEL}: not loaded")
    return lines
