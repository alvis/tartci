# Where state lives

Paths are defaults. Most can be overridden by the environment variable named
beside them. `~` is the serving user's home directory.

## Code

| path | what |
|---|---|
| `~/.local/bin/tartci` | installed shim; runs the installed support generation |
| `~/.local/share/tartci-generations/` | immutable support generations, one per installed commit |
| `~/.local/bin/agent-hygiene` | wrapper for `scripts/agent-hygiene` |
| `~/.local/bin/leased-build` | wrapper for `scripts/leased-build`, the agents' core-lease wrapper (`--class check` for git/Python checks) |

A git checkout of this repository is not what a host's supervisors run.

## Configuration (`~/.config/tartci/`)

| path | what |
|---|---|
| `governor.toml` | per-host governor settings (`tartci governor set`) |
| `owners.toml` | the owner pool's owners, extra repos, exclusions, `max_jobs`, goldens, child VM sizes (`tartci owners`) |
| `owners-vm-ed25519(.pub)` | the one SSH key the owner pool's goldens authorize |
| `macos-fleet-profile.toml` | the host's fleet profile (`TARTCI_FLEET_PROFILE`) |
| `macos-fleet-install.json` | install receipt: the services `pool on` restores |
| `self-update.toml` | self-update settings (`[launcher] auto_apply = false` requires the manual launcher ack) |
| `native-build-participation` | `1` participating, `0` opted out (`TARTCI_POOL_PARTICIPATION_FILE`) |
| `pool-state` | `on`, `draining` or `off` (`TARTCI_POOL_STATE_FILE`) |
| `macos-vm-cap` | live macOS guest cap, clamped to 1 to 2 (`TARTCI_MACOS_CAP_FILE`) |
| `macos-vm-reservations/` | per-boot macOS slot reservations (`TARTCI_MACOS_RESV_DIR`) |

## Runtime state (`~/.tartci/`, `TARTCI_HOME`)

| path | what |
|---|---|
| `goldens/`, `bench/`, `windows/`, `iso/` | image stores created by `tartci setup` |
| `state/leases/leases.json` | host lease store (`TARTCI_LEASE_DIR`) |
| `state/macos/`, `state/linux/` | per-lane state files and heartbeats; macOS JIT denial receipts (`TARTCI_STATE_DIR`) |
| `state/job-claims/` | live boot claims |
| `state/pool-log/events.jsonl` | pool status history |
| `state/incidents.jsonl` | host-wide incident lines from macOS lanes (`held_slot_released`, `delete_unproved`), one JSON object each (`TARTCI_INCIDENTS_FILE`) |
| `state/self-update/` | last attempt, waiting ticket (`waiting-aside.json` while the capacity floor refuses), `launcher-auto-apply.json` (a launcher target whose automatic apply failed and is held) |
| `state/gh-identity.json` | cached GitHub credential identity probe |
| `state/vm-dhcp/breaker.json` | VM DHCP breaker (`admission_blocked` while its verification probe is refused by VM admission) |
| `state/reclaim/denial-reclaim.json`, `denial-reclaim.lock` | last lease-denial reclaim (its start time is the per-host cooldown) and its single-runner lock; each run also appends `reclaim_on_lease_denial` to `state/reclaim/events.jsonl` |
| `runtime/` | runtime records when `TARTCI_RUNTIME_MEASURE=1` (`TARTCI_RUNTIME_STORE`) |

## VM images

`TART_HOME` holds Tart VMs. The macOS lanes default it to `~/VMs`. Set it
explicitly for the Linux lane, whose script default is a machine-specific
path. Lanes that share a host must share one `TART_HOME`.

## Logs and agents

| path | what |
|---|---|
| `~/Library/Logs/tartci/` | lane and support-agent logs |
| `~/Library/LaunchAgents/com.danielraffel.*.plist` | installed LaunchAgents (templates in `launchd/`) |

## Data that is not in this repository

Host addresses and the fleet's SSH table live in a separate private repository
that a few scripts read at runtime through the GitHub App CLI. This repository
holds no addresses. Treat that table as read-only.

## Owner pool (`~/.tartci/owners/`, `TARTCI_OWNERS_ROOT`)

| path | what |
|---|---|
| `state/dispatcher.json` | the running dispatcher's status snapshot (`tartci owners status`) |
| `state/<child>/` | one `serve --once` child's runner state; `job-running` there means it was assigned a job. `owners-child.json` is the identity record written at spawn (name, lane, repo, slot, token, pid and the process's start time): a restarted dispatcher adopts a live child from it only when pid and start time still match, and the startup reaper deletes a leftover VM only when it matches. When the child exits the record is marked `ended` and its pid dropped, so it is never adopted. A record with a pid but no start time whose pid is still alive is kept by the reaper (dir never deleted, process never signalled) |
| `app/<full sha>/`, `app/current`, `app/previous` | the pinned exports `tartci owners install` makes with `git archive` (0700 dir, read-only files, a `COMMIT` file): the LaunchAgent runs `app/<full sha>/tartci`, never a live checkout and never self-updating. `current` and `previous` are links that `install` flips; the plist names the full-sha path, so a flip alone changes nothing a running dispatcher uses. `tartci owners status` shows the commit |
| `reap/<owner>__<repo>/reap/runner-offline.json` | the offline clock of the dispatcher's periodic registration reap, one per repository (a shared file would be rewritten by every pass); `logs/reap/last.json` is the last pass's digest |
| `~/Library/LaunchAgents/com.danielraffel.tartci.owners.plist`, `~/Library/Logs/tartci/owners.{out,err}.log` | the agent `install` writes (0644; no credentials) and the dispatcher's stdout/stderr |
| `cache/`, `fetchcontent-src/`, `logs/` | the children's CI cache, the macOS lane's fetchcontent source dir (pool-owned, 0700, so the lane never mounts `$HOME/Library/Caches/Pulp` into a guest) and VM logs; `logs/children/<child>.log` is each child's output |
| retention | ended children's `state/<child>/` and `logs/children/*.log` (and per-VM `logs/{macos,linux}/*`) are removed after 7 days, and all but the newest 100; the root and its files are mode 0700 / 0600 |
