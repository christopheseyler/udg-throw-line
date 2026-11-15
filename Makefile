# udg-throw-line - install and developer shortcuts.
#
# The Yocto recipe (in udg-yocto) runs:
#     oe_runmake install DESTDIR=${D} PREFIX=${prefix} ... SYSTEMD_UNITDIR=${systemd_system_unitdir}
# Nothing is compiled: the package is plain Python 3.12, standard library only.

DESTDIR ?=
PREFIX ?= /usr
BINDIR ?= $(PREFIX)/bin
LIBDIR ?= $(PREFIX)/lib/udg-throw-line
SYSCONFDIR ?= /etc
# systemd's system unit directory. Yocto passes ${systemd_system_unitdir}
# (/lib/systemd/system without usrmerge).
SYSTEMD_UNITDIR ?= $(PREFIX)/lib/systemd/system
# Interpreter written in the launchers' shebang (target path, not the build host's).
PYTHON ?= /usr/bin/python3

INSTALL ?= install
PACKAGE_FILES := $(wildcard udg_throw_line/*.py)
# Local runs keep their data out of /data and /run.
LOCAL := --config conf/udg-throw-line.conf --data-dir _install/data --run-dir _install/run

.PHONY: all install uninstall test simulate clean

all:
	@echo "Nothing to build. Targets: install, test, simulate, clean."

install:
	$(INSTALL) -d $(DESTDIR)$(LIBDIR)/udg_throw_line
	$(INSTALL) -m 0644 $(PACKAGE_FILES) $(DESTDIR)$(LIBDIR)/udg_throw_line/
	$(INSTALL) -m 0644 README.md $(DESTDIR)$(LIBDIR)/README.md
	$(INSTALL) -d $(DESTDIR)$(BINDIR)
# Launchers: a tiny Python script per command, with the package directory
# baked in (no PYTHONPATH, no shell wrapper).
	printf '#!%s\nimport sys\nsys.path.insert(0, "%s")\nfrom udg_throw_line.service import main\nsys.exit(main())\n' \
		'$(PYTHON)' '$(LIBDIR)' > $(DESTDIR)$(BINDIR)/udg-throw-line-service
	printf '#!%s\nimport sys\nsys.path.insert(0, "%s")\nfrom udg_throw_line.cli import main\nsys.exit(main())\n' \
		'$(PYTHON)' '$(LIBDIR)' > $(DESTDIR)$(BINDIR)/udg-throw-line
	chmod 0755 $(DESTDIR)$(BINDIR)/udg-throw-line-service $(DESTDIR)$(BINDIR)/udg-throw-line
	$(INSTALL) -d $(DESTDIR)$(SYSTEMD_UNITDIR)
	sed 's|/usr/bin/udg-throw-line-service|$(BINDIR)/udg-throw-line-service|g' systemd/udg-throw-line.service \
		> $(DESTDIR)$(SYSTEMD_UNITDIR)/udg-throw-line.service
	chmod 0644 $(DESTDIR)$(SYSTEMD_UNITDIR)/udg-throw-line.service
	$(INSTALL) -d $(DESTDIR)$(SYSCONFDIR)
	$(INSTALL) -m 0644 conf/udg-throw-line.conf $(DESTDIR)$(SYSCONFDIR)/udg-throw-line.conf

uninstall:
	rm -rf $(DESTDIR)$(LIBDIR)
	rm -f $(DESTDIR)$(BINDIR)/udg-throw-line-service $(DESTDIR)$(BINDIR)/udg-throw-line
	rm -f $(DESTDIR)$(SYSTEMD_UNITDIR)/udg-throw-line.service
	rm -f $(DESTDIR)$(SYSCONFDIR)/udg-throw-line.conf

test:
	python3 -m unittest

# Run on a PC without hardware: synthetic camera, `sim foot` to cross the line.
simulate:
	python3 -m udg_throw_line --simulate --verbose $(LOCAL)

clean:
	rm -rf _install
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
