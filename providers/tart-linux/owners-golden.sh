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
#   - the toolset of GitHub's hosted Ubuntu image (actions/runner-images, Ubuntu2604-Readme.md)
#     wherever it has a native arm64 Linux build, so a job that passes on `ubuntu-latest` finds the
#     same commands here: GitHub's apt package list, compilers (gcc 13/14, gfortran, clang), CMake,
#     Ninja, Python with pip/pipx/venv, Docker CE (engine, CLI, buildx, compose; admin is in the
#     docker group), Podman/Buildah/Skopeo, Kubernetes tools, Packer, Bazelisk, the AWS, Azure and
#     Google Cloud CLIs, Temurin 11/17/21/25 (default 25), Maven, Gradle, Ant, Kotlin, Go, Rust,
#     Ruby, PHP with Composer, .NET 10, PowerShell, vcpkg, nvm, PostgreSQL and MySQL (installed,
#     services disabled, as on GitHub), Apache and nginx (disabled), Firefox with geckodriver, and
#     Node.js LTS with npm, npx, Yarn classic and the GitHub CLI in /usr/local/bin.
#     Not installed (no arm64 Linux build, or multi-version sets setup-* actions fetch on demand):
#     Google Chrome/ChromeDriver, Microsoft Edge, Chromium (snap-only on noble), Selenium server,
#     Android SDK/NDK, Haskell, Swift, Homebrew, CodeQL bundle, extra .NET SDKs, the hosted tool
#     caches. See docs/owner-pool.md "Golden toolset".
#   - the runner environment GitHub's image sets (/etc/environment and each runner's .env):
#     ImageOS, RUNNER_TOOL_CACHE=AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache (admin-writable, so
#     setup-* actions cache there), JAVA_HOME and JAVA_HOME_<v>_X64 (and _ARM64), and the PATH
#     with ~/.cargo/bin and /opt/pipx_bin. The x64 runner's .env also sets
#     DOCKER_DEFAULT_PLATFORM=linux/amd64, so its containers are amd64 (run under Rosetta), as on a
#     GitHub x64 runner.
#   - for x64: amd64 multiarch libraries the x64 runner needs, plus the common runtime libraries
#     x64 binaries that setup-* actions download expect (libatomic, libssl, libcurl, libffi, ...),
#     and a boot-time unit that mounts the `rosetta` virtiofs share and registers binfmt. The
#     unit is a no-op when no Rosetta share is attached (every linux-a64 job).
#
# Reproducible: the base image is digest-pinned, and every direct download (both runner archives,
# Node.js, gh and the TOOL_DOWNLOADS table) is checked against a pinned SHA-256 on the host before
# it enters the VM. Packages from apt repositories are verified by apt against the repository's
# signing key; each third-party key is accepted only when its fingerprint matches the one pinned
# here. The self-check's hello-world image is pinned by digest. Not hash-pinned, by design: Yarn
# and the pipx tools (ansible-core, yamllint) are exact versions from the npm registry and PyPI,
# which never republish a version, but the dependencies of the pipx tools and of the pinned
# azure-devops wheel (distro) resolve at build time; the Rust stable toolchain comes from
# rustup's checksummed channel and the vcpkg tool is checked by its bootstrap. Each is logged
# with its version. The GitHub token is never copied into the VM.
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
#     [--ssh-key ~/.config/tartci/owners-vm-ed25519] [--disk 100] [--memory 8192]
#     [--cpu 4] [--no-rosetta]
#
# Needs on the host: tart, sshpass, ssh, scp, curl, shasum. Builds into $TART_HOME, which
# defaults to ~/.tart only when unset: `export TART_HOME=~/.tart` first (the pool's store).
set -euo pipefail

# Ubuntu 24.04 (noble) arm64. Tart accepts name@digest, not name:tag@digest. GitHub's current image
# is Ubuntu 26.04, but Cirrus Labs publishes no 26.04 Tart image yet (tags: 20.04, 22.04, 24.04).
BASE="ghcr.io/cirruslabs/ubuntu@sha256:5a6dcb63b71e7120ac2a08733377083232bb8cd3cc1b20e47c518b56f0f17f1f"
NAME="owners-linux"
DISK=100; MEMORY=8192; CPU=4; REBUILD=0; ENABLE_ROSETTA=1
RUNNER_VERSION="2.338.2"
RUNNER_SHA_ARM64="670adb2f268fe21dcbb58b3db0e0aa9d5b3bddfe2c651481336e561ff86de945"
RUNNER_SHA_X64="baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40"
# Node.js LTS (SHA256 from nodejs.org/dist/v$NODE_VERSION/SHASUMS256.txt). It brings npm and
# npx, as GitHub-hosted and Ubicloud images do; Node 25+ no longer bundles corepack.
NODE_VERSION="26.13.1"
NODE_SHA_ARM64="89e1853138db51e8b82d3e98ba9c0d3f2956a28e5e55ea1b7e12d25aae92d5dd"
# GitHub CLI (SHA256 from the release's gh_<version>_checksums.txt) and Yarn classic, as hosted
# images ship them. Yarn is an exact version from the npm registry, checked only against the
# registry's own integrity metadata.
GH_VERSION="2.105.0"
GH_SHA_ARM64="b1278c75107423d818f1937fd92cafc9e5e9b606631568f68d7fb816f5885a02"
YARN_VERSION="1.22.22"
# GitHub-hosted image toolset, direct downloads. One row per tool: KEY VERSION FILE URL SHA256, where
# @V@ in FILE and URL stands for VERSION. Every SHA-256 was compared with the publisher's own
# (checksum file or release-asset digest; the AWS CLI zip with its PGP signature; the SSM plugin
# and nvm publish none). The guest finds each file as $TOOLS/$<KEY>_FILE, its version as $<KEY>_VERSION.
TOOL_DOWNLOADS='
YQ         4.54.3   yq_linux_arm64-v@V@                    https://github.com/mikefarah/yq/releases/download/v@V@/yq_linux_arm64                                          89f69310b61b68d1245065a1241d1fc843f7a36cd6103ffd77ad987613bd1b67
KUBECTL    1.37.2   kubectl-v@V@-linux-arm64               https://dl.k8s.io/release/v@V@/bin/linux/arm64/kubectl                                                         fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509
HELM       4.3.2    helm-v@V@-linux-arm64.tar.gz           https://get.helm.sh/helm-v@V@-linux-arm64.tar.gz                                                                df31cb61680ef728f82c770e5280dd565617bee6b039d37e85c677288be4bbb0
KUSTOMIZE  5.8.3    kustomize_v@V@_linux_arm64.tar.gz      https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv@V@/kustomize_v@V@_linux_arm64.tar.gz 9867b76482cfc6d25b546abc1d2b5a32bd137505aba640649b4b378f64f6d68f
KIND       0.33.0   kind-v@V@-linux-arm64                  https://github.com/kubernetes-sigs/kind/releases/download/v@V@/kind-linux-arm64                                20022bee6cfcd5086cb7234d218e3454e6090022f2a8f55d1fa7fcf42c3867a2
MINIKUBE   1.39.0   minikube-v@V@-linux-arm64              https://github.com/kubernetes/minikube/releases/download/v@V@/minikube-linux-arm64                              202f331cb677761c5aec81c58472d16d65707358b9f66226210be3fe3275b1c3
BAZELISK   1.30.0   bazelisk-v@V@-linux-arm64              https://github.com/bazelbuild/bazelisk/releases/download/v@V@/bazelisk-linux-arm64                              b2bcc040c213b44586b121bda6d983c35df121b917c3a7a66fb16f2fa0df1a4f
BICEP      0.49.2   bicep-v@V@-linux-arm64                 https://github.com/Azure/bicep/releases/download/v@V@/bicep-linux-arm64                                         48d8f152bd78c8848a41a07ac2d12b573306d9329fc961a85354a98a95cc275e
AZCOPY     10.34.2  azcopy_linux_arm64_@V@.tar.gz          https://github.com/Azure/azure-storage-azcopy/releases/download/v@V@/azcopy_linux_arm64_@V@.tar.gz              feb9f59e8a7ce701d2ec9521b85176a243a095525bebab34d7bc240e76620fcc
AWSCLI     2.42.2   awscli-exe-linux-aarch64-@V@.zip       https://awscli.amazonaws.com/awscli-exe-linux-aarch64-@V@.zip                                                  efda2e5d9d01726f47c491a7b006f55611e682a0bdb08f4d65594e27bdc6ab30
SAM        1.174.0  aws-sam-cli-v@V@-linux-arm64.zip       https://github.com/aws/aws-sam-cli/releases/download/v@V@/aws-sam-cli-linux-arm64.zip                           65901c10e2260aaacbb2b9447c017bb5d82490f294247278c63c1a61412dc8ef
SSM        1.2.896.0 session-manager-plugin-@V@-arm64.deb  https://s3.amazonaws.com/session-manager-downloads/plugin/@V@/ubuntu_arm64/session-manager-plugin.deb  914eeca87b18b55356c58be197080f73742b3ca60d3823f0a45670dc9056aa9c
CMAKE      4.5.1    cmake-@V@-linux-aarch64.tar.gz         https://github.com/Kitware/CMake/releases/download/v@V@/cmake-@V@-linux-aarch64.tar.gz                          3bec208f11ed96c173032eeaf97937c54dd246acefee22acd7794a6b4e70d536
GO         1.27.4   go@V@.linux-arm64.tar.gz               https://go.dev/dl/go@V@.linux-arm64.tar.gz                                                                      3392582988a273a87ab5043ee1b2c9c17a2c3c00580bf9326c47a2e0aaaeac92
RUSTUP     1.29.3   rustup-init-@V@-aarch64                https://static.rust-lang.org/rustup/archive/@V@/aarch64-unknown-linux-gnu/rustup-init                          9f608c142e0e38a78957d0661ba1dfaf0b6fb37184cfaa4b9a0aa57af6184282
MAVEN      3.9.16   apache-maven-@V@-bin.tar.gz            https://archive.apache.org/dist/maven/maven-3/@V@/binaries/apache-maven-@V@-bin.tar.gz                           80ffca22aed9e8b9713a232f3394fd81d7f20322df75efdb2b047dbd3e3a23bb
GRADLE     9.9.2    gradle-@V@-bin.zip                     https://services.gradle.org/distributions/gradle-@V@-bin.zip                                                    971fcd7150ca4ba04852c0b2ca0d2d0ab9468e14b03473eab19528809914c76b
KOTLIN     2.4.21   kotlin-compiler-@V@.zip                https://github.com/JetBrains/kotlin/releases/download/v@V@/kotlin-compiler-@V@.zip                              7cc140e76daf416a0424a5557f99afd7ca89ebd463ea1101261e3e01c33e75cd
PWSH       7.7.0    powershell-@V@-linux-arm64.tar.gz      https://github.com/PowerShell/PowerShell/releases/download/v@V@/powershell-@V@-linux-arm64.tar.gz               e59c89776eaa59790d44b3edd14277435225ab08b6e7603763101b89de83d935
GECKO      0.37.1   geckodriver-v@V@-linux-aarch64.tar.gz  https://github.com/mozilla/geckodriver/releases/download/v@V@/geckodriver-v@V@-linux-aarch64.tar.gz            8fd90b951422fbad5b56539fb344dff66eb3f986d615d8d6fde9f1f62dad610c
NVM        0.41.0   nvm-v@V@.tar.gz                        https://github.com/nvm-sh/nvm/archive/refs/tags/v@V@.tar.gz                                                     532560c9ecc6ef484279fcc1c600e9346537bc7913b891ffd0ec0caca5072694
AZDEVOPS   1.0.12   azure_devops-@V@-py2.py3-none-any.whl  https://azcliprod.blob.core.windows.net/cli-extensions/azure_devops-@V@-py2.py3-none-any.whl          03ad7844323e6cb3c675b1a697af833683b0f6d720f113ba5206f83cdcb88755
'
# vcpkg is a git checkout of this exact commit (tag 2026.10.16); its bootstrap verifies the tool's SHA-512.
VCPKG_COMMIT="9eec002df1cd109f8f104cd966a763daf9751489"
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
GH_TARBALL="gh_${GH_VERSION}_linux_arm64.tar.gz"
fetch_pinned "$GH_TARBALL" "https://github.com/cli/cli/releases/download/v$GH_VERSION/$GH_TARBALL" "$GH_SHA_ARM64"
# The toolset table: fetch each row through the same checksum gate, and collect the guest's
# manifest (written to a temp file once the cleanup trap is set).
TOOL_FILES=()
MANIFEST_TEXT=""
while read -r key ver file url sha; do
  [ -n "$key" ] || continue
  file="${file//@V@/$ver}"; url="${url//@V@/$ver}"
  fetch_pinned "$file" "$url" "$sha"
  TOOL_FILES+=("$DL_DIR/$file")
  MANIFEST_TEXT+="$(printf "%s_VERSION='%s'\n%s_FILE='%s'" "$key" "$ver" "$key" "$file")"$'\n'
done <<<"$TOOL_DOWNLOADS"
MANIFEST_TEXT+="VCPKG_COMMIT='$VCPKG_COMMIT'"$'\n'

# The trap comes first: a failing pull, clone or `tart set` must not leave a partial $BUILD.
RPID=""; DONE=0; BUILT=0; BOOT_LOG=""; MANIFEST=""
cleanup(){
  [ -n "$RPID" ] && { tart stop "$BUILD" >/dev/null 2>&1 || true; kill "$RPID" 2>/dev/null || true; }
  [ -z "$BOOT_LOG" ] || rm -f "$BOOT_LOG"
  [ -z "${MANIFEST:-}" ] || rm -f "$MANIFEST"
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
MANIFEST="$(mktemp -t owners-golden-manifest)"
printf '%s' "$MANIFEST_TEXT" >"$MANIFEST"

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

note "copy the runner, Node.js and GitHub CLI archives (sha256-verified on the host)"
scp -q -o BatchMode=yes -i "$SSH_KEY" ${SSH_COMMON[@]+"${SSH_COMMON[@]}"} \
  "$DL_DIR/actions-runner-linux-arm64-$RUNNER_VERSION.tar.gz" \
  "$DL_DIR/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz" \
  "$DL_DIR/$NODE_TARBALL" \
  "$DL_DIR/$GH_TARBALL" \
  "$GUEST_USER@$IP:/tmp/"
note "copy the toolset downloads (sha256-verified on the host) and their manifest"
${KSSH[@]+"${KSSH[@]}"} -n "$GUEST_USER@$IP" 'rm -rf /tmp/tartci-tools && mkdir /tmp/tartci-tools'
scp -q -o BatchMode=yes -i "$SSH_KEY" ${SSH_COMMON[@]+"${SSH_COMMON[@]}"} \
  ${TOOL_FILES[@]+"${TOOL_FILES[@]}"} "$GUEST_USER@$IP:/tmp/tartci-tools/"
scp -q -o BatchMode=yes -i "$SSH_KEY" ${SSH_COMMON[@]+"${SSH_COMMON[@]}"} \
  "$MANIFEST" "$GUEST_USER@$IP:/tmp/tartci-tools/manifest.env"

# ---- guest provisioning -------------------------------------------------------
note "provision the guest (the GitHub-image toolset; this takes a while)"
${KSSH[@]+"${KSSH[@]}"} "$GUEST_USER@$IP" "RUNNER_VERSION='$RUNNER_VERSION' ENABLE_ROSETTA='$ENABLE_ROSETTA' NODE_TARBALL='$NODE_TARBALL' GH_TARBALL='$GH_TARBALL' YARN_VERSION='$YARN_VERSION' bash -s" <<'GUEST'
# One brace group, so bash parses the whole script before running any of it: stdin is the script,
# and a step that reads stdin would otherwise swallow the rest of it and end the session with
# status 0, skipping every later step and self-check. A truncated script is a syntax error.
{
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
sudo -n true || { echo "✗ admin lacks passwordless sudo"; exit 1; }
TOOLS=/tmp/tartci-tools
# shellcheck disable=SC1091  # KEY_VERSION / KEY_FILE for every row of the host's TOOL_DOWNLOADS
. "$TOOLS/manifest.env"
apt_install(){ sudo apt-get install -y -qq -o Acquire::Retries=3 "$@"; }

# Ephemeral VMs: nothing should take the dpkg lock behind a job's back.
sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
sudo systemctl mask apt-daily.service apt-daily-upgrade.service unattended-upgrades.service >/dev/null 2>&1 || true

# --- baseline tools a GitHub-hosted job expects -------------------------------
sudo apt-get update -qq
apt_install \
  ca-certificates curl wget git jq unzip zip xz-utils file rsync openssh-client \
  build-essential pkg-config python3 python3-venv python3-pip \
  util-linux gnupg

# --- third-party apt repositories (the key is pinned by fingerprint) ---------------
# Each keyring holds exactly the pinned key, exported from a throwaway GnuPG home; a key whose
# fingerprint differs fails the build. Every source is arm64-only: the amd64 multiarch below must
# not ask these repositories for amd64 indexes.
sudo install -d -m 0755 /etc/apt/keyrings
apt_repo(){ # $1=name $2=key URL $3=key fingerprint $4=URIs $5=Suites $6=Components
  local gh key
  gh="$(mktemp -d)"; key="$gh/key"
  curl -fsSL --retry 3 -o "$key" "$2"
  GNUPGHOME="$gh" gpg --batch --quiet --import "$key" 2>/dev/null
  if ! GNUPGHOME="$gh" gpg --batch --with-colons --list-keys "$3" 2>/dev/null | grep -q "^fpr:::::::::$3:"; then
    echo "✗ $1: the signing key at $2 is not $3" >&2; exit 1
  fi
  GNUPGHOME="$gh" gpg --batch --export "$3" | sudo tee "/etc/apt/keyrings/$1.gpg" >/dev/null
  rm -rf "$gh"
  printf 'Types: deb\nURIs: %s\nSuites: %s\nComponents: %s\nArchitectures: arm64\nSigned-By: /etc/apt/keyrings/%s.gpg\n' \
    "$4" "$5" "$6" "$1" | sudo tee "/etc/apt/sources.list.d/$1.sources" >/dev/null
}
apt_repo docker     https://download.docker.com/linux/ubuntu/gpg              9DC858229FC7DD38854AE2D88D81803C0EBFCD88 https://download.docker.com/linux/ubuntu noble stable
apt_repo azure-cli  https://packages.microsoft.com/keys/microsoft.asc         BC528686B50D79E339D3721CEB3E94ADBE1229CF https://packages.microsoft.com/repos/azure-cli/ noble main
apt_repo google-cloud https://packages.cloud.google.com/apt/doc/apt-key.gpg   35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3 https://packages.cloud.google.com/apt cloud-sdk main
apt_repo adoptium   https://packages.adoptium.net/artifactory/api/gpg/key/public 3B04D753C9050D9A5D343F39843C48A565F8F04B https://packages.adoptium.net/artifactory/deb noble main
apt_repo hashicorp  https://apt.releases.hashicorp.com/gpg                    D55C0D1AC78A8D8126CB631CFC9CA96ACA026560 https://apt.releases.hashicorp.com noble main
apt_repo mozilla    https://packages.mozilla.org/apt/repo-signing-key.gpg     35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3 https://packages.mozilla.org/apt mozilla main
# Firefox from Mozilla's repository, not noble's snap-transition package; nothing else from there.
printf 'Package: firefox*\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n\nPackage: *\nPin: origin packages.mozilla.org\nPin-Priority: 100\n' \
  | sudo tee /etc/apt/preferences.d/mozilla >/dev/null
sudo apt-get update -qq

# --- GitHub's "Installed apt packages" list, as named on noble ----------------------
# Missing on noble: libjitterentropy3-dev. Renamed: netcat -> netcat-openbsd, upx -> upx-ucl.
# libasound2t64 is named so apt never picks liboss4-salsa-asound2 for ALSA (Firefox conflicts with it).
# ssh-stdin: `ssh` below is the apt package of that name, not an invocation
apt_install \
  libasound2t64 7zip 7zip-rar acl aria2 autoconf automake bind9-dnsutils binutils bison brotli bzip2 coreutils \
  dbus dpkg dpkg-dev fakeroot findutils flex fonts-noto-color-emoji ftp g++ gcc gnupg2 iproute2 \
  iputils-ping libcrypt-dev libicu-dev libnss3-tools libsqlite3-dev libssl-dev libtool libyaml-dev \
  locales lz4 m4 make net-tools netcat-openbsd parallel patchelf pigz pollinate python-is-python3 \
  rpm shellcheck sqlite3 ssh sshpass sudo swig systemd-coredump tar telnet texinfo time tk tree \
  tzdata upx-ucl xvfb zsync

# --- compilers, build tools, version control ----------------------------------------
apt_install \
  gfortran gcc-14 g++-14 gfortran-14 clang clang-format clang-tidy lld llvm ninja-build \
  git-lfs git-ftp zstd python3-dev pipx
sudo tar -xzf "$TOOLS/$CMAKE_FILE" -C /opt --no-same-owner
for b in cmake ctest cpack ccmake; do sudo ln -sf "/opt/cmake-$CMAKE_VERSION-linux-aarch64/bin/$b" "/usr/local/bin/$b"; done
sudo install -m 0755 "$TOOLS/$BAZELISK_FILE" /usr/local/bin/bazelisk
sudo ln -sf bazelisk /usr/local/bin/bazel
sudo git lfs install --system >/dev/null

# --- languages and runtimes ------------------------------------------------------
apt_install \
  ruby-full perl \
  php-cli php-common php-bcmath php-curl php-gd php-intl php-mbstring php-mysql php-pgsql \
  php-sqlite3 php-xml php-zip php-xdebug php-pcov composer \
  dotnet-sdk-10.0 \
  temurin-11-jdk temurin-17-jdk temurin-21-jdk temurin-25-jdk ant
# As on GitHub's image: Xdebug enabled, PCOV installed but off.
sudo phpdismod pcov
for t in java javac jar; do
  sudo update-alternatives --set "$t" "/usr/lib/jvm/temurin-25-jdk-arm64/bin/$t" >/dev/null
done
sudo tar -xzf "$TOOLS/$GO_FILE" -C /usr/local --no-same-owner
sudo ln -sf /usr/local/go/bin/go /usr/local/bin/go
sudo ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt
sudo tar -xzf "$TOOLS/$MAVEN_FILE" -C /usr/share --no-same-owner
sudo ln -sf "/usr/share/apache-maven-$MAVEN_VERSION/bin/mvn" /usr/local/bin/mvn
sudo unzip -q -o "$TOOLS/$GRADLE_FILE" -d /usr/share
sudo ln -sf "/usr/share/gradle-$GRADLE_VERSION/bin/gradle" /usr/local/bin/gradle
sudo unzip -q -o "$TOOLS/$KOTLIN_FILE" -d /usr/share
for b in kotlin kotlinc kotlinc-jvm kapt; do sudo ln -sf "/usr/share/kotlinc/bin/$b" "/usr/local/bin/$b"; done
sudo install -d /opt/microsoft/powershell/7
sudo tar -xzf "$TOOLS/$PWSH_FILE" -C /opt/microsoft/powershell/7 --no-same-owner
sudo chmod 0755 /opt/microsoft/powershell/7/pwsh
sudo ln -sf /opt/microsoft/powershell/7/pwsh /usr/local/bin/pwsh
# Rust for the runner user, as GitHub's image has it: rustup with the stable toolchain.
install -m 0755 "$TOOLS/$RUSTUP_FILE" /tmp/rustup-init
/tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain stable \
  --component rustfmt --component clippy >/dev/null
rm -f /tmp/rustup-init
# nvm, sourced by interactive shells (NVM_DIR is in the runner environment).
mkdir -p "$HOME/.nvm"
tar -xzf "$TOOLS/$NVM_FILE" -C "$HOME/.nvm" --strip-components=1
grep -q 'NVM_DIR/nvm.sh' "$HOME/.bashrc" 2>/dev/null \
  || printf '%s\n' 'export NVM_DIR="$HOME/.nvm"' '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"' >>"$HOME/.bashrc"
# pipx tools in the shared location GitHub's image uses, at exact versions (ansible-core 2.22 needs
# Python 3.13; noble has 3.12). Their dependencies resolve from PyPI at build time.
sudo env PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/opt/pipx_bin pipx install ansible-core==2.21.6 >/dev/null
sudo env PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/opt/pipx_bin pipx install yamllint==1.38.0 >/dev/null
# vcpkg at an exact commit; the bootstrap downloads the vcpkg tool and checks its SHA-512.
sudo git clone --quiet https://github.com/microsoft/vcpkg /usr/local/share/vcpkg
sudo git -C /usr/local/share/vcpkg checkout --quiet "$VCPKG_COMMIT"
[ "$(sudo git -C /usr/local/share/vcpkg rev-parse HEAD)" = "$VCPKG_COMMIT" ]
sudo /usr/local/share/vcpkg/bootstrap-vcpkg.sh -disableMetrics >/dev/null
sudo chown -R "$(id -u):$(id -g)" /usr/local/share/vcpkg
sudo ln -sf /usr/local/share/vcpkg/vcpkg /usr/local/bin/vcpkg

# --- containers -------------------------------------------------------------------
apt_install \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
  podman buildah skopeo amazon-ecr-credential-helper
sudo systemctl enable docker.service containerd.service >/dev/null 2>&1
sudo usermod -aG docker "$(id -un)"

# --- Kubernetes, HashiCorp, cloud CLIs --------------------------------------------------
apt_install packer azure-cli google-cloud-cli
sudo install -m 0755 "$TOOLS/$YQ_FILE" /usr/local/bin/yq
sudo install -m 0755 "$TOOLS/$KUBECTL_FILE" /usr/local/bin/kubectl
sudo install -m 0755 "$TOOLS/$KIND_FILE" /usr/local/bin/kind
sudo install -m 0755 "$TOOLS/$MINIKUBE_FILE" /usr/local/bin/minikube
sudo install -m 0755 "$TOOLS/$BICEP_FILE" /usr/local/bin/bicep
sudo tar -xzf "$TOOLS/$HELM_FILE" -C /usr/local/bin --strip-components=1 --no-same-owner linux-arm64/helm
sudo tar -xzf "$TOOLS/$KUSTOMIZE_FILE" -C /usr/local/bin --no-same-owner kustomize
sudo tar -xzf "$TOOLS/$AZCOPY_FILE" -C /usr/local/bin --strip-components=1 --no-same-owner \
  "azcopy_linux_arm64_$AZCOPY_VERSION/azcopy"
sudo ln -sf azcopy /usr/local/bin/azcopy10
rm -rf /tmp/tartci-aws && mkdir /tmp/tartci-aws
unzip -q "$TOOLS/$AWSCLI_FILE" -d /tmp/tartci-aws/cli
sudo /tmp/tartci-aws/cli/aws/install -i /usr/local/aws-cli -b /usr/local/bin >/dev/null
unzip -q "$TOOLS/$SAM_FILE" -d /tmp/tartci-aws/sam
sudo /tmp/tartci-aws/sam/install -i /usr/local/aws-sam-cli -b /usr/local/bin >/dev/null
rm -rf /tmp/tartci-aws
sudo dpkg -i "$TOOLS/$SSM_FILE" >/dev/null
# The azure-devops extension, in the image-wide extension dir GitHub's image uses, from the pinned
# wheel (--yes: a local wheel is not in the extension index's signature list).
sudo env AZURE_EXTENSION_DIR=/opt/az/azcliextensions \
  az extension add --source "$TOOLS/$AZDEVOPS_FILE" --yes --only-show-errors >/dev/null

# --- databases and web servers: installed, services disabled (as on GitHub's image) ----
# No service starts while these install (apache2 and nginx both want port 80).
printf '#!/bin/sh\nexit 101\n' | sudo tee /usr/sbin/policy-rc.d >/dev/null
sudo chmod 0755 /usr/sbin/policy-rc.d
apt_install postgresql libpq-dev mysql-server mysql-client libmysqlclient-dev apache2 nginx
sudo rm -f /usr/sbin/policy-rc.d
# GitHub's MySQL: user root, password root.
sudo systemctl start mysql.service
sudo mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY 'root'; FLUSH PRIVILEGES;"
for svc in mysql postgresql apache2 nginx; do
  sudo systemctl disable --now "$svc.service" >/dev/null 2>&1
done

# --- browsers -----------------------------------------------------------------------
# Unprivileged user namespaces, as browser sandboxes (Firefox, Chrome, Electron) and rootless
# Podman need: Ubuntu 24.04's AppArmor restricts them by default.
echo 'kernel.apparmor_restrict_unprivileged_userns = 0' | sudo tee /etc/sysctl.d/60-tartci-userns.conf >/dev/null
sudo sysctl -q -p /etc/sysctl.d/60-tartci-userns.conf
apt_install firefox
sudo install -d /usr/local/share/gecko_driver
sudo tar -xzf "$TOOLS/$GECKO_FILE" -C /usr/local/share/gecko_driver --no-same-owner
sudo ln -sf /usr/local/share/gecko_driver/geckodriver /usr/local/bin/geckodriver

# --- Node.js LTS with npm and npx on the default PATH (/usr/local/bin) ------------
# Only the install tree (not the top-level README/LICENSE/CHANGELOG), and root-owned: the
# archive's entries belong to uid 1001, which would otherwise own /usr/local/bin and friends.
NODE_DIR="${NODE_TARBALL%.tar.xz}"
sudo tar -xJf "/tmp/$NODE_TARBALL" -C /usr/local --strip-components=1 --no-same-owner \
  "$NODE_DIR/bin" "$NODE_DIR/include" "$NODE_DIR/lib" "$NODE_DIR/share"
rm -f "/tmp/$NODE_TARBALL"

# --- GitHub CLI and Yarn classic in /usr/local/bin ---------------------------------
GH_DIR="${GH_TARBALL%.tar.gz}"
sudo tar -xzf "/tmp/$GH_TARBALL" -C /usr/local --strip-components=1 --no-same-owner \
  "$GH_DIR/bin/gh" "$GH_DIR/share/man"
rm -f "/tmp/$GH_TARBALL"
sudo /usr/local/bin/npm install --global --no-fund --no-audit --loglevel=error "yarn@$YARN_VERSION"

# --- arm64 runner (the linux-a64 lane; runner.sh `cd ~/actions-runner`) --------
rm -rf "$HOME/actions-runner" "$HOME/actions-runner-x64"
mkdir "$HOME/actions-runner" "$HOME/actions-runner-x64"
tar -xzf "/tmp/actions-runner-linux-arm64-$RUNNER_VERSION.tar.gz" -C "$HOME/actions-runner"
tar -xzf "/tmp/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz" -C "$HOME/actions-runner-x64"
rm -f /tmp/actions-runner-linux-*.tar.gz
# The arm64 runner's own dependency installer (native arch, before multiarch).
sudo "$HOME/actions-runner/bin/installdependencies.sh" >/dev/null

# --- the runner environment GitHub's image sets ----------------------------------------
# /etc/environment reaches every login (pam_env); each runner's .env reaches its jobs (the
# runner loads it at start, and the lanes keep lines they do not own). The x64 runner's
# containers default to amd64, which run under Rosetta like its other x64 binaries.
sudo install -d -o "$(id -u)" -g "$(id -g)" /opt/hostedtoolcache
{
  echo "PATH=$HOME/.cargo/bin:$HOME/.local/bin:/opt/pipx_bin:$HOME/.dotnet/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin"
  echo "ImageOS=ubuntu24"
  echo "ImageVersion=$(date -u +%Y%m%d).tartci"
  echo "RUNNER_TOOL_CACHE=/opt/hostedtoolcache"
  echo "AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache"
  echo "XDG_CONFIG_HOME=$HOME/.config"
  echo "JAVA_HOME=/usr/lib/jvm/temurin-25-jdk-arm64"
  for v in 11 17 21 25; do
    echo "JAVA_HOME_${v}_X64=/usr/lib/jvm/temurin-$v-jdk-arm64"
    echo "JAVA_HOME_${v}_ARM64=/usr/lib/jvm/temurin-$v-jdk-arm64"
  done
  echo "ANT_HOME=/usr/share/ant"
  echo "GRADLE_HOME=/usr/share/gradle-$GRADLE_VERSION"
  echo "PIPX_HOME=/opt/pipx"
  echo "PIPX_BIN_DIR=/opt/pipx_bin"
  echo "VCPKG_INSTALLATION_ROOT=/usr/local/share/vcpkg"
  echo "NVM_DIR=$HOME/.nvm"
  echo "AZURE_EXTENSION_DIR=/opt/az/azcliextensions"
  echo "GECKOWEBDRIVER=/usr/local/share/gecko_driver"
  echo "DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1"
  echo "DOTNET_NOLOGO=1"
  echo "DOTNET_MULTILEVEL_LOOKUP=0"
} >/tmp/tartci-image.env
sudo install -m 0644 /tmp/tartci-image.env /etc/environment
install -m 0644 /tmp/tartci-image.env "$HOME/actions-runner/.env"
{ cat /tmp/tartci-image.env; echo "DOCKER_DEFAULT_PLATFORM=linux/amd64"; } >"$HOME/actions-runner-x64/.env"
rm -f /tmp/tartci-image.env

# --- amd64 multiarch for the x64 runner under Rosetta -------------------------
# The x64 runner's own libraries, plus the runtime libraries x64 binaries downloaded by setup-*
# actions expect (a standalone x64 pnpm needs libatomic; x64 Python builds need libffi, libssl,
# libsqlite3, libreadline, ...). The arm64 sources stay on ports.
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
AMD64_LIBS="libc6 libstdc++6 libgcc-s1 libatomic1 libgomp1 zlib1g libzstd1 liblzma5 libbz2-1.0
  libssl3t64 libcurl4t64 libnghttp2-14 libkrb5-3 libgssapi-krb5-2 libffi8 libexpat1 libxml2
  libyaml-0-2 libreadline8t64 libedit2 libncurses6 libncursesw6 libtinfo6 libsqlite3-0 libgdbm6t64
  libuuid1 libgmp10 libpcre2-8-0 libicu74 liblttng-ust1t64"
# shellcheck disable=SC2046,SC2086  # one package per word
apt_install $(printf '%s:amd64 ' $AMD64_LIBS)
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
# The first non-empty output line of a command that must succeed (a failure fails the assignment).
first_line(){ local out; out="$("$@" 2>&1)" || { echo "self-check failed: $*" >&2; return 1; }; printf '%s\n' "$out" | awk 'NF { print; exit }'; }
arm64_version="$("$HOME/actions-runner/bin/Runner.Listener" --version)"
echo "arm64 runner: $arm64_version"
node_version="$(/usr/local/bin/node --version)"; npm_version="$(/usr/local/bin/npm --version)"
echo "node $node_version, npm $npm_version"
# The whole output, then its first line: `gh --version | head -1` can fail under pipefail.
gh_version="$(/usr/local/bin/gh --version)"; gh_version="${gh_version%%$'\n'*}"
yarn_version="$(/usr/local/bin/yarn --version)"
echo "$gh_version, yarn $yarn_version"
for tool in node npm npx gh yarn; do
  check [ "$(command -v "$tool")" = "/usr/local/bin/$tool" ]
done
check [ "$(stat -c %u /usr/local/bin)" = 0 ]
check test -x "$HOME/actions-runner/run.sh"
check test -x "$HOME/actions-runner-x64/run.sh"
# The GitHub-image toolset, resolved through the runner's own environment, one version line each.
set -a
# shellcheck disable=SC1091
. "$HOME/actions-runner/.env"
set +a
TOOL_CHECKS=(
  "gcc --version" "g++ --version" "gfortran --version" "gcc-14 --version" "clang --version"
  "clang-format --version" "clang-tidy --version" "cmake --version" "ninja --version"
  "make --version" "autoconf --version" "git --version" "git-lfs --version" "jq --version"
  "yq --version" "zstd --version" "aria2c --version" "shellcheck --version" "parallel --version"
  "python --version" "python3 --version" "pip3 --version" "pipx --version"
  "docker --version" "docker buildx version" "docker compose version" "podman --version"
  "buildah --version" "skopeo --version" "kubectl version --client" "helm version --short"
  "kustomize version" "kind version" "minikube version --short" "packer version"
  "aws --version" "sam --version" "session-manager-plugin --version" "az --version"
  "gcloud --version" "azcopy --version" "bicep --version" "java -version" "javac -version"
  "mvn -v" "gradle --version" "ant -version" "kotlin -version" "go version" "rustc --version"
  "cargo --version" "rustfmt --version" "ruby --version" "gem --version" "php --version"
  "composer --version" "perl --version" "dotnet --version" "pwsh --version" "sqlite3 --version"
  "psql --version" "mysql --version" "ansible --version" "yamllint --version" "vcpkg version"
  "firefox --version" "geckodriver --version"
)
for entry in "${TOOL_CHECKS[@]}"; do  # bash32-lint: nonempty literal list just above
  # shellcheck disable=SC2086  # "tool args" splits into the command line
  line="$(first_line $entry)"
  printf 'TARTCI_TOOL %-28s %s\n' "$entry" "$line"
done
for tool in 7z bazel bazelisk docker-credential-ecr-login xvfb-run; do
  check command -v "$tool" >/dev/null
done
for v in 11 17 21 25; do
  jh="JAVA_HOME_${v}_X64"
  check test -x "${!jh}/bin/java"
done
check [ "$(java -version 2>&1 | awk -F'"' 'NR==1 { split($2, v, "."); print v[1] }')" = 25 ]
check test -w "$RUNNER_TOOL_CACHE"
# shellcheck disable=SC2154  # ImageOS comes from the runner .env sourced above
check [ "$ImageOS" = ubuntu24 ]
check grep -qx 'DOCKER_DEFAULT_PLATFORM=linux/amd64' "$HOME/actions-runner-x64/.env"
for svc in mysql postgresql apache2 nginx; do
  check [ "$(systemctl is-enabled "$svc.service" 2>/dev/null)" = disabled ]
done
# hello-world by its multi-arch index digest, so the self-check runs a known image on both arches.
HELLO_WORLD=hello-world@sha256:1e4c05b897a751332ce85c7e9449da2d93514ea2dea6dbb0e2753c9e949b2bfa
check sudo docker run --rm "$HELLO_WORLD" >/dev/null
check sg docker -c 'docker info --format "{{.ServerVersion}}"' >/dev/null
# shellcheck disable=SC2086  # one package per word
for p in $AMD64_LIBS; do
  check dpkg-query -W "$p:amd64" >/dev/null
done
if [ "$ENABLE_ROSETTA" = 1 ]; then
  sudo /usr/local/sbin/tartci-rosetta-boot
  x64_version="$("$HOME/actions-runner-x64/bin/Runner.Listener" --version)"
  echo "x64 runner under Rosetta: $x64_version"
  x64_node=""  # the runner's newest glibc Node (externals also hold *_alpine musl builds)
  for n in "$HOME"/actions-runner-x64/externals/node*/bin/node; do
    case "$n" in *alpine*) ;; *) x64_node="$n" ;; esac
  done
  check test -x "$x64_node"
  check [ "$("$x64_node" -p process.arch)" = x64 ]
  # The amd64 loader finds the libraries x64 downloads need (a missing libatomic broke pnpm's x64
  # binary): the x64 Node dlopens each one under Rosetta. ldconfig -p cannot tell, it labels the
  # x86-64 entries AArch64 on this host.
  for so in libatomic.so.1 libssl.so.3 libcurl.so.4 libffi.so.8 libz.so.1 liblzma.so.5 libbz2.so.1.0 \
      libreadline.so.8 libsqlite3.so.0 libtinfo.so.6 libicuuc.so.74 libgomp.so.1; do
    check "$x64_node" -e 'try { process.dlopen({ exports: {} }, process.argv[1]) } catch (e) { if (/cannot open shared object/.test(e.message)) { console.error(e.message); process.exit(1) } }' "$so"
  done
  check sudo docker run --rm --platform linux/amd64 "$HELLO_WORLD" >/dev/null
fi
sudo apt-get clean
sudo docker image prune -af >/dev/null
sudo docker builder prune -af >/dev/null
rm -rf "$TOOLS" "$HOME/.cache" "$HOME/.azure" "$HOME/.npm" "$HOME/.dotnet" "$HOME/.config/gcloud"
sudo rm -f /var/lib/dhcp/*.leases 2>/dev/null || true
sudo fstrim -a >/dev/null 2>&1 || true
sync
} </dev/null
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
