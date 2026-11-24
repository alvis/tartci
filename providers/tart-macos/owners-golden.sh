#!/usr/bin/env bash
# tart-macos/owners-golden.sh — build the generic `owners-macos` Tart golden used by
# `tartci serve macos` for the owner-wide runner pool (docs/owner-pool.md, D12).
#
# Base:  ghcr.io/cirruslabs/macos-golden-gate-xcode, pinned by digest (macOS 27, Homebrew, Xcode
#        and its Command Line Tools, as on GitHub's macOS images). The pin is the image `:27.2`
#        (= `:latest`) resolved to when this was written; bump it deliberately (`tart pull
#        IMAGE:latest`, then read the digest `tart list` shows for it) so two builds of this
#        script give the same base.
# Adds:  - the host's dedicated VM SSH public key in admin's authorized_keys
#        - GitHub Actions runner v2.338.2 (tartci's pinned default) in ~/actions-runner,
#          verified against tartci's pinned SHA-256, so jobs never download it
#        - the toolset of GitHub's macOS image (actions/runner-images, macos-26-Readme.md) that
#          the base lacks, from Homebrew: GNU and archive utilities (yq, wget, gnu-tar, aria2,
#          p7zip, zstd, git-lfs, gnupg, pkgconf), CMake, Ninja, Bazelisk, Packer, pipx, the AWS
#          CLI and SAM CLI, Session Manager plugin, Azure CLI, azcopy, Bicep, CocoaPods,
#          Carthage, fastlane, SwiftLint, SwiftFormat, xcbeautify, xcodes, unxip, Temurin
#          11/17/21/25 (default 21), Maven, Gradle, Ant, Kotlin, Ruby, PHP with Composer, Go,
#          GCC, PowerShell, .NET SDK, Node.js 24 (Yarn classic, exact version, via npm), jq and gh, Google Chrome and Firefox;
#          plus Rust (rustup-init, SHA-256 pinned) with the stable toolchain. Homebrew bottles are
#          checksummed by Homebrew; versions are not pinned (GitHub's image does not pin them
#          either) but every tool's version is printed in the build log, and the build fails
#          unless each one runs. The google-chrome cask is upstream `sha256 :no_check`, so
#          Homebrew cannot checksum it; the build accepts it only with a valid code signature
#          from a Developer ID certificate Apple issued to Google's team.
#        - the runner environment GitHub's image sets, in ~/actions-runner/.env (the runner loads
#          it; the lane keeps lines it does not own): PATH, ImageOS, RUNNER_TOOL_CACHE =
#          AGENT_TOOLSDIRECTORY = /Users/runner/hostedtoolcache (GitHub's path, so prebuilt
#          tool-cache archives with fixed prefixes work; admin owns it), JAVA_HOME and
#          JAVA_HOME_<v>_X64 / _arm64, LANG/LC_ALL=en_US.UTF-8.
#        Not installed: Microsoft Edge, Selenium server, Android SDK/NDK, extra .NET SDKs and
#        Xcode versions, the hosted tool caches (setup-* actions fetch on demand), CodeQL bundle.
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
# Digest of ghcr.io/cirruslabs/macos-golden-gate-xcode:27.2 (= :latest) as the registry resolved it
# (macOS 27.2 guest with Xcode). `tart list` shows the digest name next to the tag; they are one image.
BASE="ghcr.io/cirruslabs/macos-golden-gate-xcode@sha256:d7641b1d985fd53fbac071c663bf249007b9b387e16b7d576144588acb1d93f9"
REBUILD=0
KEY="${TARTCI_VM_SSH_KEY:-$HOME/.config/tartci/owners-vm-ed25519}"
GUEST_USER="admin"
GUEST_PASS="admin"          # Cirrus base-image default; used only until the key is installed
RUNNER_VERSION="${TARTCI_RUNNER_VERSION:-2.338.2}"
RUNNER_SHA256="${TARTCI_RUNNER_SHA256:-25a065af409ffa317ab4d5a3b614dd15fc9c40f2172bc1b35a1b1258e919a388}"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-osx-arm64-${RUNNER_VERSION}.tar.gz"
# rustup-init (SHA-256 from static.rust-lang.org's .sha256 file next to it).
RUSTUP_VERSION="1.29.3"
RUSTUP_SHA256="63752a1919a3a4793173ec70f8f212621a7ce38ba6a246095984482f4de20f9f"
RUSTUP_URL="https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/aarch64-apple-darwin/rustup-init"
# Yarn classic, an exact version from the npm registry (as in the Linux golden).
YARN_VERSION="1.22.22"

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

note "provisioning guest (runner v$RUNNER_VERSION and the GitHub-image toolset; this takes a while)"
key_ssh "bash -s -- '$RUNNER_URL' '$RUNNER_SHA256' '$RUNNER_VERSION' '$BASE' '$RUSTUP_URL' '$RUSTUP_SHA256' '$YARN_VERSION'" <<'GUEST'
set -euo pipefail
url="$1"; sha="$2"; ver="$3"; base="$4"; rustup_url="$5"; rustup_sha="$6"; YARN_VERSION="$7"
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_ENV_HINTS=1
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

# --- the GitHub macOS image toolset ------------------------------------------------------
# Xcode and its Command Line Tools come with the base (git, clang, swift, xcodebuild).
xcode-select -p >/dev/null || { echo "no active Xcode developer directory in the base" >&2; exit 1; }
sudo xcodebuild -license accept
brew update --quiet >/dev/null
# Taps from the tools' publishers (HashiCorp and Microsoft): Homebrew core dropped Packer, and
# Bicep ships only there.
brew tap hashicorp/tap >/dev/null
brew tap azure/bicep >/dev/null
FORMULAE=(
  jq gh yq wget gnu-tar aria2 p7zip zstd git-lfs gnupg pkgconf openssl@3
  cmake ninja bazelisk hashicorp/tap/packer pipx
  awscli aws-sam-cli azure-cli azcopy azure/bicep/bicep
  cocoapods carthage fastlane swiftlint swiftformat xcbeautify xcodes unxip
  maven gradle ant kotlin ruby php composer go gcc powershell node@24
)
CASKS=(temurin@11 temurin@17 temurin@21 temurin@25 session-manager-plugin dotnet-sdk google-chrome firefox)
brew install --quiet "${FORMULAE[@]}"  # bash32-lint: nonempty literal list just above
brew install --quiet --cask "${CASKS[@]}"  # bash32-lint: nonempty literal list just above
# Node 24 stays the default `node`, as in the base and on GitHub's image: an upgrade of the keg-only
# node@24 leaves it unlinked, so link it again.
brew link --force --overwrite node@24 >/dev/null
# Yarn classic for Node 24 in /opt/homebrew/bin (an exact version; the yarn formula would pull in a
# second, newest Node).
/opt/homebrew/bin/npm install --global --no-fund --no-audit --loglevel=error "yarn@$YARN_VERSION"
git lfs install --system >/dev/null 2>&1 || sudo git lfs install --system >/dev/null
# Rust for the runner user, as GitHub's image has it: rustup with the stable toolchain.
curl -fsSL --retry 3 --retry-all-errors --connect-timeout 15 --max-time 600 "$rustup_url" -o /tmp/rustup-init
[ "$(shasum -a 256 /tmp/rustup-init | awk '{print $1}')" = "$rustup_sha" ] || { echo "rustup-init SHA-256 mismatch" >&2; exit 1; }
chmod 0755 /tmp/rustup-init
/tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain stable \
  --component rustfmt --component clippy >/dev/null
rm -f /tmp/rustup-init

# --- the runner environment GitHub's image sets ------------------------------------------------
# The Aqua launcher starts the runner with Homebrew's shellenv; the runner then loads .env, so this
# PATH is the jobs' PATH. Keg-only Ruby and Node 24 come first, as GitHub's image puts them.
# GitHub's tool-cache path. Cirrus's base links /Users/runner to admin's home; link it if absent.
[ -e /Users/runner ] || sudo ln -s "$HOME" /Users/runner
mkdir -p /Users/runner/hostedtoolcache
jdk(){ printf '/Library/Java/JavaVirtualMachines/temurin-%s.jdk/Contents/Home' "$1"; }
cd "$HOME/actions-runner"
touch .env
{
  awk -F= '$1 !~ /^(PATH|LANG|LC_ALL|ImageOS|ImageVersion|RUNNER_TOOL_CACHE|AGENT_TOOLSDIRECTORY|JAVA_HOME.*|DOTNET_.*)$/' .env
  echo "PATH=$HOME/.cargo/bin:$HOME/.local/bin:/opt/homebrew/opt/ruby/bin:/opt/homebrew/opt/node@24/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.dotnet/tools"
  echo "LANG=en_US.UTF-8"  # launchd starts the runner without a locale; CocoaPods and fastlane need UTF-8
  echo "LC_ALL=en_US.UTF-8"
  echo "ImageOS=macos$(sw_vers -productVersion | cut -d. -f1)"
  echo "ImageVersion=$(date -u +%Y%m%d).tartci"
  echo "RUNNER_TOOL_CACHE=/Users/runner/hostedtoolcache"
  echo "AGENT_TOOLSDIRECTORY=/Users/runner/hostedtoolcache"
  echo "JAVA_HOME=$(jdk 21)"
  for v in 11 17 21 25; do
    echo "JAVA_HOME_${v}_X64=$(jdk "$v")"
    echo "JAVA_HOME_${v}_arm64=$(jdk "$v")"
  done
  echo "DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1"
  echo "DOTNET_NOLOGO=1"
} >.env.new
mv .env.new .env
cd "$HOME"

# --- self-checks: every tool resolves through the runner's environment and runs ---------------
check(){ "$@" || { echo "self-check failed: $*" >&2; exit 1; }; }
first_line(){ local out; out="$("$@" 2>&1)" || { echo "self-check failed: $*" >&2; return 1; }; printf '%s\n' "$out" | awk 'NF { print; exit }'; }
set -a
# shellcheck disable=SC1091
. "$HOME/actions-runner/.env"
set +a
TOOL_CHECKS=(
  "xcodebuild -version" "swift --version" "clang --version" "git --version" "git-lfs --version"
  "jq --version" "yq --version" "gh --version" "wget --version" "gtar --version" "aria2c --version"
  "7z i" "zstd --version" "gpg --version" "pkgconf --version" "cmake --version" "ninja --version"
  "packer version" "pipx --version" "python3 --version" "aws --version" "sam --version"
  "session-manager-plugin --version" "az --version" "azcopy --version" "bicep --version"
  "pod --version" "carthage version" "fastlane --version" "swiftlint version"
  "swiftformat --version" "xcbeautify --version" "xcodes version" "unxip --version"
  "java -version" "mvn -v" "gradle --version" "ant -version" "kotlin -version" "ruby --version"
  "gem --version" "php --version" "composer --version" "go version" "gfortran --version"
  "pwsh --version" "dotnet --version" "node --version" "npm --version" "yarn --version"
  "rustc --version" "cargo --version" "rustfmt --version"
)
for entry in "${TOOL_CHECKS[@]}"; do  # bash32-lint: nonempty literal list just above
  # shellcheck disable=SC2086  # "tool args" splits into the command line
  line="$(first_line $entry)"
  printf 'TARTCI_TOOL %-34s %s\n' "$entry" "$line"
done
for tool in bazel bazelisk; do
  check command -v "$tool" >/dev/null
done
for v in 11 17 21 25; do
  jh="JAVA_HOME_${v}_X64"
  check test -x "${!jh}/bin/java"
done
check [ "$(command -v ruby)" = /opt/homebrew/opt/ruby/bin/ruby ]
check [ "$(command -v node)" = /opt/homebrew/opt/node@24/bin/node ]
check [ "$(command -v yarn)" = /opt/homebrew/bin/yarn ]
check test -w "$RUNNER_TOOL_CACHE"
check [ "$LANG" = en_US.UTF-8 ]  # without it CocoaPods prints its UTF-8 warning first
check test -d "/Applications/Google Chrome.app"
# The google-chrome cask carries no checksum (sha256 :no_check): accept the app only when its
# signature chains to Apple's Developer ID CA and the leaf certificate names Google's team.
CHROME_SIGNER='anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = "EQHXZ8M8AV"'
check codesign --verify -R="$CHROME_SIGNER" "/Applications/Google Chrome.app"
check test -d /Applications/Firefox.app
brew list --versions | sed 's/^/TARTCI_BREW /'
brew list --cask --versions | sed 's/^/TARTCI_CASK /'
brew cleanup -s >/dev/null 2>&1 || true
rm -rf "$(brew --cache)" 2>/dev/null || true

# Hygiene: nothing from the build session reaches clones.
rm -rf "$HOME/.tartci" "$HOME/.cache" "$HOME/.azure" "$HOME/.dotnet" "$HOME/.npm" 2>/dev/null || true
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
