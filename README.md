# tartci

[![lint](https://github.com/danielraffel/tartci/actions/workflows/ci.yml/badge.svg)](https://github.com/danielraffel/tartci/actions/workflows/ci.yml)

tartci turns an Apple Silicon Mac into a pool of disposable CI machines. It
boots a fresh macOS, Linux or Windows virtual machine for each GitHub Actions
job, runs the job, and throws the machine away. Build caches live on the host,
so each new VM starts warm. A per-host budget keeps CI from starving everything
else on the Mac.

## Who it's for

Use tartci if you:

- own one or more Apple Silicon Macs and want them to run your GitHub Actions
  jobs;
- want every job to start from a clean machine, without paying for hosted
  macOS minutes;
- share those Macs with other work (local builds, a DAW, an editor) and need
  CI to stay inside a budget.

tartci is a set of shell and Python scripts plus docs. It holds no VM images:
those are baked or pulled onto each host and never committed.

The public contract is the CLI, provider behavior, and per-project manifests.
Host profiles, runner labels, receipts, host paths, and fleet snapshots describe
one deployment. Do not copy those values into a new installation. Keep a real
fleet's desired state and live receipts in its private operator repository;
examples in this repository are not requirements for using TartCI.

## How it works

```
GitHub Actions queues a job with self-hosted labels
        |
        v
tartci on a Mac sees a matching queued job
        |
        v
takes a lease from the host budget (CPU, memory, disk)
        |
        v
clones a golden VM image (copy-on-write, seconds)
and mounts the host's build caches
        |
        v
registers a one-job runner in the VM; GitHub assigns the job
        |
        v
job finishes -> VM deleted, lease released
```

- **macOS and Linux** guests run on [Tart](https://github.com/openai/tart)
  (Apple Virtualization).
- **Windows** guests run on QEMU, since Apple Virtualization cannot install
  Windows.
- **Golden images** are built or tagged per lane. `tartci prepare linux` builds
  the Pulp Linux golden, `tartci prepare macos` provides macOS golden helpers,
  and `tartci windows golden` snapshots a prepared Windows VM. Copy Windows
  images with `tartci goldens sync`; copy Tart images with `tartci tart-image-sync`.
- **LaunchAgents** in `launchd/` keep the serving loops running across
  reboots.
- **Storage health admission (macOS)** — before new VM admission, each supervisor runs a bounded write/fsync/unlink probe against `TART_HOME`; two failures quarantine admission and three successes reopen it. This gate never changes configured leases or VM capacity, so a healthy host retains its configured envelope. Active VMs are left running.
- **`tartci pool drain` and `tartci pool on`** take a Mac out of the pool and
  bring it back. Drain lets the running job finish.

## Quick start

Requirements: an Apple Silicon Mac on macOS 15 or later, and
[Homebrew](https://brew.sh).

```bash
git clone https://github.com/danielraffel/tartci.git && cd tartci
./tartci doctor          # check prerequisites; changes nothing
./tartci setup           # install Tart, QEMU and tools; create local stores
./tartci status --json   # this host's providers, capacity and role
./tartci host-profile    # the CPU and memory budget tartci derived for this Mac
```

The quickest way to put the Mac to work is `tartci owners`. It serves every
private repository of the GitHub organizations you name, plus the personal
repositories you list. One LaunchAgent starts at login, learns about queued
jobs from webhooks (nothing is polled), and boots a fresh VM per job on one of
three labels: `macos-a64`, `linux-a64` or `linux-x64` (arm64 Linux with an x64
runner under Rosetta). Your GitHub account must be an admin of every
repository to serve and an owner of every organization you list: GitHub lets
only organization owners create the organization webhook the pool listens on.

1. **Build the goldens.** Both carry the toolset of GitHub's hosted runners.
   They need about 250 GB free; the first macOS build also pulls an Xcode
   base image of about 89 GB.

   ```bash
   export TART_HOME=~/.tart
   providers/tart-macos/owners-golden.sh   # -> owners-macos
   providers/tart-linux/owners-golden.sh   # -> owners-linux
   ```

2. **Log in the webhook forwarder.** It uses a second `gh` login in its own
   directory, so the hook scopes never reach your main login:

   ```bash
   gh extension install cli/gh-webhook
   GH_CONFIG_DIR=~/.config/tartci/gh-webhooks gh auth login --hostname github.com \
     --git-protocol ssh --skip-ssh-key --web --insecure-storage \
     --scopes admin:repo_hook,admin:org_hook
   ```

   Never run `gh auth logout`, `refresh` or `switch` with this
   `GH_CONFIG_DIR`: `gh` may act on the keychain entry your main login uses.
   To remove this login, delete the directory.

3. **Name what to serve** in `~/.config/tartci/owners.toml`:

   ```toml
   owners = ["my-org"]                     # organizations: every private repo, including new ones
   repos = ["my-user/app", "my-user/site"] # personal repositories, listed one by one
   max_jobs = 4                            # jobs running at once
   max_macos = 2                           # macOS jobs at once (Apple allows 2 macOS VMs)
   # linux_vm_cores = 4                    # optional per-job VM ceilings
   # linux_vm_mem_mb = 10240
   ```

   A personal account cannot go in `owners`: GitHub has no account-wide
   webhook for a user, so list each of its repositories in `repos`. Only
   private repositories are served, and each must have "Run workflows from
   fork pull requests" turned off.

4. **Check, install and watch.**

   ```bash
   ./tartci owners dry-run   # what would be served; starts nothing
   ./tartci owners install   # pin this commit and load the LaunchAgent
   ./tartci owners status    # served repositories, queued and running jobs
   ```

5. **Opt a workflow in** by its label:

   ```yaml
   jobs:
     build:
       runs-on: [self-hosted, linux-x64]   # or linux-a64, macos-a64
   ```

The service reads `owners.toml` only when it starts. After editing it, check it
with `./tartci owners dry-run`, then restart the service:

```bash
launchctl kickstart -k gui/$(id -u)/com.danielraffel.tartci.owners
```

A restart does not check the config first, and a config that fails to parse
makes the service exit and retry every 30 seconds. Logs are in
`~/Library/Logs/tartci/owners.{out,err}.log`. The
[owner-wide runner pool guide](docs/owner-pool.md) covers each step in depth,
the host's resource budget, security, and removal.

To serve one repository from a lane you configure yourself instead, you need a
macOS golden image on this host and a `gh` login that may register self-hosted
runners on your repository. Point tartci at the repository, the golden and the
labels your workflow asks for:

```bash
export TARTCI_RUNNER_REPO=owner/repo
export TARTCI_MACOS_GOLDEN=my-macos-golden
./tartci serve macos --once --labels self-hosted,macOS,ARM64,my-label
```

`--once` serves one job and exits. `--loop` keeps serving; the LaunchAgent
templates in `launchd/` run it that way. To declare such lanes host by host,
create a profile with the [profile-authoring guide](docs/profile-authoring.md)
and its clean inline example. The public
[`normal-local-fast.toml`](profiles/normal-local-fast.toml) profile is a fuller
operator reference. A private fleet source can replace the checked-in
`*-macos-fleet.toml` profiles and advertised labels used by fleet tooling;
TartCI validates its commit and digests when installing, starting with the
canary host. The [runbook](docs/runbook.md) covers baking goldens, onboarding
a host and installing the agents.

## What it doesn't do

- It does not schedule jobs. GitHub assigns jobs to runners by label.
- It does not merge, queue or cancel pull requests.
  [Shipyard](https://github.com/danielraffel/Shipyard) or your own tooling
  does that.
- It does not run x86_64 natively. Guests are arm64. x86_64 Linux code can be
  cross-built and smoke-tested under Rosetta, but that is not a substitute for
  an x86_64 runner.
- It does not ship VM images. You bake or copy them on each host.
- The on-demand `tartci up` lanes and the bake recipes in `providers/` build
  [Pulp](https://github.com/Generous-Corp/pulp) today. The serving lanes take
  any repository, golden image and labels from the environment.

## Tart and TartCI

[Tart](https://github.com/openai/tart) provides the Apple Virtualization
VM engine. TartCI is an independent project that adds the CI layer: disposable
per-job GitHub runners, host resource budgets, warm caches, Windows through
QEMU, and coordination with [Shipyard](https://github.com/danielraffel/Shipyard).
TartCI is not affiliated with Tart or its maintainers.

With Shipyard, TartCI is the testing half of a loop that drains a repository on
its own: TartCI runs each pull request's checks in a fresh VM, and Shipyard
hands every approved, green head to GitHub to merge. Shipyard's
[Draining pull requests automatically](https://github.com/danielraffel/Shipyard/blob/main/docs/automated-draining.md)
explains how the pieces fit, including GitHub-hosted runners as overflow.

## Learn more

- [Runbook](docs/runbook.md): set up a host, build images, and serve jobs.
- [Prepare](docs/prepare.md): what each `prepare` command does and when to use it.
- [Reference](docs/reference.md): resource limits, serving behavior, diagnostics,
  and environment variables.
- [Gotchas](docs/gotchas.md): known failure modes and practical fixes.
- [Onboarding a new repository](docs/new-repo-agent-guide.md): connect a repo to
  an existing TartCI host.
- [Owner-wide runner pool](docs/owner-pool.md): serve every private repository
  of some GitHub owners from one Mac (`tartci owners`), on `macos-a64`,
  `linux-a64` and `linux-x64` labels.
- [LaunchAgents](launchd/README.md) and the
  [published label table](fleet/README.md).
- [AGENTS.md](AGENTS.md): a dense reference for coding agents.
- [Draining pull requests automatically](https://github.com/danielraffel/Shipyard/blob/main/docs/automated-draining.md)
  (in Shipyard): TartCI, Shipyard and GitHub merging agent pull requests together.

## Contributing

Run the same checks CI runs before you push:

```bash
./scripts/prepush.sh          # fast: lint.sh, 3.9 checks, changed modules' tests
./scripts/prepush.sh --full   # what CI runs: whole suite under 3.9 and python3, shell tests
./scripts/lint.sh      # just shellcheck, bash -n, py_compile and TOML parsing
```

## License

See [LICENSE](LICENSE).
