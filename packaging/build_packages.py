#!/usr/bin/env python3
"""Build the LAN Chess release packages into dist/.

Two packages are released:

  deb    lanchess_V-1_all.deb           Ubuntu 20.04+ and Debian-based systems;
                                        built in pure Python (no dpkg needed)
  arch   lanchess-V-1-any.pkg.tar.zst   Arch Linux and derivatives; makepkg with
                                        packaging/aur/PKGBUILD (as a regular user)

Both are built from a source tarball of this tree with `make install PREFIX=/usr`.
SHA256SUMS is rewritten at the end for this version's release files in dist/.
A package whose tools are missing is skipped with a message (--strict turns
skips into failures).

  python3 packaging/build_packages.py                 # deb and arch (what is possible here)
  python3 packaging/build_packages.py --only deb
  python3 packaging/build_packages.py --list          # tool availability

Test helper (not a release file): `--only tarball` writes lanchess-V.tar.gz,
laid out like GitHub's tag archive (top directory lanchess-V/), for building
the AUR recipe offline.

AUR recipe for a tagged release: the PKGBUILD with the real sha256 of the
GitHub tag archive (downloaded, or given with --tarball or --sha256) and the
maintainer from packaging/channels.conf, plus its .SRCINFO (needs makepkg):

  python3 packaging/build_packages.py --aur out/aur

SOURCE_DATE_EPOCH, if set, is used for every timestamp (reproducible builds).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

try:
    import lzma
except ImportError:  # some minimal Python builds lack lzma; .deb falls back to gzip
    lzma = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
AUR_DIR = PACKAGING / "aur"
CHANNELS_CONF = PACKAGING / "channels.conf"
NAME = "lanchess"
MAINTAINER = "Devesh B <devesh.b@esds.co.in>"
HOMEPAGE = "https://github.com/DeveshB-1/lanchess"
PKG_RELEASE = "1"  # package revision: lanchess_V-1, lanchess-V-1

FORMATS = ("tarball", "deb", "arch")
DEFAULT_FORMATS = ("deb", "arch")
ALIASES = {
    "debian": "deb", "ubuntu": "deb", "pacman": "arch", "pkg": "arch",
    "archlinux": "arch", "source": "tarball", "tar": "tarball",
}

DEB_SYNOPSIS = "terminal chess for two players on the same network"
DEB_DESCRIPTION = """\
LAN Chess is a chess game that runs in a terminal. Two people on two
computers on the same local network play against each other: one hosts the
game and the other joins it, finding the host automatically with a UDP
broadcast. A hot-seat mode lets two players share one keyboard.

It shows a live board with chess clocks and chat, handles draw offers,
taking back moves and rematches, and saves every finished game as a PGN
file. It uses only the Python standard library. Hosting needs TCP port 5555
and UDP port 5556; a firewalld service and a ufw application profile named
"lanchess" are included."""

LINTIAN_OVERRIDES = f"""\
# This package is distributed on GitHub, not uploaded to Debian or Ubuntu, so
# there is no ITP bug for the first changelog entry to close.
{NAME}: initial-upload-closes-no-bugs
# The program is /usr/games/{NAME} (Policy 11.11); /usr/bin/{NAME} is a
# symlink to it so that the command is also on root's PATH and in containers,
# like the Arch package of the same release.
{NAME}: package-section-games-but-has-usr-bin
# /usr/share/{NAME} is the program's private directory (Policy 9.1.1) and
# {NAME}/ in it is the Python package, so the name appears twice.
{NAME}: repeated-path-segment {NAME} [usr/share/{NAME}/{NAME}/]
"""

# Source tarball contents: everything in the project except these.
TOP_LEVEL_HIDDEN_ALLOWED = {".github", ".gitignore", ".gitattributes", ".editorconfig"}
EXCLUDED_TOP_LEVEL = {"dist", "build", "lanchess_games", "venv"}
EXCLUDED_DIR_NAMES = {
    "__pycache__", ".git", ".hg", ".svn", ".tox", ".nox", ".eggs", ".venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
EXCLUDED_REL_PATHS = {"packaging/aur/src", "packaging/aur/pkg"}
EXCLUDED_FILE_PATTERNS = (
    re.compile(r".*\.py[cod]$"),
    re.compile(r".*\.(deb|pyz|whl)$"),
    re.compile(r".*\.pkg\.tar(\.\w+)?$"),
    re.compile(r".*(~|\.swp|\.orig|\.rej)$"),
    re.compile(r"^\.DS_Store$"),
)
# makepkg output next to the PKGBUILD (the tarball, the package, -L logs); see .gitignore
EXCLUDED_REL_FILE_PATTERNS = (re.compile(r"^packaging/aur/[^/]+\.(tar\.\w+|log)$"),)

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class Skip(Exception):
    """The package cannot be built on this machine (a tool is missing)."""


class BuildFailure(Exception):
    """A build step failed."""


# ---------------------------------------------------------------- helpers


def read_version(root: Path = ROOT) -> str:
    """Return ``__version__`` from lanchess/__init__.py without importing the package."""
    text = (root / NAME / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r"""^__version__\s*=\s*["']([^"']+)["']""", text, re.MULTILINE)
    if not match:
        raise BuildFailure(f"cannot find __version__ in {root / NAME / '__init__.py'}")
    version = match.group(1)
    # Plain dotted numbers only: "-" is invalid in a pacman pkgver, and
    # pre-release suffixes would sort after the final release in dpkg.
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)*", version):
        raise BuildFailure(f"unsupported version {version!r}: use plain numbers like 1.2.3")
    return version


def say(message: str) -> None:
    print(message, flush=True)


def run(cmd: Sequence[object], *, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> None:
    """Run a command, echoing it first. Raises BuildFailure on a non-zero exit."""
    args = [str(c) for c in cmd]
    say("  $ " + " ".join(args))
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, env=env)
    if proc.returncode != 0:
        raise BuildFailure(f"{Path(args[0]).name} exited with status {proc.returncode}")


def is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def rfc2822_date(epoch: int) -> str:
    """Debian changelog date, independent of the current locale."""
    t = time.gmtime(epoch)
    return f"{DAYS[t.tm_wday]}, {t.tm_mday:02d} {MONTHS[t.tm_mon - 1]} {t.tm_year} " \
           f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} +0000"


def gzip_bytes(data: bytes, mtime: int = 0) -> bytes:
    """gzip -9n equivalent: maximum compression, no file name, fixed timestamp."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=9, mtime=mtime) as gz:
        gz.write(data)
    return buf.getvalue()


def missing_tools(*tools: str) -> List[str]:
    return [tool for tool in tools if shutil.which(tool) is None]


def extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(str(archive)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(str(dest), filter="data")
        else:  # Python < 3.12 without the security backport; the archive is our own
            tar.extractall(str(dest))


# ---------------------------------------------------------------- source tree


def source_files(root: Path = ROOT) -> Tuple[List[str], List[str]]:
    """Directories and files (POSIX relative paths) that belong in the source tarball."""
    dirs: List[str] = []
    files: List[str] = []
    for current, dirnames, filenames in os.walk(str(root)):
        rel_dir = Path(current).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if d in EXCLUDED_DIR_NAMES or d.endswith(".egg-info") or rel in EXCLUDED_REL_PATHS:
                continue
            if not rel_dir and (d in EXCLUDED_TOP_LEVEL or (d.startswith(".") and d not in TOP_LEVEL_HIDDEN_ALLOWED)):
                continue
            if os.path.islink(os.path.join(current, d)):
                continue
            keep.append(d)
            dirs.append(rel)
        dirnames[:] = keep
        for f in sorted(filenames):
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if not rel_dir and f.startswith(".") and f not in TOP_LEVEL_HIDDEN_ALLOWED:
                continue
            if any(p.match(f) for p in EXCLUDED_FILE_PATTERNS):
                continue
            if any(p.match(rel) for p in EXCLUDED_REL_FILE_PATTERNS):
                continue
            files.append(rel)
    return dirs, files


def normalized_info(info: tarfile.TarInfo, mtime: int) -> tarfile.TarInfo:
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = mtime
    if info.isdir():
        info.mode = 0o755
    elif info.isfile():
        info.mode = 0o755 if info.mode & 0o111 else 0o644
    return info


# ---------------------------------------------------------------- build context


class Builder:
    def __init__(self, outdir: Path, workdir: Path) -> None:
        self.version = read_version()
        self.outdir = outdir
        self.workdir = workdir
        env_epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
        self.reproducible = env_epoch.isdigit()
        self.epoch = int(env_epoch) if self.reproducible else int(time.time())
        self._tarball: Optional[Path] = None
        self._source_tree: Optional[Path] = None

    # Artifact names -------------------------------------------------------
    @property
    def tarball_name(self) -> str:
        return f"{NAME}-{self.version}.tar.gz"

    @property
    def deb_name(self) -> str:
        return f"{NAME}_{self.version}-{PKG_RELEASE}_all.deb"

    @property
    def arch_name(self) -> str:
        return f"{NAME}-{self.version}-{PKG_RELEASE}-any.pkg.tar.zst"

    def release_files(self) -> List[Path]:
        """The files of this version in outdir that belong to a release (and SHA256SUMS).

        The two packages, plus the AUR PKGBUILD and SRCINFO (the .SRCINFO
        under a name without the leading dot, which GitHub would rename) when
        the release workflow has put them there for this version.
        """
        present = [self.outdir / n for n in (self.deb_name, self.arch_name) if (self.outdir / n).is_file()]
        for name, marker in (("PKGBUILD", f"pkgver={self.version}\n"), ("SRCINFO", f"pkgver = {self.version}\n")):
            path = self.outdir / name
            if path.is_file() and marker in path.read_text(encoding="utf-8", errors="replace"):
                present.append(path)
        return present

    def file_mtime(self, path: Path) -> int:
        mtime = int(path.stat().st_mtime)
        return min(mtime, self.epoch) if self.reproducible else mtime

    # tarball -------------------------------------------------------------------
    def write_tarball(self, target: Path) -> Path:
        prefix = f"{NAME}-{self.version}"
        dirs, files = source_files(ROOT)
        for required in ("Makefile", "LICENSE", "README.md", f"{NAME}/__init__.py",
                         "packaging/launcher.py.in", "packaging/lanchess.6"):
            if required not in files:
                raise BuildFailure(f"source tree is missing {required}")
        partial = target.with_name(target.name + ".part")
        with partial.open("wb") as raw, \
                gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=self.epoch) as gz, \
                tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            top = tarfile.TarInfo(prefix)
            top.type = tarfile.DIRTYPE
            tar.addfile(normalized_info(top, self.epoch))
            entries = sorted([(d, True) for d in dirs] + [(f, False) for f in files])
            for rel, is_dir in entries:
                path = ROOT / rel
                info = tar.gettarinfo(str(path), arcname=f"{prefix}/{rel}")
                normalized_info(info, self.file_mtime(path))
                if is_dir or not info.isfile():
                    tar.addfile(info)
                else:
                    with path.open("rb") as fh:
                        tar.addfile(info, fh)
        os.replace(str(partial), str(target))
        return target

    def build_tarball(self) -> Path:
        target = self.write_tarball(self.outdir / self.tarball_name)
        self._tarball = target
        return target

    def tarball(self) -> Path:
        """The source tarball for the packages (built on demand if needed)."""
        if self._tarball is None:
            say("  (building the source tarball first)")
            self._tarball = self.write_tarball(self.workdir / self.tarball_name)
        return self._tarball

    def source_tree(self) -> Path:
        """The source tarball, extracted (what a distro build would see)."""
        if self._source_tree is None:
            dest = self.workdir / "source"
            dest.mkdir()
            extract_tar(self.tarball(), dest)
            self._source_tree = dest / f"{NAME}-{self.version}"
        return self._source_tree

    # deb -------------------------------------------------------------------------
    def build_deb(self) -> Path:
        missing = missing_tools("make", "sed", "gzip", "install")
        if missing:
            raise Skip("missing tools: " + ", ".join(missing))
        src = self.source_tree()
        root = self.workdir / "deb-root"
        # Debian Policy 11.11: the programs of a package in section "games" live in
        # /usr/games. /usr/bin/lanchess stays as a symlink so the command is found on
        # every PATH (root, containers) and the layout matches the Arch package.
        # The licence goes to /usr/share/doc/lanchess/copyright instead.
        run(["make", "-C", src, "install", f"DESTDIR={root}", "PREFIX=/usr", "SYSCONFDIR=/etc",
             "BINDIR=/usr/games", "LAUNCHER_PYTHON=/usr/bin/python3", "INSTALL_LICENSE=no"])
        (root / "usr" / "bin").mkdir(parents=True, exist_ok=True)
        os.symlink(f"../games/{NAME}", str(root / "usr" / "bin" / NAME))
        overrides = root / "usr" / "share" / "lintian" / "overrides"
        overrides.mkdir(parents=True, exist_ok=True)
        (overrides / NAME).write_text(LINTIAN_OVERRIDES, encoding="utf-8")
        docdir = root / "usr" / "share" / "doc" / NAME
        docdir.mkdir(parents=True, exist_ok=True)
        (docdir / "copyright").write_text(debian_copyright((src / "LICENSE").read_text(encoding="utf-8")),
                                          encoding="utf-8")
        changelog = (
            f"{NAME} ({self.version}-{PKG_RELEASE}) unstable; urgency=medium\n\n"
            f"  * Release {self.version}. Packaged with packaging/build_packages.py.\n\n"
            f" -- {MAINTAINER}  {rfc2822_date(self.epoch)}\n"
        )
        (docdir / "changelog.Debian.gz").write_bytes(gzip_bytes(changelog.encode("utf-8")))

        entries = tree_entries(root)
        md5sums: List[str] = []
        conffiles: List[str] = []
        installed_kib = 0
        for rel, path in entries:
            st = path.lstat()
            if path.is_file() and not path.is_symlink():
                installed_kib += (st.st_size + 1023) // 1024
                md5sums.append(f"{hashlib.md5(path.read_bytes()).hexdigest()}  {rel}")
                if rel.startswith("etc/"):
                    conffiles.append("/" + rel)
            else:
                installed_kib += 1
        description = "\n".join(" " + line if line.strip() else " ." for line in DEB_DESCRIPTION.splitlines())
        control = (
            f"Package: {NAME}\n"
            f"Version: {self.version}-{PKG_RELEASE}\n"
            f"Architecture: all\n"
            f"Maintainer: {MAINTAINER}\n"
            f"Installed-Size: {installed_kib}\n"
            f"Depends: python3 (>= 3.8)\n"
            f"Section: games\n"
            f"Priority: optional\n"
            f"Homepage: {HOMEPAGE}\n"
            f"Description: {DEB_SYNOPSIS}\n"
            f"{description}\n"
        )
        control_members: List[Tuple[str, bytes]] = [("control", control.encode("utf-8")),
                                                     ("md5sums", ("\n".join(md5sums) + "\n").encode("utf-8"))]
        if conffiles:
            control_members.append(("conffiles", ("\n".join(conffiles) + "\n").encode("utf-8")))

        ext, compress = ("xz", xz_bytes) if lzma is not None else ("gz", lambda b: gzip_bytes(b, self.epoch))
        control_tar = compress(tar_from_members(control_members, self.epoch))
        data_tar = compress(tar_from_tree(root, entries, self.epoch))
        target = self.outdir / self.deb_name
        write_ar(target, [("debian-binary", b"2.0\n"), (f"control.tar.{ext}", control_tar),
                          (f"data.tar.{ext}", data_tar)], self.epoch)
        say(f"  control.tar.{ext} + data.tar.{ext}: {len(entries)} entries, Installed-Size {installed_kib} KiB")
        return target

    # arch --------------------------------------------------------------------------
    def build_arch(self) -> Path:
        if is_root():
            raise Skip("makepkg refuses to run as root; run this step as a regular user")
        missing = missing_tools(*ARCH_TOOLS)
        if missing:
            raise Skip("missing tools: " + ", ".join(missing))
        work = self.workdir / "arch"
        out = work / "out"
        out.mkdir(parents=True)
        tarball = work / self.tarball_name
        shutil.copy2(str(self.tarball()), str(tarball))
        (work / "PKGBUILD").write_text(self.rendered_pkgbuild(tarball), encoding="utf-8")
        env = dict(os.environ)
        env.update(PKGDEST=str(out), SRCDEST=str(work), SRCPKGDEST=str(out), LOGDEST=str(work),
                   BUILDDIR=str(work / "build"), PKGEXT=".pkg.tar.zst",
                   SOURCE_DATE_EPOCH=str(self.epoch))
        env.setdefault("PACKAGER", MAINTAINER)
        # --nocheck: the unit tests ran before packaging; the AUR build runs check().
        run(["makepkg", "--force", "--nodeps", "--nocheck", "--noconfirm", "--noprogressbar"],
            cwd=work, env=env)
        built = out / self.arch_name
        if not built.is_file():
            raise BuildFailure(f"makepkg did not produce {built.name}")
        target = self.outdir / built.name
        shutil.copy2(str(built), str(target))
        return target

    def rendered_pkgbuild(self, tarball: Path) -> str:
        """packaging/aur/PKGBUILD pointed at the local tarball, with its real checksum."""
        text = (AUR_DIR / "PKGBUILD").read_text(encoding="utf-8")
        declared = re.search(r"^pkgver=(\S+)", text, re.MULTILINE)
        if declared is None:
            raise BuildFailure("packaging/aur/PKGBUILD has no pkgver= line")
        if declared.group(1) != self.version:
            say(f"  warning: PKGBUILD says pkgver={declared.group(1)}, using {self.version}; update it")
        text = re.sub(r"^pkgver=\S+", f"pkgver={self.version}", text, count=1, flags=re.MULTILINE)
        text = re.sub(r"^pkgrel=\S+", f"pkgrel={PKG_RELEASE}", text, count=1, flags=re.MULTILINE)
        text, n_src = re.subn(r"^source=\(.*?\)", f'source=("{tarball.name}")', text, count=1,
                              flags=re.MULTILINE | re.DOTALL)
        text, n_sum = re.subn(r"^sha256sums=\(.*?\)", f"sha256sums=('{sha256_file(tarball)}')", text,
                              count=1, flags=re.MULTILINE | re.DOTALL)
        if not (n_src and n_sum):
            raise BuildFailure("could not rewrite source=/sha256sums= in the PKGBUILD")
        return text

    # checksums ------------------------------------------------------------------------
    def write_sums(self) -> Optional[Path]:
        present = self.release_files()
        if not present:
            return None
        target = self.outdir / "SHA256SUMS"
        target.write_text("".join(f"{sha256_file(p)}  {p.name}\n" for p in present), encoding="utf-8")
        return target


ARCH_TOOLS = ("makepkg", "fakeroot", "bsdtar", "zstd", "make")


# ---------------------------------------------------------------- .deb plumbing


def debian_copyright(license_text: str) -> str:
    """debian/copyright in the machine-readable format, from the MIT LICENSE file."""
    lines = license_text.strip().splitlines()
    holder = "2026 Devesh B"
    body_start = 0
    for i, line in enumerate(lines):
        match = re.match(r"\s*Copyright\s*(?:\(c\)|©)?\s*(.+)", line, re.IGNORECASE)
        if match:
            holder = match.group(1).strip()
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip()
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", body)]
    wrapped: List[str] = []
    for para in paragraphs:
        if wrapped:
            wrapped.append(" .")
        wrapped.extend(" " + line for line in wrap(para, 78))
    return (
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        f"Upstream-Name: {NAME}\n"
        f"Upstream-Contact: {MAINTAINER}\n"
        f"Source: {HOMEPAGE}\n"
        "\n"
        "Files: *\n"
        f"Copyright: {holder}\n"
        "License: Expat\n"
        "\n"
        "License: Expat\n" + "\n".join(wrapped) + "\n"
    )


def wrap(text: str, width: int) -> List[str]:
    out: List[str] = []
    line = ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        out.append(line)
    return out


def tree_entries(root: Path) -> List[Tuple[str, Path]]:
    """Every path below ``root`` (POSIX relative, parents before children, sorted)."""
    entries: List[Tuple[str, Path]] = []
    for current, dirnames, filenames in os.walk(str(root)):
        dirnames.sort()
        base = Path(current)
        for name in sorted(dirnames + filenames):
            path = base / name
            entries.append((path.relative_to(root).as_posix(), path))
    entries.sort(key=lambda e: e[0].split("/"))
    return entries


def tar_from_tree(root: Path, entries: List[Tuple[str, Path]], mtime: int) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        top = tarfile.TarInfo("./")
        top.type = tarfile.DIRTYPE
        tar.addfile(normalized_info(top, mtime))
        for rel, path in entries:
            info = tar.gettarinfo(str(path), arcname="./" + rel)
            normalized_info(info, mtime)
            if info.isfile():
                with path.open("rb") as fh:
                    tar.addfile(info, fh)
            else:
                tar.addfile(info)
    return buf.getvalue()


def tar_from_members(members: List[Tuple[str, bytes]], mtime: int) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        top = tarfile.TarInfo("./")
        top.type = tarfile.DIRTYPE
        tar.addfile(normalized_info(top, mtime))
        for name, data in members:
            info = tarfile.TarInfo("./" + name)
            info.size = len(data)
            info.mode = 0o644
            normalized_info(info, mtime)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def xz_bytes(data: bytes) -> bytes:
    assert lzma is not None
    return lzma.compress(data, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, preset=9)


def write_ar(target: Path, members: List[Tuple[str, bytes]], mtime: int) -> None:
    """Write a System V/GNU ``ar`` archive, the container format of a .deb."""
    partial = target.with_name(target.name + ".part")
    with partial.open("wb") as fh:
        fh.write(b"!<arch>\n")
        for name, data in members:
            header = f"{name:<16}{mtime:<12}{0:<6}{0:<6}{0o100644:<8o}{len(data):<10}`\n"
            assert len(header) == 60, header
            fh.write(header.encode("ascii"))
            fh.write(data)
            if len(data) % 2:
                fh.write(b"\n")
    os.replace(str(partial), str(target))


# ---------------------------------------------------------------- command line


def parse_formats(text: str) -> List[str]:
    chosen: List[str] = []
    for item in text.split(","):
        item = item.strip().lower()
        if not item:
            continue
        item = ALIASES.get(item, item)
        if item not in FORMATS:
            raise argparse.ArgumentTypeError(
                f"unknown format {item!r} (choose from {', '.join(FORMATS)})")
        if item not in chosen:
            chosen.append(item)
    return chosen


def tool_report() -> List[Tuple[str, str]]:
    def status(tools: Sequence[str], extra: str = "") -> str:
        missing = missing_tools(*tools)
        return ("missing: " + ", ".join(missing)) if missing else ("ok" + extra)

    arch = "unavailable: running as root" if is_root() else status(ARCH_TOOLS)
    return [
        ("deb", status(("make", "sed", "gzip", "install"), " (pure Python, xz)" if lzma else " (pure Python, gzip)")),
        ("arch", arch),
        ("tarball", "ok (test helper, not a release file)"),
    ]


# ---------------------------------------------------------------- AUR recipe


def read_channels(path: Path = CHANNELS_CONF) -> Dict[str, str]:
    """The KEY="value" settings of packaging/channels.conf."""
    values: Dict[str, str] = {}
    if not path.is_file():
        raise BuildFailure(f"{path} is missing")
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"""([A-Za-z_][A-Za-z0-9_]*)=(?:"([^"$`\\]*)"|'([^']*)'|([^\s"'#$`\\]*))""", line)
        if match is None:
            raise BuildFailure(f"{path.name}:{number}: expected KEY=\"value\", got {raw!r}")
        key, double, single, bare = match.groups()
        values[key] = next(v for v in (double, single, bare) if v is not None)
    return values


def tag_archive_url(repo: str, version: str) -> str:
    """GitHub's archive of tag vVERSION; its top directory is NAME-VERSION/."""
    return f"https://github.com/{repo}/archive/refs/tags/v{version}.tar.gz"


def check_source_tarball(path: Path, version: str) -> None:
    """Fail unless ``path`` is a source tarball of this version (top directory NAME-VERSION/)."""
    prefix = f"{NAME}-{version}"
    try:
        with tarfile.open(str(path)) as tar:
            names = tar.getnames()
    except (OSError, tarfile.TarError) as exc:
        raise BuildFailure(f"{path} is not a readable tarball") from exc
    # git archive (GitHub) adds a pax_global_header entry with the commit id.
    stray = [n for n in names if n.rstrip("/") != prefix and not n.startswith(prefix + "/")
             and n != "pax_global_header"]
    if stray or f"{prefix}/Makefile" not in names:
        raise BuildFailure(f"{path.name} does not look like the {version} source "
                           f"(expected everything under {prefix}/, with a Makefile)")


def download(url: str, target: Path, timeout: int = 60) -> None:
    import urllib.request  # only needed here
    say(f"  downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": f"{NAME}-build_packages"})
    with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as fh:  # noqa: S310
        shutil.copyfileobj(response, fh)


def release_checksum(args: argparse.Namespace, version: str, url: str, workdir: Path) -> str:
    """sha256 of the release source: --sha256, else --tarball, else a download of ``url``."""
    if args.sha256:
        if not re.fullmatch(r"[0-9a-f]{64}", args.sha256):
            raise BuildFailure("--sha256 needs 64 lowercase hex digits")
        return args.sha256
    tarball = args.tarball
    if tarball is None:
        tarball = workdir / f"{NAME}-{version}.tar.gz"
        try:
            download(url, tarball)
        except OSError as exc:
            raise BuildFailure(f"cannot download {url}: {exc} (is the tag pushed? or pass --tarball/--sha256)") from exc
    check_source_tarball(tarball, version)
    return sha256_file(tarball)


def write_aur(outdir: Path, version: str, sha256: str, channels: Dict[str, str]) -> List[Path]:
    """Ready-to-push AUR files: PKGBUILD with the real checksum, and its .SRCINFO."""
    text = (AUR_DIR / "PKGBUILD").read_text(encoding="utf-8")
    repo_url = f"https://github.com/{channels.get('GITHUB_REPO', 'DeveshB-1/lanchess')}"
    if f"url='{repo_url}'" not in text:
        raise BuildFailure(f"packaging/aur/PKGBUILD must have url='{repo_url}' (GITHUB_REPO in channels.conf)")
    maintainer = channels.get("AUR_MAINTAINER", MAINTAINER)
    text, n_maint = re.subn(r"^# Maintainer:.*$", f"# Maintainer: {maintainer}", text, count=1, flags=re.MULTILINE)
    text = re.sub(r"^pkgver=\S+", f"pkgver={version}", text, count=1, flags=re.MULTILINE)
    text = re.sub(r"^pkgrel=\S+", f"pkgrel={PKG_RELEASE}", text, count=1, flags=re.MULTILINE)
    text, n_sum = re.subn(r"^sha256sums=\(.*?\)", f"sha256sums=('{sha256}')", text, count=1,
                          flags=re.MULTILINE | re.DOTALL)
    if not (n_maint and n_sum):
        raise BuildFailure("packaging/aur/PKGBUILD needs a '# Maintainer:' line and sha256sums=()")
    outdir.mkdir(parents=True, exist_ok=True)
    pkgbuild = outdir / "PKGBUILD"
    pkgbuild.write_text(text, encoding="utf-8")
    if is_root() or shutil.which("makepkg") is None:
        raise BuildFailure(f"wrote {pkgbuild}, but .SRCINFO needs makepkg run as a regular user "
                           f"(on Arch: cd {outdir} && makepkg --printsrcinfo > .SRCINFO)")
    proc = subprocess.run(["makepkg", "--printsrcinfo"], cwd=str(outdir), stdout=subprocess.PIPE,
                          universal_newlines=True)
    if proc.returncode != 0 or "pkgbase = " not in proc.stdout:
        raise BuildFailure(f"makepkg --printsrcinfo exited with status {proc.returncode}")
    srcinfo = outdir / ".SRCINFO"
    srcinfo.write_text(proc.stdout, encoding="utf-8")
    unset = [key for key in ("AUR_USER", "AUR_MAINTAINER") if "CHANGE_ME" in channels.get(key, "CHANGE_ME")]
    if unset:
        say(f"  note: set {' and '.join(unset)} in packaging/channels.conf before pushing to the AUR")
    return [pkgbuild, srcinfo]


def write_recipe(args: argparse.Namespace) -> int:
    """--aur DIR: the AUR recipe for the tagged release."""
    try:
        version = read_version()
        channels = read_channels()
        archive = tag_archive_url(channels.get("GITHUB_REPO", "DeveshB-1/lanchess"), version)
        with tempfile.TemporaryDirectory(prefix="lanchess-aur-") as tmp:
            sha256 = release_checksum(args, version, archive, Path(tmp))
        say(f"LAN Chess {version}: source {archive}\n  sha256 {sha256}")
        written = write_aur(args.aur.resolve(), version, sha256, channels)
    except BuildFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for path in written:
        say(f"  wrote {path}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the LAN Chess release packages (.deb and Arch) into dist/.",
        epilog="Formats: deb, arch (default), tarball (test helper). SHA256SUMS is always refreshed.",
    )
    parser.add_argument("--only", type=parse_formats, metavar="LIST",
                        help="comma-separated formats to build (default: deb,arch)")
    parser.add_argument("--skip", type=parse_formats, metavar="LIST", default=[],
                        help="comma-separated formats to leave out")
    parser.add_argument("-o", "--outdir", type=Path, default=ROOT / "dist",
                        help="output directory (default: dist/)")
    parser.add_argument("--strict", action="store_true",
                        help="treat skipped formats (missing tools) as failures")
    parser.add_argument("--keep-work", action="store_true", help="keep the temporary build directory")
    parser.add_argument("--list", action="store_true", help="show which formats can be built here and exit")
    parser.add_argument("--sums-only", action="store_true",
                        help="only rewrite SHA256SUMS for the release files already in the output directory")
    aur = parser.add_argument_group("AUR recipe", "write the AUR files for the tagged release instead of building")
    aur.add_argument("--aur", type=Path, metavar="DIR",
                     help="write a ready-to-push PKGBUILD and .SRCINFO to DIR (needs makepkg, not as root)")
    aur.add_argument("--tarball", type=Path, metavar="FILE",
                     help="the release source (default: download the GitHub tag archive)")
    aur.add_argument("--sha256", metavar="HEX", help="checksum of the release source, instead of --tarball")
    args = parser.parse_args(argv)

    if args.list:
        for fmt, state in tool_report():
            print(f"{fmt:<8} {state}")
        return 0
    if args.aur:
        return write_recipe(args)
    if args.tarball or args.sha256:
        parser.error("--tarball and --sha256 go with --aur")

    if args.sums_only:
        builder = Builder(args.outdir.resolve(), Path(tempfile.gettempdir()))
        sums = builder.write_sums()
        if sums is None:
            print(f"no release files for version {builder.version} in {builder.outdir}", file=sys.stderr)
            return 1
        print(sums.read_text(encoding="utf-8"), end="")
        return 0

    selected = [f for f in (args.only or list(DEFAULT_FORMATS)) if f not in args.skip]
    selected.sort(key=FORMATS.index)
    outdir = args.outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="lanchess-pkg-"))
    try:
        builder = Builder(outdir, workdir)
    except BuildFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        shutil.rmtree(str(workdir), ignore_errors=True)
        return 1
    say(f"LAN Chess {builder.version}: building {', '.join(selected) or 'nothing'} into {outdir}")

    steps: Dict[str, Callable[[], Path]] = {
        "tarball": builder.build_tarball, "deb": builder.build_deb, "arch": builder.build_arch,
    }
    results: List[Tuple[str, str, str]] = []
    try:
        for fmt in selected:
            say(f"\n==> {fmt}")
            try:
                path = steps[fmt]()
            except Skip as exc:
                say(f"  SKIPPED: {exc}")
                results.append((fmt, "skipped", str(exc)))
            except (BuildFailure, OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
                say(f"  FAILED: {exc}")
                results.append((fmt, "FAILED", str(exc)))
            else:
                size = path.stat().st_size
                say(f"  built {path.name} ({size / 1024:.1f} KiB)")
                results.append((fmt, "built", path.name))
        sums = builder.write_sums()
    finally:
        if args.keep_work:
            say(f"\nwork directory kept: {workdir}")
        else:
            shutil.rmtree(str(workdir), ignore_errors=True)

    say("\nSummary")
    for fmt, state, detail in results:
        say(f"  {fmt:<8} {state:<8} {detail}")
    if sums is not None:
        say(f"  {'sums':<8} {'written':<8} {sums.name}")
    failed = [r for r in results if r[1] == "FAILED" or (args.strict and r[1] == "skipped")]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
