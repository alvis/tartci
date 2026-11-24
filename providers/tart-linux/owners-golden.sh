#!/usr/bin/env bash
# tart-linux/owners-golden.sh — build the generic `owners-linux` golden: ONE
# Ubuntu 24.04 arm64 image that serves both the `linux-a64` lane (arm64 runner,
# native) and the `linux-x64` lane (x64 runner under Rosetta, booted with
# TARTCI_LINUX_ROSETTA=1). Unlike provision.sh it carries no project: no Pulp
# checkout, no Skia, no project dependency set.
#
# What the Linux serving lane (runner.sh) needs from a golden, and what this
# script bakes in:
#   - user `admin` with passwordless sudo, reachable over SSH by KEY (the lane
#     uses BatchMode, never a password): the public half of --ssh-key is put in
#     ~admin/.ssh/authorized_keys. Point the lane at the private half with
#     TARTCI_VM_SSH_KEY.
#   - the arm64 actions-runner at ~/actions-runner (the lane `cd`s there and
#     runs ./run.sh with the JIT config in ACTIONS_RUNNER_INPUT_JITCONFIG). The
#     x64 runner sits at ~/actions-runner-x64 and is used only when
#     TARTCI_LINUX_ROSETTA=1.
#   - `findmnt`, `mountpoint`, awk, bash (the ccache binding and .env edit).
#   - baseline tools a GitHub-hosted job expects: git curl jq unzip zip
#     build-essential python3 ..., and Node.js LTS with npm and npx in
#     /usr/local/bin (hosted images ship them; pnpm/setup's
#     `runtime: node@...` installs node alone)
#   - for x64: amd64 multiarch libraries the x64 runner needs, and a boot-time
#     unit that mounts the `rosetta` virtiofs share and registers binfmt. The
#     unit is a no-op when no Rosetta share is attached (every linux-a64 job).
#
# Reproducible: the base image is digest-pinned, and both runner archives and
# the Node.js archive are checked against pinned SHA-256 values before they
# enter the VM. The GitHub token is never copied into the VM.
#
# Hardening (the last guest step, once key login is proven): sshd refuses password and
# keyboard-interactive login (a drop-in that sorts before cloud-init's, because sshd keeps
# the first value it reads). The lanes use the key only; the `admin` password is used by
# this script alone, until the key is installed. The password is not changed and admin keeps
# passwordless sudo (jobs need it).
#
# The golden is built under a temporary name and renamed into place only when the
# whole build succeeded, so a failed --rebuild never leaves the pool without a golden.
#
# Usage:
#   providers/tart-linux/owners-golden.sh [--name owners-linux] [--rebuild]
#     [--base ghcr.io/cirruslabs/ubuntu@sha256:<pin>]
#     [--ssh-key ~/.config/tartci/owners-vm-ed25519] [--disk 40] [--memory 8192]
#     [--cpu 4] [--no-rosetta]
#
# Needs on the host: tart, sshpass, ssh, scp, curl, shasum. Builds into $TART_HOME, which
# defaults to ~/.tart only when unset: `export TART_HOME=~/.tart` first (the pool's store).
set -euo pipefail

# Ubuntu 24.04 (noble) arm64. Tart accepts name@digest, not name:tag@digest.
BASE="ghcr.io/cirruslabs/ubuntu@sha256:5a6dcb63b71e7120ac2a08733377083232bb8cd3cc1b20e47c518b56f0f17f1f"
NAME="owners-linux"
DISK=40; MEMORY=8192; CPU=4; REBUILD=0; ENABLE_ROSETTA=1
RUNNER_VERSION="2.338.2"
RUNNER_SHA_ARM64="670adb2f268fe21dcbb58b3db0e0aa9d5b3bddfe2c651481336e561ff86de945"
RUNNER_SHA_X64="baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40"
# Node.js LTS (SHA256 from nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt). It brings npm and
# npx, as GitHub-hosted and Ubicloud images do; Node 25+ no longer bundles corepack.
NODE_VERSION="26.13.1"
NODE_SHA_ARM64="89e1853138db51e8b82d3e98ba9c0d3f2956a28e5e55ea1b7e12d25aae92d5dd"
SSH_KEY="${TARTCI_VM_SSH_KEY:-$HOME/.config/tartci/owners-vm-ed25519}"
GUEST_USER="admin"; GUEST_PASS="admin"
DL_DIR="${TARTCI_GOLDEN_DOWNLOADS:-$HOME/.cache/tartci/owners-golden}"
export TART_HOME="${TART_HOME:-$HOME/.tart}"

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do case "$1" in
  --name) NAME="${2:?--name needs a value}"; shift 2;;
  --base) BASE="$2"; shift 2;;
  --ssh-key) SSH_KEY="$2"; shift 2;;
  --disk) DISK="$2"; shift 2;;
  --memory) MEMORY="$2"; shift 2;;
  --cpu) CPU="$2"; shift 2;;
  --rebuild) REBUILD=1; shift;;
  --no-rosetta) ENABLE_ROSETTA=0; shift;;
  -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0;;
  *) die "unknown arg: $1";;
esac; done

for tool in tart sshpass scp ssh-keygen curl shasum ssh; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not installed"
done
case "$BASE" in *@sha256:*) ;; *) die "--base must be digest-pinned (@sha256:...) so the golden is reproducible";; esac

# A failed `tart list` must not read as "absent": the swap-in would then rename onto the old golden.
vm_exists(){
  local names=""
  for _ in 1 2 3; do
    if names="$(tart list --quiet)"; then
      grep -qxF -- "$1" <<<"$names"
      return
    fi
    sleep 2
  done
  die "tart list failed; cannot tell whether $1 exists"
}
if vm_exists "$NAME"; then
  [ "$REBUILD" = 1 ] || die "$NAME already exists in $TART_HOME (use --rebuild to replace it)"
fi
# Everything is built under BUILD; $NAME is only touched by the final swap.
BUILD="${NAME}-build-$$"; OLD="${NAME}-old-$$"
vm_exists "$BUILD" && die "$BUILD already exists (a leftover of this script? delete it and retry)"
# A build killed outright (kill -9, power loss) can leave these; never deleted automatically.
stale="$(tart list --quiet 2>/dev/null | grep -E "^${NAME}-(build|old)-[0-9]+\$" || true)"
[ -z "$stale" ] || note "leftover VMs of an earlier killed build (check, then tart delete): $(printf "%s" "$stale" | tr "\n" " ")"

# ---- host-side inputs ---------------------------------------------------------
if [ ! -f "$SSH_KEY" ]; then
  note "creating the pool's VM SSH key $SSH_KEY (passwordless; used only for guests of this pool)"
  mkdir -p "$(dirname "$SSH_KEY")"
  ssh-keygen -q -t ed25519 -N '' -C "tartci-owners-vm" -f "$SSH_KEY"
fi
[ -f "$SSH_KEY.pub" ] || ssh-keygen -y -f "$SSH_KEY" >"$SSH_KEY.pub"

mkdir -p "$DL_DIR"
fetch_pinned(){ # $1=file name in $DL_DIR $2=url $3=sha256
  local file="$1" got
  if [ -f "$DL_DIR/$file" ]; then
    got="$(shasum -a 256 "$DL_DIR/$file" | awk '{print $1}')"
    [ "$got" = "$3" ] && return 0
    rm -f "$DL_DIR/$file"
  fi
  note "downloading $file"
  curl -fsSL -o "$DL_DIR/$file.part" "$2"
  got="$(shasum -a 256 "$DL_DIR/$file.part" | awk '{print $1}')"
  [ "$got" = "$3" ] || { rm -f "$DL_DIR/$file.part"; die "$file sha256 mismatch (got $got, pinned $3)"; }
  mv "$DL_DIR/$file.part" "$DL_DIR/$file"
}
fetch_runner(){ # $1=arm64|x64 $2=sha256
  local file="actions-runner-linux-$1-$RUNNER_VERSION.tar.gz"
  fetch_pinned "$file" "https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/$file" "$2"
}
fetch_runner arm64 "$RUNNER_SHA_ARM64"
fetch_runner x64 "$RUNNER_SHA_X64"
NODE_TARBALL="node-v$NODE_VERSION-linux-arm64.tar.xz"
fetch_pinned "$NODE_TARBALL" "https://nodejs.org/dist/v$NODE_VERSION/$NODE_TARBALL" "$NODE_SHA_ARM64"

# The trap comes first: a failing pull, clone or `tart set` must not leave a partial $BUILD.
RPID=""; DONE=0; BUILT=0; BOOT_LOG=""
cleanup(){
  [ -n "$RPID" ] && { tart stop "$BUILD" >/dev/null 2>&1 || true; kill "$RPID" 2>/dev/null || true; }
  [ -z "$BOOT_LOG" ] || rm -f "$BOOT_LOG"
  if [ "$DONE" != 1 ]; then
    # Without a listing every VM looks absent, and the recovery below could delete a finished
    # build or leave no VM named $NAME: rename and delete nothing.
    if ! tart list --quiet >/dev/null; then
      note "tart list failed, so nothing was renamed or deleted; check tart list for $NAME, $BUILD and $OLD"
      return
    fi
    # Interrupted mid-swap: $NAME is gone. Put a golden back under it before deleting anything:
    # the new build if it finished, else the old golden.
    if ! vm_exists "$NAME"; then
      if [ "$BUILT" = 1 ] && vm_exists "$BUILD" && tart rename "$BUILD" "$NAME"; then
        note "interrupted mid-swap; the completed new build is now $NAME$(vm_exists "$OLD" && echo " (the previous one is kept as $OLD; delete it when happy)")"
        DONE=1
      elif vm_exists "$OLD" && tart rename "$OLD" "$NAME"; then
        note "interrupted mid-swap; the previous $NAME was restored"
      elif vm_exists "$OLD"; then
        note "WARNING: no VM is named $NAME; the previous golden is $OLD (tart rename $OLD $NAME)"
      fi
    fi
    if [ "$DONE" != 1 ]; then
      if [ "$BUILT" = 1 ] && ! vm_exists "$BUILD" && vm_exists "$NAME"; then
        # Interrupted right after the second rename: the new golden is already in place.
        note "interrupted after the swap; the new $NAME is in place$(vm_exists "$OLD" && echo " (delete the previous one, $OLD, when happy)")"
      elif [ "$BUILT" = 1 ] && vm_exists "$BUILD"; then
        # A complete build that was not swapped in is never deleted.
        note "keeping the complete build $BUILD: it was not swapped in (tart rename it to $NAME once $NAME is out of the way, or tart delete it)"
      elif vm_exists "$NAME" || ! vm_exists "$OLD"; then
        note "build failed — deleting the partial $BUILD"
        tart delete "$BUILD" >/dev/null 2>&1 || true
      else
        note "keeping $BUILD: no VM is named $NAME"
      fi
    fi
  fi
}
trap cleanup EXIT

# ---- clone + size -------------------------------------------------------------
note "pull base (digest-pinned) and clone → $BUILD"
tart pull "$BASE"
tart clone "$BASE" "$BUILD"
tart set "$BUILD" --disk-size "$DISK" --memory "$MEMORY" --cpu "$CPU"

RUN_ARGS=(--no-graphics)
[ "$ENABLE_ROSETTA" = 1 ] && RUN_ARGS+=(--rosetta=rosetta)
note "boot $BUILD (rosetta share: $([ "$ENABLE_ROSETTA" = 1 ] && echo on || echo off))"
BOOT_LOG="$(mktemp -t owners-golden-boot)"
tart run ${RUN_ARGS[@]+"${RUN_ARGS[@]}"} "$BUILD" >"$BOOT_LOG" 2>&1 & RPID=$!

IP=""
for _ in $(seq 1 60); do
  IP="$(tart ip "$BUILD" 2>/dev/null || true)"; [ -n "$IP" ] && break
  kill -0 "$RPID" 2>/dev/null || { tail -5 "$BOOT_LOG" >&2; die "tart run exited early (see above)"; }
  sleep 2
done
[ -n "$IP" ] || { tail -5 "$BOOT_LOG" >&2; die "no IP for $BUILD after 120s"; }

SSH_COMMON=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10)
gpass(){ SSHPASS="$GUEST_PASS" sshpass -e "$@"; }
# One retrying step: wait for password SSH, insist the host is a Linux guest (a
# stale DHCP lease can briefly point `tart ip` at another VM), install the key.
note "install the pool SSH key for $GUEST_USER"
installed=0
for _ in $(seq 1 60); do
  if gpass ssh ${SSH_COMMON[@]+"${SSH_COMMON[@]}"} -o PreferredAuthentications=password -o PubkeyAuthentication=no \
      "$GUEST_USER@$IP" '[ "$(uname -s)" = Linux ] && umask 077 && mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && sort -u -o ~/.ssh/authorized_keys ~/.ssh/authorized_keys' \
      <"$SSH_KEY.pub" 2>/dev/null; then
    installed=1; break
  fi
  sleep 3
done
[ "$installed" = 1 ] || die "could not install the SSH key in the guest at $IP (password SSH, Linux check)"
# ssh-stdin: an argv prefix; every use below passes -n or feeds a heredoc to `bash -s`
KSSH=(ssh -o BatchMode=yes -i "$SSH_KEY" ${SSH_COMMON[@]+"${SSH_COMMON[@]}"})
${KSSH[@]+"${KSSH[@]}"} -n "$GUEST_USER@$IP" true || die "key SSH to the guest failed"
# `tart ip` can briefly answer with another VM's stale lease; every later step goes to $IP, so
# re-check that it is still $BUILD's at each stage, and stop rather than provision the wrong VM.
same_vm(){
  local now="" n=0
  while [ -z "$now" ] && [ "$n" -lt 3 ]; do  # retry a transient empty answer; a DIFFERENT IP is fatal at once
    now="$(tart ip "$BUILD" 2>/dev/null || true)"; [ -n "$now" ] || sleep 2; n=$((n + 1))
  done
  [ "$now" = "$IP" ] || die "tart ip $BUILD changed from $IP to ${now:-nothing} ($1); refusing to continue"
}
same_vm "after the key install"

note "copy the runner and Node.js archives (sha256-verified on the host)"
scp -q -o BatchMode=yes -i "$SSH_KEY" ${SSH_COMMON[@]+"${SSH_COMMON[@]}"} \
  "$DL_DIR/actions-runner-linux-arm64-$RUNNER_VERSION.tar.gz" \
  "$DL_DIR/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz" \
  "$DL_DIR/$NODE_TARBALL" \
  "$GUEST_USER@$IP:/tmp/"

# ---- guest provisioning -------------------------------------------------------
note "provision the guest"
${KSSH[@]+"${KSSH[@]}"} "$GUEST_USER@$IP" "RUNNER_VERSION='$RUNNER_VERSION' ENABLE_ROSETTA='$ENABLE_ROSETTA' NODE_TARBALL='$NODE_TARBALL' bash -s" <<'GUEST'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
sudo -n true || { echo "✗ admin lacks passwordless sudo"; exit 1; }

# Ephemeral VMs: nothing should take the dpkg lock behind a job's back.
sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
sudo systemctl mask apt-daily.service apt-daily-upgrade.service unattended-upgrades.service >/dev/null 2>&1 || true

# --- baseline tools a GitHub-hosted job expects -------------------------------
sudo apt-get update -qq
sudo apt-get install -y -qq \
  ca-certificates curl wget git jq unzip zip xz-utils file rsync openssh-client \
  build-essential pkg-config python3 python3-venv python3-pip \
  util-linux gnupg

# --- Node.js LTS with npm and npx on the default PATH (/usr/local/bin) ------------
# Only the install tree (not the top-level README/LICENSE/CHANGELOG), and root-owned: the
# archive's entries belong to uid 1001, which would otherwise own /usr/local/bin and friends.
NODE_DIR="${NODE_TARBALL%.tar.xz}"
sudo tar -xJf "/tmp/$NODE_TARBALL" -C /usr/local --strip-components=1 --no-same-owner \
  "$NODE_DIR/bin" "$NODE_DIR/include" "$NODE_DIR/lib" "$NODE_DIR/share"
rm -f "/tmp/$NODE_TARBALL"

# --- arm64 runner (the linux-a64 lane; runner.sh `cd ~/actions-runner`) --------
rm -rf "$HOME/actions-runner" "$HOME/actions-runner-x64"
mkdir "$HOME/actions-runner" "$HOME/actions-runner-x64"
tar -xzf "/tmp/actions-runner-linux-arm64-$RUNNER_VERSION.tar.gz" -C "$HOME/actions-runner"
tar -xzf "/tmp/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz" -C "$HOME/actions-runner-x64"
rm -f /tmp/actions-runner-linux-*.tar.gz
# The arm64 runner's own dependency installer (native arch, before multiarch).
sudo "$HOME/actions-runner/bin/installdependencies.sh" >/dev/null

# --- amd64 multiarch for the x64 runner under Rosetta -------------------------
# Only the amd64 libraries the runner needs; the arm64 sources stay on ports.
for f in /etc/apt/sources.list.d/*.sources; do
  [ -e "$f" ] || continue
  if ! grep -q '^Architectures:' "$f"; then
    sudo sed -i '/^URIs:/a Architectures: arm64' "$f"
  fi
done
[ ! -f /etc/apt/sources.list ] || sudo sed -i -E '/^deb / { /\[arch=/! s/^deb /deb [arch=arm64] / }' /etc/apt/sources.list
sudo tee /etc/apt/sources.list.d/tartci-amd64.sources >/dev/null <<'EOF'
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: noble noble-updates noble-security
Components: main universe
Architectures: amd64
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
sudo dpkg --add-architecture amd64
sudo apt-get update -qq
sudo apt-get install -y -qq \
  libc6:amd64 libstdc++6:amd64 libicu74:amd64 libssl3t64:amd64 zlib1g:amd64 \
  libkrb5-3:amd64 libgssapi-krb5-2:amd64 liblttng-ust1t64:amd64
sudo apt-get clean

# --- Rosetta: mount + binfmt at boot, a no-op when no share is attached --------
sudo mkdir -p /mnt/rosetta /usr/local/sbin
sudo tee /usr/local/sbin/tartci-register-rosetta-binfmt >/dev/null <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
mountpoint -q /proc/sys/fs/binfmt_misc || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
if [ ! -x /mnt/rosetta/rosetta ]; then
  echo "Rosetta runtime not mounted at /mnt/rosetta/rosetta" >&2
  exit 1
fi
if [ -e /proc/sys/fs/binfmt_misc/rosetta ]; then
  echo -1 > /proc/sys/fs/binfmt_misc/rosetta
fi
# binfmt_misc decodes \xHH escapes itself. Do not use printf %b here; decoded
# NUL bytes truncate the match and make Rosetta catch arm64 binaries too.
printf '%s' ':rosetta:M::\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x3e\x00:\xff\xff\xff\xff\xff\xfe\xfe\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/mnt/rosetta/rosetta:F' > /proc/sys/fs/binfmt_misc/register
SCRIPT
sudo chmod 0755 /usr/local/sbin/tartci-register-rosetta-binfmt
sudo tee /usr/local/sbin/tartci-rosetta-boot >/dev/null <<'SCRIPT'
#!/bin/sh
# Boot hook: mount the Rosetta share and register x86_64 binfmt, only when the
# VM was started with `tart run --rosetta=rosetta`. Otherwise do nothing, so the
# same golden serves linux-a64 (no share) and linux-x64 (share) unchanged.
mkdir -p /mnt/rosetta
if ! mountpoint -q /mnt/rosetta; then
  mount -t virtiofs rosetta /mnt/rosetta 2>/dev/null || { echo "tartci-rosetta: no rosetta share attached; nothing to do"; exit 0; }
fi
[ -x /mnt/rosetta/rosetta ] || { echo "tartci-rosetta: share has no rosetta runtime; nothing to do"; exit 0; }
exec /usr/local/sbin/tartci-register-rosetta-binfmt
SCRIPT
sudo chmod 0755 /usr/local/sbin/tartci-rosetta-boot
sudo tee /etc/systemd/system/tartci-rosetta.service >/dev/null <<'UNIT'
[Unit]
Description=Mount the Rosetta share and register x86_64 binfmt when attached (no-op otherwise)
Before=ssh.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/tartci-rosetta-boot
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable tartci-rosetta.service >/dev/null 2>&1

# --- self-checks ----------------------------------------------------------------
# Each check is its own statement: `set -e` ignores a failure inside an `a && b` list
# (all but its last command) and inside a command substitution in `echo`'s arguments.
check(){ "$@" || { echo "self-check failed: $*" >&2; exit 1; }; }
arm64_version="$("$HOME/actions-runner/bin/Runner.Listener" --version)"
echo "arm64 runner: $arm64_version"
node_version="$(/usr/local/bin/node --version)"; npm_version="$(/usr/local/bin/npm --version)"
echo "node $node_version, npm $npm_version"
for tool in node npm npx; do
  check [ "$(command -v "$tool")" = "/usr/local/bin/$tool" ]
done
check [ "$(stat -c %u /usr/local/bin)" = 0 ]
check test -x "$HOME/actions-runner/run.sh"
check test -x "$HOME/actions-runner-x64/run.sh"
if [ "$ENABLE_ROSETTA" = 1 ]; then
  sudo /usr/local/sbin/tartci-rosetta-boot
  x64_version="$("$HOME/actions-runner-x64/bin/Runner.Listener" --version)"
  echo "x64 runner under Rosetta: $x64_version"
fi
sudo apt-get clean
sudo rm -f /var/lib/dhcp/*.leases 2>/dev/null || true
sync
GUEST

# ---- hardening: the LAST guest step. The password was needed only to install the key. -----
# The lanes connect with BatchMode + the pool key. Without this, a job that knows the image's
# `admin`/`admin` could log in to any pool VM it can reach (they share Tart's default NAT
# network). cloud-init's 50-cloud-init.conf sets `PasswordAuthentication yes` and sshd keeps the
# FIRST value it reads, so the drop-in must sort before it: 00-*.
same_vm "after provisioning"
note "hardening: key-only sshd"
${KSSH[@]+"${KSSH[@]}"} "$GUEST_USER@$IP" 'bash -s' <<'HARDEN'
set -euo pipefail
sudo -n true
sudo tee /etc/ssh/sshd_config.d/00-tartci-key-only.conf >/dev/null <<'CONF'
# tartci owner pool: the serving lanes log in by key only. First match wins, so this sorts before 50-cloud-init.conf.
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
CONF
sudo chmod 644 /etc/ssh/sshd_config.d/00-tartci-key-only.conf
sudo sshd -t
effective="$(sudo sshd -T -C user=admin,host=pool,addr=192.168.64.2)"  # with Match blocks applied
for want in 'passwordauthentication no' 'kbdinteractiveauthentication no' 'authenticationmethods publickey'; do
  printf '%s\n' "$effective" | grep -qix "$want" || { echo "sshd does not honour: $want" >&2; exit 1; }
done
sudo systemctl restart ssh.service
sync  # a guest write only a hard stop could lose (a bare `tart stop` did, on Linux)
echo "TARTCI_HARDENED sshd=publickey-only"
HARDEN
same_vm "after hardening"
note "proving: key login works, password login is refused"
for _ in $(seq 1 20); do ${KSSH[@]+"${KSSH[@]}"} -n "$GUEST_USER@$IP" true 2>/dev/null && break; sleep 2; done
${KSSH[@]+"${KSSH[@]}"} -n "$GUEST_USER@$IP" true || die "key login broke after hardening"
if gpass ssh -n ${SSH_COMMON[@]+"${SSH_COMMON[@]}"} -o PreferredAuthentications=password,keyboard-interactive -o PubkeyAuthentication=no \
    -o NumberOfPasswordPrompts=1 "$GUEST_USER@$IP" true 2>/dev/null; then
  die "password login still works after hardening"
fi

note "shut down $BUILD"
${KSSH[@]+"${KSSH[@]}"} -n "$GUEST_USER@$IP" 'sudo systemctl poweroff' >/dev/null 2>&1 || true
for _ in $(seq 1 30); do kill -0 "$RPID" 2>/dev/null || break; sleep 2; done
tart stop "$BUILD" >/dev/null 2>&1 || true
RPID=""; BUILT=1
rm -f "$BOOT_LOG"; BOOT_LOG=""

# ---- swap in: $NAME is replaced only now, after a complete build ---------------------------
if vm_exists "$NAME"; then
  note "replacing $NAME (the old one is kept as $OLD until the new one is in place)"
  tart rename "$NAME" "$OLD"
  if ! tart rename "$BUILD" "$NAME"; then
    if tart rename "$OLD" "$NAME"; then
      die "could not move $BUILD to $NAME; the previous $NAME was restored"
    fi
    die "could not move $BUILD to $NAME, nor restore $OLD; run: tart rename $OLD $NAME"
  fi
  DONE=1
  tart delete "$OLD" || note "could not delete $OLD; delete it by hand"
else
  tart rename "$BUILD" "$NAME"
  DONE=1
fi
note "built $NAME ($(tart list | awk -v n="$NAME" '$2==n {print $3 " GB disk, " $5 " GB on host"}'))"
