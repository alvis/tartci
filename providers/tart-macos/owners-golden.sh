#!/usr/bin/env bash
# tart-macos/owners-golden.sh — build the generic `owners-macos` Tart golden used by
# `tartci serve macos` for the owner-wide runner pool (docs/owner-pool.md, D12).
#
# Base:  ghcr.io/cirruslabs/macos-golden-gate-base, pinned by digest (macOS, Homebrew, no
#        Xcode). The pin is the image the host's `:latest` resolved to when this was written;
#        bump it deliberately (`tart pull IMAGE:latest`, then read the digest `tart list`
#        shows for it) so two builds of this script give the same base.
# Adds:  - the host's dedicated VM SSH public key in admin's authorized_keys
#        - GitHub Actions runner v2.338.2 (tartci's pinned default) in ~/actions-runner,
#          verified against tartci's pinned SHA-256, so jobs never download it
#        - jq and gh from Homebrew (git comes with the Command Line Tools)
#        - as the LAST step, once key login is proven: sshd refuses password and
#          keyboard-interactive login, and Screen Sharing / Remote Management are off. The
#          lanes log in by key only (BatchMode); the `admin` password is used by this script
#          alone, until the key is installed. The password itself is NOT changed: the base's
#          auto-login and keychain state depend on it.
# No token, JIT config or other secret ever enters the golden: the runner is
# unconfigured, and tartci streams a one-job JIT config into each throwaway clone.
#
# The golden is built under a temporary name and renamed into place only when the whole
# build succeeded, so a failed rebuild never leaves the pool without a golden.
#
# Usage:  providers/tart-macos/owners-golden.sh [--rebuild|--force] [--name owners-macos] [--base IMAGE@sha256:DIGEST]
# Env:    TART_HOME            Tart store. Defaults to ~/.tart only when unset; the pool uses
#                              ~/.tart, so `export TART_HOME=~/.tart` first (runner.sh falls
#                              back to ~/VMs when it is unset).
#         TARTCI_VM_SSH_KEY    private key the host uses to reach guests
#                              (default: ~/.config/tartci/owners-vm-ed25519, created if absent).
#         TARTCI_RUNNER_VERSION / TARTCI_RUNNER_SHA256  override the runner pin (both required).
# Requires on the host: tart, sshpass, ssh, ssh-keygen. Pulls the pinned base if it is not
# in the store yet (a no-op when it is).
set -euo pipefail

export TART_HOME="${TART_HOME:-$HOME/.tart}"
NAME="owners-macos"
# Digest of ghcr.io/cirruslabs/macos-golden-gate-base:latest as `tart pull` resolved it on this
# host (macOS 27.2 guest). `tart list` shows the digest name next to the tag; they are one image.
BASE="ghcr.io/cirruslabs/macos-golden-gate-base@sha256:4859ea9483a1f6e377301f23a5243798044266a373fc36635e9dcd045e09292f"
REBUILD=0
KEY="${TARTCI_VM_SSH_KEY:-$HOME/.config/tartci/owners-vm-ed25519}"
GUEST_USER="admin"
GUEST_PASS="admin"          # Cirrus base-image default; used only until the key is installed
RUNNER_VERSION="${TARTCI_RUNNER_VERSION:-2.338.2}"
RUNNER_SHA256="${TARTCI_RUNNER_SHA256:-25a065af409ffa317ab4d5a3b614dd15fc9c40f2172bc1b35a1b1258e919a388}"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-osx-arm64-${RUNNER_VERSION}.tar.gz"

note() { printf '• %s\n' "$*" >&2; }
die()  { printf '✗ %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --rebuild|--force) REBUILD=1; shift ;;
    --name)  NAME="${2:?--name needs a value}"; shift 2 ;;
    --base)  BASE="${2:?--base needs a value}"; shift 2 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
done

for c in tart sshpass ssh-keygen ssh; do command -v "$c" >/dev/null || die "$c not installed"; done
case "$BASE" in *@sha256:*) ;; *) die "--base must be digest-pinned (IMAGE@sha256:...) so the golden is reproducible" ;; esac

# A failed `tart list` must not read as "absent": the swap-in would then rename onto the old golden.
vm_exists() {
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
BUILD="${NAME}-build-$$"
OLD="${NAME}-old-$$"
vm_exists "$BUILD" && die "$BUILD already exists (a leftover of this script? delete it and retry)"
# A build killed outright (kill -9, power loss) can leave these; never deleted automatically.
stale="$(tart list --quiet 2>/dev/null | grep -E "^${NAME}-(build|old)-[0-9]+\$" || true)"
[ -z "$stale" ] || note "leftover VMs of an earlier killed build (check, then tart delete): $(printf "%s" "$stale" | tr "\n" " ")"

if [ ! -f "$KEY" ]; then
  note "creating dedicated VM SSH key $KEY"
  mkdir -p "$(dirname "$KEY")" && chmod 700 "$(dirname "$KEY")"
  ssh-keygen -q -t ed25519 -N '' -C "tartci-owners-vm" -f "$KEY"
fi
[ -f "$KEY.pub" ] || ssh-keygen -y -f "$KEY" >"$KEY.pub"
PUBKEY="$(cat "$KEY.pub")"

RPID=""; DONE=0; BUILT=0
BOOT_LOG="$(mktemp -t owners-macos-golden-boot)"
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  tart stop "$BUILD" >/dev/null 2>&1 || true
  [ -z "$RPID" ] || { kill "$RPID" 2>/dev/null || true; wait "$RPID" 2>/dev/null || true; }
  rm -f "$BOOT_LOG"
  if [ "$DONE" != 1 ]; then
    [ "$rc" -ne 0 ] || rc=1
    # Without a listing every VM looks absent, and the recovery below could delete a finished
    # build or leave no VM named $NAME: rename and delete nothing.
    if ! tart list --quiet >/dev/null; then
      note "tart list failed, so nothing was renamed or deleted; check tart list for $NAME, $BUILD and $OLD"
      exit "$rc"
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
        note "build failed (rc=$rc); deleting the partial $BUILD"
        tart delete "$BUILD" >/dev/null 2>&1 || true
      else
        note "keeping $BUILD: no VM is named $NAME"
      fi
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

note "pulling $BASE (no-op when it is already in $TART_HOME)"
tart pull "$BASE"
note "cloning $BASE -> $BUILD"
tart clone "$BASE" "$BUILD"
note "booting $BUILD"
tart run --no-graphics "$BUILD" >"$BOOT_LOG" 2>&1 & RPID=$!

IP=""
for _ in $(seq 1 90); do IP="$(tart ip "$BUILD" 2>/dev/null || true)"; [ -n "$IP" ] && break; sleep 2; done
[ -n "$IP" ] || die "no IP for $BUILD"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10)
pw_ssh() { SSHPASS="$GUEST_PASS" sshpass -e ssh -n ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} -o PreferredAuthentications=password,keyboard-interactive -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1 "$GUEST_USER@$IP" "$@"; }
# The base's password login is flaky for its first minute (measured: roughly one attempt in three
# was refused while it settled), so every password-auth step retries, and the IP is re-read while
# waiting (a stale DHCP lease can briefly point `tart ip` at another VM).
pw_retry() { local n=0; while [ "$n" -lt 8 ]; do pw_ssh "$@" && return 0; n=$((n + 1)); sleep 3; done; return 1; }
ready=0
for _ in $(seq 1 120); do
  IP="$(tart ip "$BUILD" 2>/dev/null || echo "$IP")"
  if pw_ssh true 2>/dev/null; then ready=1; break; fi
  sleep 2
done
[ "$ready" = 1 ] || die "ssh (password) did not become ready at $IP"

note "installing the host's VM public key"
pw_retry "umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && { grep -qxF '$PUBKEY' ~/.ssh/authorized_keys || printf '%s\n' '$PUBKEY' >> ~/.ssh/authorized_keys; } && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys" \
  || die "could not install the VM public key over password ssh"
# ssh-stdin: callers feed a heredoc to `bash -s` or pass </dev/null
key_ssh() { ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} -o BatchMode=yes -o IdentitiesOnly=yes -i "$KEY" "$GUEST_USER@$IP" "$@"; }
key_ssh true </dev/null || die "key auth failed after install"
# `tart ip` can briefly answer with another VM's stale lease; every later step goes to $IP, so
# re-check that it is still $BUILD's at each stage, and stop rather than provision the wrong VM.
same_vm() {
  local now="" n=0
  while [ -z "$now" ] && [ "$n" -lt 3 ]; do  # retry a transient empty answer; a DIFFERENT IP is fatal at once
    now="$(tart ip "$BUILD" 2>/dev/null || true)"; [ -n "$now" ] || sleep 2; n=$((n + 1))
  done
  [ "$now" = "$IP" ] || die "tart ip $BUILD changed from $IP to ${now:-nothing} ($1); refusing to continue"
}
same_vm "after the key install"

note "provisioning guest (runner v$RUNNER_VERSION, jq, gh)"
key_ssh "bash -s -- '$RUNNER_URL' '$RUNNER_SHA256' '$RUNNER_VERSION' '$BASE'" <<'GUEST'
set -euo pipefail
url="$1"; sha="$2"; ver="$3"; base="$4"
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_INSTALL_CLEANUP=1
eval "$(/opt/homebrew/bin/brew shellenv)"

# Actions runner, unconfigured, at the path tartci expects ($HOME/actions-runner).
if [ "$("$HOME/actions-runner/bin/Runner.Listener" --version 2>/dev/null | head -n1 || true)" != "$ver" ]; then
  rm -rf "$HOME/actions-runner"; mkdir -p "$HOME/actions-runner"
  curl -fsSL --retry 3 --retry-all-errors --connect-timeout 15 --max-time 600 "$url" -o /tmp/runner.tgz
  [ "$(shasum -a 256 /tmp/runner.tgz | awk '{print $1}')" = "$sha" ] || { echo "runner archive SHA-256 mismatch" >&2; exit 1; }
  tar -xzf /tmp/runner.tgz -C "$HOME/actions-runner"
  rm -f /tmp/runner.tgz
fi
[ "$("$HOME/actions-runner/bin/Runner.Listener" --version | head -n1)" = "$ver" ]
rm -f "$HOME/actions-runner/.runner" "$HOME/actions-runner/.credentials" \
      "$HOME/actions-runner/.credentials_rsaparams" "$HOME/actions-runner/.path" "$HOME/actions-runner/jit.cfg"

# Baseline tools a GitHub-hosted job expects, where cheap.
command -v git >/dev/null || { echo "git missing" >&2; exit 1; }
for f in jq gh; do brew list --formula "$f" >/dev/null 2>&1 || brew install "$f"; done
brew cleanup -s >/dev/null 2>&1 || true
rm -rf "$(brew --cache)" 2>/dev/null || true

# Hygiene: nothing from the build session reaches clones.
rm -rf "$HOME/.tartci" "$HOME/.cache" 2>/dev/null || true
echo "TARTCI_GOLDEN base=$base runner=$("$HOME/actions-runner/bin/Runner.Listener" --version) git=$(git --version) jq=$(jq --version) gh=$(gh --version | head -n1) os=$(sw_vers -productVersion) arch=$(uname -m)"
GUEST

note "verifying the console Aqua session the runner launcher requires"
key_ssh 'id -u; stat -f %Su /dev/console; launchctl print gui/501 | grep -E "^[[:space:]]*session = Aqua$"; pgrep -x WindowServer' </dev/null \
  || die "console Aqua session (uid 501 auto-login) is not healthy in the base"

# ---- hardening: the LAST guest step. The password was needed only to install the key. -----
# The lanes connect with BatchMode + the pool key, never a password. Without this, a job that
# knows the base image's public `admin`/`admin` could log in to any pool VM it can reach
# (the pool's VMs share Tart's default NAT network). Not changed: the account password (auto-login
# and the login keychain depend on it) and admin's passwordless sudo (jobs need it).
# It is a drop-in that sorts before macOS's 100-macos.conf (sshd keeps the FIRST value it reads),
# and the step fails the build unless `sshd -T` reports the settings as effective. Idempotent.
same_vm "after provisioning"
note "hardening: key-only sshd, Screen Sharing and Remote Management off"
key_ssh 'bash -s' <<'HARDEN'
set -euo pipefail
sudo -n true
sudo tee /etc/ssh/sshd_config.d/000-tartci-key-only.conf >/dev/null <<'CONF'
# tartci owner pool: the serving lanes log in by key only. First match wins, so this sorts before 100-macos.conf.
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
CONF
sudo chmod 644 /etc/ssh/sshd_config.d/000-tartci-key-only.conf
sudo /usr/sbin/sshd -t
effective="$(sudo /usr/sbin/sshd -T -C user=admin,host=pool,addr=192.168.64.2)"  # with Match blocks applied
for want in 'passwordauthentication no' 'kbdinteractiveauthentication no' 'authenticationmethods publickey'; do
  printf '%s\n' "$effective" | grep -qix "$want" || { echo "sshd does not honour: $want" >&2; exit 1; }
done
# Screen Sharing (VNC, tcp/5900) and Remote Management (ARD). Disabled in launchd's persistent
# db (survives reboots) and unloaded now. Each is a no-op when it is already off.
sudo launchctl disable system/com.apple.screensharing || true
sudo launchctl bootout system/com.apple.screensharing 2>/dev/null || true
sudo /System/Library/CoreServices/RemoteManagement/ARDAgent.app/Contents/Resources/kickstart -deactivate -stop >/dev/null 2>&1 || true
if sudo lsof -nP -iTCP:5900 -sTCP:LISTEN 2>/dev/null | grep -q .; then
  echo "something still listens on tcp/5900" >&2; exit 1
fi
sync  # a guest write only a hard stop could lose (a bare `tart stop` did, on Linux)
echo "TARTCI_HARDENED sshd=publickey-only 5900=closed"
HARDEN

same_vm "after hardening"
note "proving: key login works, password login is refused"
key_ssh true </dev/null || die "key login broke after hardening"
if pw_ssh true 2>/dev/null; then die "password login still works after hardening"; fi

note "stopping $BUILD"
tart stop "$BUILD"; wait "$RPID" 2>/dev/null || true; RPID=""
BUILT=1

# ---- swap in: $NAME is replaced only now, after a complete build ----------------------------
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
tart list | awk -v n="$NAME" 'NR==1 || $2==n'
note "done: golden $NAME built from $BASE"
