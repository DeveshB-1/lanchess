# LAN Chess: installed file layout, shared by every distribution package.
#
#   make install [DESTDIR=/staging] [PREFIX=/usr/local]
#   make uninstall [DESTDIR=...] [PREFIX=...]
#   make test      unit tests
#   make dist      the release packages (.deb and Arch) in dist/
#   make pyz       dist/lanchess.pyz, a single-file zipapp (developer tool)
#   make clean
#
# The .deb (Ubuntu) and the Arch package both run `make install PREFIX=/usr`,
# so this file is the single source of truth for where everything goes.
# (GNU make functions split on spaces: keep the source tree in a path without spaces.)

PACKAGE := lanchess
# Directory holding this Makefile, so `make -f /path/to/Makefile` also works.
SRCDIR  := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' "$(SRCDIR)/lanchess/__init__.py")

PREFIX       ?= /usr/local
DESTDIR      ?=
# System prefixes use /etc; a private prefix (e.g. PREFIX=$HOME/.local) keeps
# its configuration under the prefix, so a non-root install never touches /etc.
ifneq ($(filter /usr /usr/local,$(PREFIX)),)
SYSCONFDIR   ?= /etc
else
SYSCONFDIR   ?= $(PREFIX)/etc
endif
BINDIR       ?= $(PREFIX)/bin
DATADIR      ?= $(PREFIX)/share
PKGDATADIR   ?= $(DATADIR)/$(PACKAGE)
MANDIR       ?= $(DATADIR)/man
APPDIR       ?= $(DATADIR)/applications
ICONDIR      ?= $(DATADIR)/icons/hicolor/scalable/apps
DOCDIR       ?= $(DATADIR)/doc/$(PACKAGE)
LICENSEDIR   ?= $(DATADIR)/licenses/$(PACKAGE)
# firewalld reads services from /usr/lib/firewalld/services (packages) and
# /etc/firewalld/services (local admin), never from /usr/local/lib.
ifeq ($(PREFIX),/usr)
FIREWALLDDIR ?= $(PREFIX)/lib/firewalld/services
else
FIREWALLDDIR ?= $(SYSCONFDIR)/firewalld/services
endif
UFWDIR       ?= $(SYSCONFDIR)/ufw/applications.d

# Set to "no" to skip a part of the layout (the .deb keeps the licence in
# /usr/share/doc/lanchess/copyright instead of /usr/share/licenses).
INSTALL_LICENSE ?= yes
INSTALL_UFW     ?= yes

# Python used for `make test` / `make pyz` / `make dist`.
PYTHON ?= python3

# Interpreter written into the launcher's #! line. System prefixes get the
# system interpreter (what distro policies expect); other prefixes use it too
# when it exists, and fall back to `/usr/bin/env python3` (macOS, BSDs).
ifeq ($(PREFIX),/usr)
LAUNCHER_PYTHON ?= /usr/bin/python3
else
LAUNCHER_PYTHON ?= $(if $(wildcard /usr/bin/python3),/usr/bin/python3,/usr/bin/env python3)
endif

INSTALL      ?= install
INSTALL_DATA ?= $(INSTALL) -m 0644
INSTALL_DIR  ?= $(INSTALL) -d -m 0755
# Not called GZIP: gzip itself reads options from a GZIP environment variable.
MANGZIP      ?= gzip -9n

MODULES := $(sort $(wildcard $(SRCDIR)/lanchess/*.py))

.PHONY: all install uninstall test pyz dist clean check-version

all: pyz

check-version:
	@test -n "$(VERSION)" || { echo "cannot read __version__ from lanchess/__init__.py" >&2; exit 1; }
	@test -f "$(SRCDIR)/lanchess/cli.py" || echo "warning: lanchess/cli.py is missing; the installed command will not start" >&2

install: check-version
	$(INSTALL_DIR) "$(DESTDIR)$(PKGDATADIR)/$(PACKAGE)"
	$(INSTALL_DATA) $(MODULES) "$(DESTDIR)$(PKGDATADIR)/$(PACKAGE)/"
	$(INSTALL_DIR) "$(DESTDIR)$(BINDIR)"
	sed -e 's|@PYTHON@|$(LAUNCHER_PYTHON)|' -e 's|@PKGDATADIR@|$(PKGDATADIR)|' \
	    "$(SRCDIR)/packaging/launcher.py.in" > "$(DESTDIR)$(BINDIR)/$(PACKAGE).tmp"
	chmod 0755 "$(DESTDIR)$(BINDIR)/$(PACKAGE).tmp"
	mv -f "$(DESTDIR)$(BINDIR)/$(PACKAGE).tmp" "$(DESTDIR)$(BINDIR)/$(PACKAGE)"
	$(INSTALL_DIR) "$(DESTDIR)$(MANDIR)/man6"
	sed -e '/^\.TH /s/"lanchess [^"]*"/"lanchess $(VERSION)"/' "$(SRCDIR)/packaging/lanchess.6" \
	    > "$(DESTDIR)$(MANDIR)/man6/$(PACKAGE).6.tmp"
	$(MANGZIP) < "$(DESTDIR)$(MANDIR)/man6/$(PACKAGE).6.tmp" > "$(DESTDIR)$(MANDIR)/man6/$(PACKAGE).6.gz"
	rm -f "$(DESTDIR)$(MANDIR)/man6/$(PACKAGE).6.tmp"
	chmod 0644 "$(DESTDIR)$(MANDIR)/man6/$(PACKAGE).6.gz"
	$(INSTALL_DIR) "$(DESTDIR)$(APPDIR)"
	$(INSTALL_DATA) "$(SRCDIR)/packaging/lanchess.desktop" "$(DESTDIR)$(APPDIR)/$(PACKAGE).desktop"
	$(INSTALL_DIR) "$(DESTDIR)$(ICONDIR)"
	$(INSTALL_DATA) "$(SRCDIR)/packaging/lanchess.svg" "$(DESTDIR)$(ICONDIR)/$(PACKAGE).svg"
	$(INSTALL_DIR) "$(DESTDIR)$(FIREWALLDDIR)"
	$(INSTALL_DATA) "$(SRCDIR)/packaging/firewalld/lanchess.xml" "$(DESTDIR)$(FIREWALLDDIR)/$(PACKAGE).xml"
ifeq ($(INSTALL_UFW),yes)
	$(INSTALL_DIR) "$(DESTDIR)$(UFWDIR)"
	$(INSTALL_DATA) "$(SRCDIR)/packaging/ufw/lanchess" "$(DESTDIR)$(UFWDIR)/$(PACKAGE)"
endif
	$(INSTALL_DIR) "$(DESTDIR)$(DOCDIR)"
	$(INSTALL_DATA) "$(SRCDIR)/README.md" "$(DESTDIR)$(DOCDIR)/README.md"
ifeq ($(INSTALL_LICENSE),yes)
	$(INSTALL_DIR) "$(DESTDIR)$(LICENSEDIR)"
	$(INSTALL_DATA) "$(SRCDIR)/LICENSE" "$(DESTDIR)$(LICENSEDIR)/LICENSE"
endif

uninstall:
	rm -f "$(DESTDIR)$(BINDIR)/$(PACKAGE)"
	rm -f "$(DESTDIR)$(PKGDATADIR)/$(PACKAGE)/"*.py
	rm -rf "$(DESTDIR)$(PKGDATADIR)/$(PACKAGE)/__pycache__"
	rmdir "$(DESTDIR)$(PKGDATADIR)/$(PACKAGE)" "$(DESTDIR)$(PKGDATADIR)" 2>/dev/null || :
	rm -f "$(DESTDIR)$(MANDIR)/man6/$(PACKAGE).6.gz"
	rm -f "$(DESTDIR)$(APPDIR)/$(PACKAGE).desktop"
	rm -f "$(DESTDIR)$(ICONDIR)/$(PACKAGE).svg"
	rm -f "$(DESTDIR)$(FIREWALLDDIR)/$(PACKAGE).xml"
	rm -f "$(DESTDIR)$(UFWDIR)/$(PACKAGE)"
	rm -f "$(DESTDIR)$(DOCDIR)/README.md" "$(DESTDIR)$(LICENSEDIR)/LICENSE"
	rmdir "$(DESTDIR)$(DOCDIR)" "$(DESTDIR)$(LICENSEDIR)" 2>/dev/null || :

test:
	cd "$(SRCDIR)" && $(PYTHON) -m unittest discover -s tests -v

pyz:
	$(PYTHON) "$(SRCDIR)/build.py"

dist:
	$(PYTHON) "$(SRCDIR)/packaging/build_packages.py"

clean:
	cd "$(SRCDIR)" && rm -rf dist build packaging/aur/src packaging/aur/pkg packaging/aur/*.tar.* packaging/aur/*.log *.egg-info
	find "$(SRCDIR)" -name __pycache__ -type d -prune -exec rm -rf {} +
