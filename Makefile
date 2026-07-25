PYTHON := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: install install-api install-web dev-api dev-web test lint format-check typecheck build check

install: install-api install-web

install-api:
	python3 -m venv .venv
	$(PIP) install -e 'apps/api[dev]'

install-web:
	npm --prefix apps/web install

dev-api:
	cd apps/api && ../../$(PYTHON) -m uvicorn flowguard.main:app --reload --port 8000

dev-web:
	npm --prefix apps/web run dev

test:
	cd apps/api && ../../$(PYTHON) -m pytest

lint:
	cd apps/api && ../../$(PYTHON) -m ruff check flowguard tests
	npm --prefix apps/web run lint

format-check:
	cd apps/api && ../../$(PYTHON) -m ruff format --check flowguard tests

typecheck:
	npm --prefix apps/web run typecheck

build:
	npm --prefix apps/web run build

check: test lint format-check typecheck build
