#!/bin/sh
# LAN Chess installer for Ubuntu and Arch Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/DeveshB-1/lanchess/main/install.sh | sh
#
# Downloads the package for your system from the latest GitHub release, checks
# it against the release's SHA256SUMS and installs it:
#   Ubuntu, Debian and Debian-based systems   lanchess_V-1_all.deb with apt
#   Arch Linux and Arch-based systems         lanchess-V-1-any.pkg.tar.zst with pacman -U
# Packages exist for these two families only. Anywhere else, run LAN Chess
# from source (Python 3.8 or newer):
#   git clone https://github.com/DeveshB-1/lanchess && cd lanchess && python3 play.py
# Needs curl or wget, and sudo or doas unless run as root.
#
# Options:
#   --method deb|arch   use this package type instead of detecting it
#   -h, --help          show this help
# Environment:
#   LANCHESS_VERSION=1.2.3      install that release instead of the latest
#   LANCHESS_METHOD=deb|arch    same as --method
#   LANCHESS_LOCAL_DIR=DIR      install from local files (e.g. dist/), no download:
#                               the package DIR/SHA256SUMS lists, checksum checked
#   LANCHESS_RELEASES_URL=URL   a mirror laid out like GitHub releases
#                               (URL/latest/download/FILE, URL/download/vV/FILE)

set -eu

REPO="DeveshB-1/lanchess"
RELEASES_URL="${LANCHESS_RELEASES_URL:-https://github.com/$REPO/releases}"
SOURCE_CMD="git clone https://github.com/$REPO && cd lanchess && python3 play.py"
TMP_DIR=""
REQUIRE_CHECKSUM=0

say() { printf '%s\n' "lanchess-install: $*"; }
warn() { printf '%s\n' "lanchess-install: warning: $*" >&2; }
die() {
    printf '%s\n' "lanchess-install: error: $*" >&2
    exit 1
}
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    sed -n '2,/^$/s/^# \{0,1\}//p' "$0" 2>/dev/null || true
    printf '%s\n' "Usage: install.sh [--method deb|arch]"
}

cleanup() {
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
}

# Run a command as root (directly, or with sudo/doas). stdin is /dev/null so a
# piped `curl | sh` script is never read by the package manager.
as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@" </dev/null
    elif have sudo; then
        sudo "$@" </dev/null
    elif have doas; then
        doas "$@" </dev/null
    else
        die "root privileges are needed to install a package; install sudo or run this as root"
    fi
}

os_release_value() {
    # Prints the value of KEY from /etc/os-release, without quotes.
    if [ -r /etc/os-release ]; then
        sed -n "s/^$1=//p" /etc/os-release | head -n 1 | tr -d '"' | tr -d "'"
    fi
}

# Prints deb or arch, or nothing when there is no package for this system.
detect_method() {
    [ "$(uname -s)" = "Linux" ] || return 0
    # ID_LIKE names the parent distribution (Mint, Pop!_OS: "ubuntu debian";
    # Manjaro, EndeavourOS: "arch"); a few derivatives only set ID.
    for word in $(os_release_value ID) $(os_release_value ID_LIKE); do
        case "$word" in
            ubuntu | debian | linuxmint | pop | elementary | zorin | neon | raspbian | kali | devuan)
                if have apt-get; then echo deb; return 0; fi ;;
            arch | archlinux | manjaro | endeavouros | garuda | artix | cachyos)
                if have pacman; then echo arch; return 0; fi ;;
        esac
    done
}

unsupported() {
    system=$(os_release_value PRETTY_NAME)
    [ -n "$system" ] || system=$(uname -s)
    printf '%s\n' \
        "lanchess-install: LAN Chess has packages for Ubuntu (and other Debian-based systems)" \
        "lanchess-install: and Arch Linux only, and this system ($system) is neither." \
        "lanchess-install: Run it from source instead; it needs Python 3.8 or newer and git:" \
        "" \
        "    $SOURCE_CMD" \
        "" >&2
    exit 1
}

# The release file name for METHOD, as a glob; LANCHESS_VERSION narrows it to
# that version (lanchess_1.2.3-1_all.deb, lanchess-1.2.3-1-any.pkg.tar.zst).
asset_pattern() {
    v=${LANCHESS_VERSION:-}
    v=${v#v}
    [ -n "$v" ] || v='*'
    case "$1" in
        deb) echo "lanchess_${v}-*_all.deb" ;;
        arch) echo "lanchess-${v}-*-any.pkg.tar.zst" ;;
        *) die "unknown install method: $1 (use deb or arch)" ;;
    esac
}

# Prints the file that the SHA256SUMS file $1 lists for the glob $2, or
# nothing. A release lists one file per package type; more than one is an error
# rather than a guess.
listed_asset() {
    listed=""
    while read -r _ file || [ -n "${file:-}" ]; do
        file=${file#\*}
        # shellcheck disable=SC2254 # $2 is a glob on purpose
        case "$file" in
            $2)
                [ -z "$listed" ] || [ "$listed" = "$file" ] ||
                    die "SHA256SUMS lists more than one file matching $2 ($listed, $file); set LANCHESS_VERSION"
                listed="$file" ;;
        esac
    done <"$1"
    printf '%s\n' "$listed"
}

download() {
    # download URL DEST
    if have curl; then
        curl -fsSL --retry 3 --connect-timeout 20 -o "$2" "$1"
    elif have wget; then
        wget -q -T 20 -t 3 -O "$2" "$1"
    else
        die "curl or wget is needed to download LAN Chess"
    fi
}

# Copies (local) or downloads (release) the file matching PATTERN into
# $TMP_DIR and prints its path. Runs in a $(...) subshell: messages go to stderr.
# The file is the one SHA256SUMS lists, never a glob guess, and its checksum is
# checked. Only a local directory with no SHA256SUMS entry for the package type
# falls back to its one matching file, with a warning.
fetch_asset() {
    pattern="$1"
    if [ -n "${LANCHESS_LOCAL_DIR:-}" ]; then
        dir=$LANCHESS_LOCAL_DIR
        [ -d "$dir" ] || die "LANCHESS_LOCAL_DIR=$dir is not a directory"
        name=""
        if [ -f "$dir/SHA256SUMS" ]; then
            cp "$dir/SHA256SUMS" "$TMP_DIR/SHA256SUMS"
            name=$(listed_asset "$TMP_DIR/SHA256SUMS" "$pattern") || exit 1
        fi
        if [ -n "$name" ]; then
            # make dist leaves older versions in dist/, and they can sort after
            # this one (1.9.0 after 1.10.0), so a glob would pick the wrong file.
            [ -f "$dir/$name" ] || die "$dir/SHA256SUMS lists $name, but there is no $dir/$name"
            cp "$dir/$name" "$TMP_DIR/$name"
            verify_checksum "$name"
        else
            count=0
            matches=""
            for candidate in "$dir"/$pattern; do
                [ -f "$candidate" ] || continue
                count=$((count + 1))
                name=$(basename "$candidate")
                matches="$matches $name"
            done
            [ "$count" -gt 0 ] || die "no file matching $pattern in $dir"
            [ "$count" -eq 1 ] ||
                die "$dir has $count files matching $pattern:$matches; choose one with LANCHESS_VERSION=X.Y.Z"
            if [ -f "$dir/SHA256SUMS" ]; then
                warn "$dir/SHA256SUMS has no entry for $name; installing it without a checksum check"
            else
                warn "$dir has no SHA256SUMS; installing $name without a checksum check"
            fi
            cp "$dir/$name" "$TMP_DIR/$name"
        fi
    else
        if [ -n "${LANCHESS_VERSION:-}" ]; then
            base="$RELEASES_URL/download/v${LANCHESS_VERSION#v}"
        else
            base="$RELEASES_URL/latest/download"
        fi
        # SHA256SUMS lists every file of the release, so it is both the index
        # (no rate-limited GitHub API calls) and the checksum source.
        download "$base/SHA256SUMS" "$TMP_DIR/SHA256SUMS" ||
            die "could not download $base/SHA256SUMS (no release yet, or no network?); see $RELEASES_URL"
        name=$(listed_asset "$TMP_DIR/SHA256SUMS" "$pattern") || exit 1
        [ -n "$name" ] || die "the release has no file matching $pattern; see $RELEASES_URL"
        say "downloading $base/$name" >&2
        download "$base/$name" "$TMP_DIR/$name" || die "download failed: $base/$name"
        REQUIRE_CHECKSUM=1
        verify_checksum "$name"
    fi
    chmod 0644 "$TMP_DIR/$name"
    echo "$TMP_DIR/$name"
}

# Checks $TMP_DIR/NAME against its SHA256SUMS entry. Without sha256sum or
# shasum, a download is refused and a local file only warned about.
# (Plain sh only: minimal images may lack awk.)
verify_checksum() {
    expected=""
    while read -r sum file || [ -n "${file:-}" ]; do
        if [ "$file" = "$1" ] || [ "$file" = "*$1" ]; then expected="$sum"; fi
    done <"$TMP_DIR/SHA256SUMS"
    [ -n "$expected" ] || die "SHA256SUMS has no entry for $1"
    if have sha256sum; then
        actual=$(sha256sum "$TMP_DIR/$1")
    elif have shasum; then
        actual=$(shasum -a 256 "$TMP_DIR/$1")
    else
        [ "$REQUIRE_CHECKSUM" -eq 0 ] || die "sha256sum or shasum is needed to verify the download"
        warn "no sha256sum or shasum; skipping the checksum check"
        return 0
    fi
    actual=${actual%% *}
    [ "$expected" = "$actual" ] || die "checksum mismatch for $1 (expected $expected, got $actual)"
    say "checksum OK for $1" >&2
}

install_deb() {
    if ! as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades "$1"; then
        say "refreshing the package lists and retrying"
        as_root apt-get update
        as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades "$1"
    fi
    REMOVE_HINT="sudo apt remove lanchess"
}

install_arch() {
    as_root pacman -U --noconfirm --needed "$1" ||
        die "pacman failed; if a dependency could not be downloaded, run 'sudo pacman -Syu' and try again"
    REMOVE_HINT="sudo pacman -R lanchess"
    say "Arch users can also install LAN Chess from the AUR once it is published there: yay -S lanchess"
}

main() {
    method="${LANCHESS_METHOD:-auto}"
    while [ $# -gt 0 ]; do
        case "$1" in
            --method) [ $# -ge 2 ] || die "--method needs a value"; method="$2"; shift ;;
            --method=*) method="${1#--method=}" ;;
            -h | --help) usage; exit 0 ;;
            *) die "unknown option: $1 (try --help)" ;;
        esac
        shift
    done
    if [ "$method" = auto ]; then
        method=$(detect_method)
        [ -n "$method" ] || unsupported
    fi
    pattern=$(asset_pattern "$method")

    TMP_DIR=$(mktemp -d 2>/dev/null || mktemp -d -t lanchess)
    # apt reads the package as the unprivileged _apt user.
    chmod 0755 "$TMP_DIR"
    trap cleanup EXIT
    trap 'exit 130' INT TERM

    say "install method: $method"
    file=$(fetch_asset "$pattern")
    REMOVE_HINT=""
    case "$method" in
        deb) install_deb "$file" ;;
        arch) install_arch "$file" ;;
    esac

    # Check the command the package installed, not whatever comes first on PATH.
    if version=$(/usr/bin/lanchess --version 2>&1); then
        say "installed: $version"
        wanted=${LANCHESS_VERSION:-}
        wanted=${wanted#v}
        case "$version" in
            *"$wanted"*) ;;
            *) warn "expected version $wanted; to downgrade, uninstall LAN Chess first" ;;
        esac
    else
        die "installed, but '/usr/bin/lanchess --version' failed: $version"
    fi
    say "start a game with:  lanchess host    (the other computer: lanchess join)"
    say "the host must allow TCP 5555 and UDP 5556 in its firewall (see 'man lanchess')"
    say "to uninstall:  $REMOVE_HINT"
}

main "$@"
