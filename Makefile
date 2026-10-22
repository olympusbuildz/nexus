PYTHON ?= python3
VENV ?= $(CURDIR)/.venv
VENV_PYTHON ?= $(VENV)/bin/python
UV ?= uv

.PHONY: dev-setup dev-install test check lint identity menubar-install

# A uv-made venv has no pip module, so use uv when it is on PATH.
dev-install:
	@if command -v $(UV) >/dev/null 2>&1; then \
		$(UV) pip install --python $(PYTHON) -r requirements-dev.txt; \
	else \
		$(PYTHON) -m pip install -r requirements-dev.txt; \
	fi

dev-setup:
	@test -x $(VENV_PYTHON) || $(UV) venv $(VENV)
	$(MAKE) dev-install PYTHON=$(VENV_PYTHON)

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

test:
	$(PYTHON) -m pytest -q

identity:
	bash scripts/check_commit_identity.sh

check: lint identity test

MENUBAR_PYTHON ?= $(VENV_PYTHON)

menubar-install:
	@if command -v $(UV) >/dev/null 2>&1; then \
		$(UV) pip install --python $(MENUBAR_PYTHON) pyobjc-framework-Cocoa; \
	else \
		$(MENUBAR_PYTHON) -m pip install pyobjc-framework-Cocoa; \
	fi
