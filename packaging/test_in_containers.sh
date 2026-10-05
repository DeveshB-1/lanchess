#!/usr/bin/env bash
# Install and smoke-test the LAN Chess .deb and Arch packages in fresh containers.
#
#   packaging/test_in_containers.sh [--only CASE,...] [--list] [--jobs N] [--timeout SECS] [--no-lint] [--dump DIR]
#
# Run `make dist` (packaging/build_packages.py) first. dist/ is mounted
# read-only at /dist and the source tree read-only at /src. Every case runs in
# a new container. Install cases install the package, check the file layout
# and the man page, run `lanchess --version` and a scripted fool's-mate game
# that must print "Checkmate", uninstall, and check that /usr/bin/lanchess and
# /usr/share/lanchess are gone. Other cases build the Arch package with makepkg
# as a regular user, test install.sh, and run lintian and namcap.
#
# Environment: CONTAINER_ENGINE (podman or docker; default: podman if found),
# DIST (default: ./dist), LOG_DIR (default: build/container-tests), JOBS
# (default 4), CASE_TIMEOUT (seconds per case, default 900).
# Exit status is non-zero when any case fails or times out.

set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DIST=${DIST:-$ROOT/dist}
LOG_DIR=${LOG_DIR:-$ROOT/build/container-tests}
JOBS=${JOBS:-4}
CASE_TIMEOUT=${CASE_TIMEOUT:-900}
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/lanchess/__init__.py")

IMG_UBUNTU20=docker.io/library/ubuntu:20.04
IMG_UBUNTU22=docker.io/library/ubuntu:22.04
IMG_UBUNTU24=docker.io/library/ubuntu:24.04
IMG_UBUNTU26=docker.io/library/ubuntu:26.04
IMG_UBUNTU_LATEST=docker.io/library/ubuntu:latest   # used when 26.04 cannot be pulled
IMG_ARCH=docker.io/library/archlinux:latest
IMG_ALPINE=docker.io/library/alpine:latest

DEB="lanchess_${VERSION}-1_all.deb"
ARCHPKG="lanchess-${VERSION}-1-any.pkg.tar.zst"
SDIST="lanchess-${VERSION}.tar.gz"

# ---------------------------------------------------------------------------
# Shell functions available inside every container (POSIX sh).
read -r -d '' PRELUDE <<'EOF' || true
set -eu
fail() { echo "FAIL: $*"; exit 1; }
ok() { echo "ok: $*"; }
step() { echo; echo "--- $*"; }

# apt_prep: network timeouts, keep man pages (minimised images drop them),
# refresh the package lists.
apt_prep() {
    export DEBIAN_FRONTEND=noninteractive
    printf '%s\n' 'Acquire::http::Timeout "20";' 'Acquire::https::Timeout "20";' 'Acquire::Retries "2";' \
        >/etc/apt/apt.conf.d/99lanchess-test
    rm -f /etc/dpkg/dpkg.cfg.d/excludes
    apt-get update -q >/dev/null
}

# pacman_prep [PACKAGE...]: keep man pages, bring the image up to date (as a
# real Arch system is) and install PACKAGE...
pacman_prep() {
    sed -i '/^NoExtract/d' /etc/pacman.conf
    pacman -Syu --noconfirm --needed "$@" >/dev/null
}

# smoke CMD: --version and a scripted checkmate game.
smoke() {
    cmd=$1
    step "smoke test: $cmd"
    out=$("$cmd" --version 2>&1) || fail "'$cmd --version' exited non-zero: $out"
    case "$out" in *"$VERSION"*) ok "--version -> $out" ;; *) fail "--version printed '$out', expected $VERSION" ;; esac
    rc=0
    game=$(printf 'f3\ne5\ng4\nQh4#\n/quit\n' | timeout 30 "$cmd" local --plain --no-save 2>&1) || rc=$?
    [ "$rc" -ne 124 ] || { echo "$game" | tail -n 20; fail "scripted game timed out after 30 s"; }
    if echo "$game" | grep -q "Checkmate"; then
        ok "scripted game printed Checkmate (exit status $rc)"
    else
        echo "$game" | tail -n 25
        fail "scripted game output has no 'Checkmate' (exit status $rc)"
    fi
}

# check_layout: the files `make install PREFIX=/usr` puts in place.
check_layout() {
    step "file layout"
    for f in /usr/bin/lanchess /usr/share/lanchess/lanchess/__init__.py \
             /usr/share/lanchess/lanchess/cli.py /usr/share/lanchess/lanchess/engine.py \
             /usr/share/man/man6/lanchess.6.gz /usr/share/applications/lanchess.desktop \
             /usr/share/icons/hicolor/scalable/apps/lanchess.svg \
             /usr/lib/firewalld/services/lanchess.xml /etc/ufw/applications.d/lanchess; do
        [ -e "$f" ] || fail "missing $f"
    done
    [ -x /usr/bin/lanchess ] || fail "/usr/bin/lanchess is not executable"
    head -n 1 /usr/bin/lanchess | grep -qx '#!/usr/bin/python3' || fail "unexpected launcher shebang: $(head -n 1 /usr/bin/lanchess)"
    if ls /usr/share/lanchess/lanchess/*.pyc /usr/share/lanchess/lanchess/__pycache__ >/dev/null 2>&1; then
        fail ".pyc files or __pycache__ in /usr/share/lanchess"
    fi
    gzip -dc /usr/share/man/man6/lanchess.6.gz | grep -q "^\.TH LANCHESS 6 .*\"lanchess $VERSION\"" ||
        fail "man page has no .TH line for version $VERSION"
    page=$(man -w lanchess 2>/dev/null || true)
    case "$page" in
        */man6/lanchess.6*) ok "man page: man -w lanchess -> $page" ;;
        *) ok "man page present: /usr/share/man/man6/lanchess.6.gz (no man command in this image)" ;;
    esac
    v=$(/usr/bin/python3 -B -c 'import sys; sys.path.insert(0, "/usr/share/lanchess"); import lanchess; print(lanchess.__version__)')
    [ "$v" = "$VERSION" ] || fail "installed package reports version $v"
    ok "layout complete, package version $v, python $(/usr/bin/python3 -B -c 'import platform; print(platform.python_version())')"
}

check_removed() {
    step "after uninstall"
    for f in /usr/bin/lanchess /usr/games/lanchess /usr/share/lanchess /usr/share/man/man6/lanchess.6.gz \
             /usr/share/applications/lanchess.desktop /usr/lib/firewalld/services/lanchess.xml; do
        [ ! -e "$f" ] || fail "$f still exists after uninstall"
    done
    ok "/usr/bin/lanchess, /usr/share/lanchess and the other package files are gone"
}

# installsh_tests REMOVE_CMD PACKAGE DECOY: install.sh from a local directory,
# then from a local HTTP mirror laid out like GitHub releases (a tampered file
# must be refused, the good one installed, LANCHESS_VERSION must pick that
# release). From a local directory install.sh must take the PACKAGE that
# SHA256SUMS lists, not DECOY (a stray file that sorts after it, as an older
# 1.9.0 sorts after 1.10.0), and without SHA256SUMS must refuse to choose
# between the two unless LANCHESS_VERSION names one. Needs python3 and curl.
# REMOVE_CMD uninstalls the package.
installsh_tests() {
    remove=$1 pkg=$2 decoy=$3
    step 'install.sh with LANCHESS_LOCAL_DIR=/dist'
    rc=0; out=$(LANCHESS_LOCAL_DIR=/dist sh /src/install.sh 2>&1) || rc=$?
    echo "$out"
    [ "$rc" -eq 0 ] || fail "install.sh exited with status $rc"
    echo "$out" | grep -qF "checksum OK for $pkg" || fail "install.sh did not check $pkg against /dist/SHA256SUMS"
    check_layout
    smoke lanchess
    $remove >/dev/null
    check_removed

    step "install.sh with LANCHESS_LOCAL_DIR: the $pkg SHA256SUMS lists, not $decoy"
    mkdir -p /tmp/local
    cp "/dist/$pkg" /dist/SHA256SUMS /tmp/local/
    printf 'not a package\n' >"/tmp/local/$decoy"
    rc=0; out=$(LANCHESS_LOCAL_DIR=/tmp/local sh /src/install.sh 2>&1) || rc=$?
    echo "$out"
    [ "$rc" -eq 0 ] || fail "install.sh exited with status $rc"
    echo "$out" | grep -qF "checksum OK for $pkg" || fail "install.sh did not install the $pkg that SHA256SUMS lists"
    smoke lanchess
    $remove >/dev/null
    check_removed

    step 'install.sh with LANCHESS_LOCAL_DIR and no SHA256SUMS: two packages are refused'
    rm /tmp/local/SHA256SUMS
    rc=0; out=$(LANCHESS_LOCAL_DIR=/tmp/local sh /src/install.sh 2>&1) || rc=$?
    echo "$out"
    [ "$rc" -ne 0 ] || fail 'install.sh chose one of two packages by itself'
    echo "$out" | grep -F "$pkg" | grep -qF "$decoy" || fail 'the error does not name both files'
    [ ! -e /usr/bin/lanchess ] || fail 'something was installed'
    ok "refused with exit status $rc"

    step "install.sh with LANCHESS_LOCAL_DIR, no SHA256SUMS and LANCHESS_VERSION=$VERSION"
    rc=0; out=$(LANCHESS_VERSION=$VERSION LANCHESS_LOCAL_DIR=/tmp/local sh /src/install.sh 2>&1) || rc=$?
    echo "$out"
    [ "$rc" -eq 0 ] || fail "install.sh exited with status $rc"
    echo "$out" | grep -qF "warning: /tmp/local has no SHA256SUMS; installing $pkg without a checksum check" ||
        fail 'no warning that the file is installed without a checksum check'
    smoke lanchess
    $remove >/dev/null
    check_removed

    for d in /srv/good/latest/download /srv/good/download/v$VERSION /srv/bad/latest/download; do
        mkdir -p "$d" && cp /dist/* "$d/"
    done
    for f in /srv/bad/latest/download/*.deb /srv/bad/latest/download/*.pkg.tar.zst; do
        printf 'tampered' >>"$f"
    done
    (cd /srv && exec python3 -m http.server 8765 --bind 127.0.0.1 >/dev/null 2>&1) &
    i=0
    until curl -fsS -o /dev/null http://127.0.0.1:8765/good/latest/download/SHA256SUMS; do
        i=$((i + 1)); [ $i -lt 40 ] || fail 'mirror did not start'; sleep 0.5
    done

    step 'install.sh refuses a download whose checksum does not match SHA256SUMS'
    if LANCHESS_RELEASES_URL=http://127.0.0.1:8765/bad sh /src/install.sh; then fail 'tampered package accepted'; fi
    [ ! -e /usr/bin/lanchess ] || fail 'tampered package was installed'
    ok 'tampered download rejected'

    step 'install.sh downloads the latest release listed in SHA256SUMS and verifies it'
    LANCHESS_RELEASES_URL=http://127.0.0.1:8765/good sh /src/install.sh || fail "install.sh exited with status $?"
    check_layout
    smoke lanchess
    $remove >/dev/null
    check_removed

    step "install.sh with LANCHESS_VERSION=$VERSION (download/v$VERSION/)"
    LANCHESS_VERSION=$VERSION LANCHESS_RELEASES_URL=http://127.0.0.1:8765/good sh /src/install.sh >/dev/null ||
        fail "install.sh exited with status $?"
    smoke lanchess
    $remove >/dev/null
    check_removed
}

# namcap_check FILE...: namcap must report no errors or warnings, except the
# msvcrt false positive (the Windows-only stdlib module term.py imports on
# Windows). namcap resolves the package's private modules in the installed
# /usr/share/lanchess, so install the package first. It also looks up script
# interpreters on PATH; use Arch's login PATH (the container default puts
# /usr/sbin, a symlink to /usr/bin, first, so /usr/bin/python3 looks unowned).
namcap_check() {
    step "namcap $(pacman -Q namcap)"
    PATH=/usr/local/sbin:/usr/local/bin:/usr/bin namcap -i "$@" 2>&1 | tee /tmp/namcap.txt
    msvcrt="Referenced python module 'msvcrt' is an uninstalled dependency"
    grep -E ' [EW]: ' /tmp/namcap.txt >/tmp/namcap-issues.txt || true
    grep -F "$msvcrt" /tmp/namcap-issues.txt | sed 's/^/allowed (Windows-only stdlib module): /' || true
    if grep -vF "$msvcrt" /tmp/namcap-issues.txt; then fail 'namcap reported errors or warnings'; fi
    ok "namcap: no errors or warnings in $*"
}
EOF

# ---------------------------------------------------------------------------
# Test cases: name -> image and script.

declare -A IMAGE SCRIPT
CASES=()
add_case() { CASES+=("$1"); IMAGE[$1]=$2; SCRIPT[$1]=$3; }

# deb_case NAME IMAGE [EXTRA-SCRIPT]
deb_case() {
    add_case "deb-$1" "$2" "
apt_prep
step 'apt-get install ./$DEB'
apt-get install -y -q /dist/$DEB
dpkg -s lanchess | sed -n 's/^\(Version\|Depends\|Installed-Size\):/  &/p'
check_layout
[ -x /usr/games/lanchess ] && [ -L /usr/bin/lanchess ] || fail 'expected /usr/games/lanchess plus the /usr/bin/lanchess symlink'
ok \"/usr/bin/lanchess -> \$(readlink /usr/bin/lanchess)\"
smoke lanchess
step 'apt-get purge lanchess'
apt-get purge -y -q lanchess
check_removed
[ ! -e /etc/ufw/applications.d/lanchess ] || fail 'conffile left after purge'
${3:-}"
}
deb_case ubuntu-20.04 "$IMG_UBUNTU20" "
step 'build_packages.py --only deb on Python 3.8'
apt-get install -y -q make >/dev/null
python3 /src/packaging/build_packages.py --only deb --outdir /tmp/out38 --strict
dpkg-deb --info /tmp/out38/$DEB | sed -n 's/^ \(Package\|Version\|Depends\):/  &/p'
"
deb_case ubuntu-22.04 "$IMG_UBUNTU22"
deb_case ubuntu-24.04 "$IMG_UBUNTU24"
deb_case ubuntu-26.04 "$IMG_UBUNTU26"

add_case arch "$IMG_ARCH" "
pacman_prep python   # the dependency, so -U needs no download
step 'pacman -U ./$ARCHPKG'
pacman -U --noconfirm /dist/$ARCHPKG
pacman -Qi lanchess | grep -E '^(Version|Depends On|Backup Files)' | sed 's/^/  /'
check_layout
smoke lanchess
step 'pacman -R lanchess'
pacman -R --noconfirm lanchess
check_removed
"

# The release build: build_packages.py --only arch as a regular user (makepkg
# refuses to run as root), as CI does.
add_case build-arch "$IMG_ARCH" "
pacman_prep base-devel python
useradd -m builder
step 'build_packages.py --only arch as a regular user (makepkg)'
su builder -c 'python3 /src/packaging/build_packages.py --only arch --outdir /tmp/out --strict'
pacman -Qip /tmp/out/$ARCHPKG | grep -E '^(Name|Version|Architecture|Depends On|Backup Files|Packager)' | sed 's/^/  /'
pacman -U --noconfirm /tmp/out/$ARCHPKG >/dev/null
check_layout
smoke lanchess
pacman -R --noconfirm lanchess >/dev/null
check_removed
"

# The AUR recipe as an AUR user builds it: build_packages.py --aur writes the
# ready-to-push PKGBUILD (real checksum) and .SRCINFO for a source tarball laid
# out like GitHub's tag archive; makepkg builds it, running check() (the unit
# tests). The tarball sits next to the PKGBUILD under the name source=()
# gives it, so makepkg verifies it against sha256sums instead of downloading.
add_case aur-makepkg "$IMG_ARCH" "
pacman_prep base-devel python hicolor-icon-theme namcap
useradd -m builder
step 'build_packages.py --aur: PKGBUILD and .SRCINFO for the source tarball'
su builder -c 'python3 /src/packaging/build_packages.py --only tarball --outdir ~/tarball --strict'
su builder -c 'python3 /src/packaging/build_packages.py --aur ~/aur --tarball ~/tarball/$SDIST'
su builder -c 'cp ~/tarball/$SDIST ~/aur/'
grep -E '^(# Maintainer|pkgver|pkgrel|source|sha256sums)' /home/builder/aur/PKGBUILD | sed 's/^/  /'
su builder -c 'cd ~/aur && makepkg --printsrcinfo' | cmp - /home/builder/aur/.SRCINFO || fail '.SRCINFO does not match the PKGBUILD'
grep -q \"sha256sums = \$(sha256sum /home/builder/tarball/$SDIST | cut -d' ' -f1)\" /home/builder/aur/.SRCINFO ||
    fail 'wrong checksum in .SRCINFO'
grep -q 'source = lanchess-$VERSION.tar.gz::https://github.com/DeveshB-1/lanchess/archive/refs/tags/v$VERSION.tar.gz' \
    /home/builder/aur/.SRCINFO || fail '.SRCINFO source is not the GitHub tag archive'
ok '.SRCINFO matches the PKGBUILD, the tarball checksum and the GitHub tag archive URL'
step 'makepkg as a regular user (check() runs the unit tests)'
su builder -c 'cd ~/aur && makepkg --noconfirm --noprogressbar' || fail 'makepkg failed'
pkg=/home/builder/aur/$ARCHPKG
[ -f \$pkg ] || fail 'makepkg did not produce $ARCHPKG'
pacman -U --noconfirm \$pkg >/dev/null
namcap_check /home/builder/aur/PKGBUILD \$pkg
check_layout
smoke lanchess
pacman -R --noconfirm lanchess >/dev/null
check_removed
"

add_case installsh-ubuntu-24.04 "$IMG_UBUNTU24" "
apt_prep
apt-get install -y -q curl python3 >/dev/null
installsh_tests 'apt-get purge -y -q lanchess' $DEB lanchess_9.9.9-1_all.deb
"

add_case installsh-arch "$IMG_ARCH" "
pacman_prep python
installsh_tests 'pacman -R --noconfirm lanchess' $ARCHPKG lanchess-9.9.9-1-any.pkg.tar.zst
"

# Neither Ubuntu/Debian nor Arch (busybox sh): install.sh must stop with the
# run-from-source instructions and install nothing.
add_case installsh-unsupported "$IMG_ALPINE" "
step 'install.sh on Alpine Linux'
rc=0; out=\$(LANCHESS_LOCAL_DIR=/dist sh /src/install.sh 2>&1) || rc=\$?
echo \"\$out\"
[ \$rc -ne 0 ] || fail 'install.sh succeeded on an unsupported system'
echo \"\$out\" | grep -qF 'git clone https://github.com/DeveshB-1/lanchess && cd lanchess && python3 play.py' ||
    fail 'no run-from-source command in the message'
echo \"\$out\" | grep -qF 'Ubuntu' || fail 'the message does not name the supported systems'
[ ! -e /usr/bin/lanchess ] || fail 'something was installed'
ok \"install.sh refused with exit status \$rc and the run-from-source command\"
"

add_case lint-deb "$IMG_UBUNTU24" "
apt_prep
apt-get install -y -q lintian >/dev/null
step 'dpkg-deb --info / --contents'
dpkg-deb --info /dist/$DEB
dpkg-deb --contents /dist/$DEB | awk '{ print \$1, \$2, \$6, \$7, \$8 }'
step \"lintian \$(lintian --version)\"
lintian --display-info --display-experimental --pedantic --show-overrides --color never /dist/$DEB >/tmp/lintian.txt 2>&1 || true
cat /tmp/lintian.txt
if grep -E '^[EW]: ' /tmp/lintian.txt; then fail 'lintian reported errors or warnings'; fi
ok 'lintian: 0 errors, 0 warnings'
"

add_case lint-arch "$IMG_ARCH" "
pacman_prep namcap
pacman -U --noconfirm /dist/$ARCHPKG >/dev/null
namcap_check /dist/$ARCHPKG /src/packaging/aur/PKGBUILD
"

# ---------------------------------------------------------------------------

usage() {
    sed -n '2,18s/^# \{0,1\}//p' "$0"
    echo "Cases: ${CASES[*]}"
}

SELECTED=()
LINT=1
while [[ $# -gt 0 ]]; do
    case $1 in
        --only) IFS=, read -r -a SELECTED <<<"$2"; shift ;;
        --only=*) IFS=, read -r -a SELECTED <<<"${1#--only=}" ;;
        --jobs) JOBS=$2; shift ;;
        --jobs=*) JOBS=${1#--jobs=} ;;
        --timeout) CASE_TIMEOUT=$2; shift ;;
        --timeout=*) CASE_TIMEOUT=${1#--timeout=} ;;
        --no-lint) LINT=0 ;;
        --list) printf '%s\n' "${CASES[@]}"; exit 0 ;;
        --dump)
            # Write each case's in-container script to DIR (for shellcheck/debugging).
            mkdir -p "$2"
            for c in "${CASES[@]}"; do
                printf '#!/bin/sh\n%s\n%s\necho; echo PASS\n' "$PRELUDE" "${SCRIPT[$c]}" >"$2/$c.sh"
            done
            echo "wrote ${#CASES[@]} scripts to $2"
            exit 0 ;;
        -h | --help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done
[[ $JOBS =~ ^[1-9][0-9]*$ ]] || { echo "error: --jobs needs a positive number" >&2; exit 2; }
[[ $CASE_TIMEOUT =~ ^[1-9][0-9]*$ ]] || { echo "error: --timeout needs a number of seconds" >&2; exit 2; }
if [[ ${#SELECTED[@]} -eq 0 ]]; then
    for c in "${CASES[@]}"; do
        if [[ $LINT -eq 0 && $c == lint-* ]]; then continue; fi
        SELECTED+=("$c")
    done
fi
for c in "${SELECTED[@]}"; do
    [[ -n ${IMAGE[$c]:-} ]] || { echo "unknown case: $c (try --list)" >&2; exit 2; }
done

if [[ -n ${CONTAINER_ENGINE:-} ]]; then
    ENGINE=$CONTAINER_ENGINE
elif command -v podman >/dev/null 2>&1; then
    ENGINE=podman
elif command -v docker >/dev/null 2>&1; then
    ENGINE=docker
else
    echo "error: neither podman nor docker is installed" >&2
    exit 2
fi

[[ -d $DIST ]] || { echo "error: $DIST does not exist; run make dist first" >&2; exit 2; }
DIST=$(cd "$DIST" && pwd)
for f in "$DEB" "$ARCHPKG" SHA256SUMS; do
    [[ -f $DIST/$f ]] || echo "warning: $DIST/$f is missing; cases that need it will fail" >&2
done

mkdir -p "$LOG_DIR"
NAME_PREFIX="lanchess-test-$$"

# Remove this run's containers (after a timeout, or when interrupted).
remove_containers() {
    for c in "${SELECTED[@]}"; do
        "$ENGINE" rm -f "$NAME_PREFIX-$c" >/dev/null 2>&1 || true
    done
}
trap 'remove_containers; exit 130' INT TERM

# Pull missing images (docker's --pull=never needs them present). Ubuntu
# 26.04 falls back to ubuntu:latest when the registry does not have it.
have_image() {
    "$ENGINE" image inspect "$1" >/dev/null 2>&1 || timeout 600 "$ENGINE" pull -q "$1" >/dev/null 2>&1
}
for c in "${SELECTED[@]}"; do
    img=${IMAGE[$c]}
    have_image "$img" && continue
    if [[ $img == "$IMG_UBUNTU26" ]] && have_image "$IMG_UBUNTU_LATEST"; then
        echo "note: $img is not available; $c uses $IMG_UBUNTU_LATEST" >&2
        IMAGE[$c]=$IMG_UBUNTU_LATEST
    else
        echo "warning: cannot pull $img; $c will fail" >&2
    fi
done

echo "LAN Chess $VERSION: ${#SELECTED[@]} container cases with $ENGINE ($JOBS at a time," \
    "${CASE_TIMEOUT}s each), logs in $LOG_DIR"

run_case() {
    local name=$1 log="$LOG_DIR/$1.log" start rc=0
    start=$(date +%s)
    {
        echo "### $name on ${IMAGE[$name]}"
        # Read-only bind mounts; label=disable instead of relabelling (":Z"
        # would give each parallel container a private label on SELinux hosts).
        timeout -k 30 "$CASE_TIMEOUT" "$ENGINE" run --rm --pull=never --name "$NAME_PREFIX-$name" \
            --security-opt label=disable \
            -v "$DIST:/dist:ro" -v "$ROOT:/src:ro" \
            -e "VERSION=$VERSION" -e "LC_ALL=C.UTF-8" \
            "${IMAGE[$name]}" sh -c "$PRELUDE
${SCRIPT[$name]}
echo; echo PASS" || rc=$?
        if [[ $rc -eq 124 || $rc -eq 137 ]]; then
            echo "### TIMEOUT after ${CASE_TIMEOUT}s"
            "$ENGINE" rm -f "$NAME_PREFIX-$name" >/dev/null 2>&1 || true
            rc=124
        fi
        echo "### exit status $rc"
    } >"$log" 2>&1 </dev/null
    echo "$rc $(($(date +%s) - start))" >"$LOG_DIR/$name.status"
    case $rc in
        0) echo "  PASS     $name" ;;
        124) echo "  TIMEOUT  $name (see $log)" ;;
        *) echo "  FAIL     $name (see $log)" ;;
    esac
}

running=0
for c in "${SELECTED[@]}"; do
    rm -f "$LOG_DIR/$c.status"
    run_case "$c" &
    running=$((running + 1))
    if ((running >= JOBS)); then
        wait -n || true
        running=$((running - 1))
    fi
done
wait || true

echo
printf '%-24s %-8s %6s  %s\n' CASE RESULT TIME IMAGE
printf '%-24s %-8s %6s  %s\n' ---- ------ ---- -----
failures=0
for c in "${SELECTED[@]}"; do
    read -r rc secs <"$LOG_DIR/$c.status" 2>/dev/null || { rc=1; secs=0; }
    case $rc in
        0) result=PASS ;;
        124) result=TIMEOUT ;;
        *) result=FAIL ;;
    esac
    [[ $result == PASS ]] || failures=$((failures + 1))
    printf '%-24s %-8s %5ss  %s\n' "$c" "$result" "$secs" "${IMAGE[$c]}"
done
echo
if ((failures)); then
    echo "$failures of ${#SELECTED[@]} cases did not pass. Logs: $LOG_DIR"
    exit 1
fi
echo "All ${#SELECTED[@]} cases passed."
