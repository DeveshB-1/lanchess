# Packaging LAN Chess

Maintainer notes. LAN Chess ships two packages:

| Target | File | Built by |
|---|---|---|
| Ubuntu 20.04+, Debian and derivatives | `lanchess_V-1_all.deb` | `build_packages.py`, in pure Python (no dpkg needed) |
| Arch Linux and derivatives | `lanchess-V-1-any.pkg.tar.zst` | `makepkg` with `packaging/aur/PKGBUILD`, as a regular user |

Each GitHub release also carries the AUR recipe for that release (`PKGBUILD` and
`SRCINFO`) and `SHA256SUMS`. Other systems run LAN Chess from source
(`python3 play.py`) or with `pipx install git+https://github.com/DeveshB-1/lanchess`;
`make pyz` builds a single-file zipapp for development, which is not released.

## Files

| Path | Purpose |
|---|---|
| `lanchess/__init__.py` | `__version__`, the only place the version is set |
| `Makefile` | the installed layout; both packages run `make install PREFIX=/usr` |
| `packaging/build_packages.py` | builds the packages into `dist/`, writes `SHA256SUMS` and the AUR recipe |
| `packaging/aur/PKGBUILD` | the Arch recipe (source: the GitHub tag archive, `sha256sums=('SKIP')` until release) |
| `packaging/channels.conf` | GitHub repository and AUR maintainer (`AUR_USER` is a placeholder to fill in) |
| `packaging/test_in_containers.sh` | installs and tests the packages in containers, runs lintian and namcap |
| `packaging/lanchess.6`, `launcher.py.in`, `lanchess.desktop`, `lanchess.svg` | man page, `/usr/bin/lanchess`, desktop entry, icon |
| `packaging/ufw/lanchess`, `packaging/firewalld/lanchess.xml` | firewall profiles for TCP 5555 and UDP 5556 (both packages ship both) |
| `install.sh` | `curl … \| sh` installer: picks the .deb or the Arch package from the latest release |
| `.github/workflows/` | `ci.yml` (tests on Linux, macOS, Windows), `packages.yml` (build and container tests), `release.yml` |

`make install PREFIX=/usr` puts the program in `/usr/bin/lanchess`, the package in
`/usr/share/lanchess/lanchess/` (private directory, no `.pyc`), the man page in
`/usr/share/man/man6/lanchess.6.gz`, plus the desktop entry, icon, firewalld service
(`/usr/lib/firewalld/services`), ufw profile (`/etc/ufw/applications.d`), README and
licence. `make uninstall` with the same `PREFIX`/`DESTDIR` removes them.

The .deb is in `Section: games`, so its program is `/usr/games/lanchess` (Policy 11.11)
with `/usr/bin/lanchess` as a symlink, and the licence is in
`/usr/share/doc/lanchess/copyright`. Its lintian overrides (`LINTIAN_OVERRIDES` in
`build_packages.py`, each with a comment) cover `initial-upload-closes-no-bugs` (the
package is not uploaded to Debian, so there is no ITP bug to close),
`package-section-games-but-has-usr-bin` (the `/usr/bin/lanchess` symlink) and
`repeated-path-segment` (`/usr/share/lanchess/lanchess/`, the Python package in the
program's private directory). namcap reports `msvcrt` as a missing dependency;
that is the Windows-only module `term.py` imports on Windows, and the tests allow exactly
that message.

## Build and test locally

```sh
make test                                 # unit tests
make dist                                 # dist/: .deb, .pkg.tar.zst (on Arch, not as root), SHA256SUMS
python3 packaging/build_packages.py --list
packaging/test_in_containers.sh           # every case, 4 at a time (podman or docker)
packaging/test_in_containers.sh --only deb-ubuntu-24.04,lint-deb --jobs 2 --timeout 600
```

A package whose tools are missing is skipped (`--strict` makes that an error); the Arch
package needs `makepkg`, so build it on Arch or in an `archlinux` container as a regular
user. `SOURCE_DATE_EPOCH` makes the timestamps reproducible (CI uses the commit time).

The container cases: the .deb on Ubuntu 20.04 (Python 3.8), 22.04, 24.04 and 26.04; the
Arch package with `pacman -U`; `build_packages.py --only arch` as a regular user; the AUR
recipe built with `makepkg` (its `check()` runs the unit tests); `install.sh` on Ubuntu
24.04 and Arch (from a local directory, where it must install the file `SHA256SUMS`
lists rather than another one that sorts after it, and without `SHA256SUMS` refuse to
choose between two packages unless `LANCHESS_VERSION` names one; and from a local
mirror that must reject a tampered file); `install.sh` on Alpine, which must print the
run-from-source command;
lintian (Ubuntu 24.04) and namcap, which must report no errors or warnings. Each install
case checks the file layout and man page, runs `lanchess --version` and a scripted
fool's mate that must print `Checkmate`, uninstalls, and checks that `/usr/bin/lanchess`
and `/usr/share/lanchess` are gone. Logs are in `build/container-tests/`.

## Cut a release

1. Set `__version__` in `lanchess/__init__.py` and `pkgver=` in `packaging/aur/PKGBUILD`
   (`build_packages.py` warns if they differ).
2. `make test && make dist && packaging/test_in_containers.sh`
3. Commit, tag and push:

   ```sh
   git tag -a v1.2.3 -m "LAN Chess 1.2.3"
   git push origin main v1.2.3
   ```

4. `.github/workflows/release.yml` checks that the tag is `v` + `__version__`, runs the
   tests, builds both packages and runs the container tests, writes the AUR `PKGBUILD`
   with the sha256 of the GitHub tag archive and its `.SRCINFO` (then builds that recipe
   with `makepkg`), and creates the GitHub release with the .deb, the Arch package,
   `PKGBUILD`, `SRCINFO` and `SHA256SUMS`, plus install instructions. GitHub renames
   release files that start with a dot, so `.SRCINFO` is attached as `SRCINFO`.

## Publish to the AUR

Once per machine: create an account on aur.archlinux.org, add your SSH public key to it,
and put your account name in `AUR_USER` in `packaging/channels.conf`. Then, for each
release:

```sh
git clone ssh://aur@aur.archlinux.org/lanchess.git aur-lanchess
cd aur-lanchess
curl -fLO https://github.com/DeveshB-1/lanchess/releases/download/v1.2.3/PKGBUILD
curl -fL -o .SRCINFO https://github.com/DeveshB-1/lanchess/releases/download/v1.2.3/SRCINFO
makepkg -si                     # optional: build and install it locally first
git add PKGBUILD .SRCINFO
git commit -m "Update to 1.2.3"
git push
```

Without the release files, `python3 packaging/build_packages.py --aur DIR` (on Arch, as a
regular user, after the tag is pushed) downloads the tag archive and writes the same
`PKGBUILD` and `.SRCINFO` to `DIR`.
