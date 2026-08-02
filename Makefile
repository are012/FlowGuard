PYTHON := .venv/bin/python
PIP := .venv/bin/pip

.PHONY: install install-api install-ai install-web migrate migration-parity migration-check dev-api dev-ai dev-web test web-test lint format-check typecheck build check

install: install-ai install-web

install-api:
	python3 -m venv .venv
	$(PIP) install -e 'apps/api[dev]'

install-ai: install-api
	$(PIP) install -e 'apps/api[ai]'

install-web:
	npm --prefix apps/web install

migrate:
	cd apps/api && ../../$(PYTHON) -m alembic upgrade head

migration-parity:
	cd apps/api && ../../$(PYTHON) -m flowguard.schema_parity

migration-check:
	cd apps/api && ../../$(PYTHON) -m alembic check

dev-api: migrate
	cd apps/api && ../../$(PYTHON) -m uvicorn flowguard.main:app --reload --port 8000

dev-ai:
	$(PYTHON) -m uvicorn --app-dir apps/ai-service main:app --reload \
		--reload-dir apps/ai-service --reload-dir apps/api/flowguard --port 8001

dev-web:
	npm --prefix apps/web run dev

test:
	cd apps/api && ../../$(PYTHON) -m pytest

web-test:
	npm --prefix apps/web run test

lint:
	cd apps/api && ../../$(PYTHON) -m ruff check flowguard tests
	npm --prefix apps/web run lint

format-check:
	cd apps/api && ../../$(PYTHON) -m ruff format --check flowguard tests

typecheck:
	npm --prefix apps/web run typecheck

build:
	npm --prefix apps/web run build

check: test web-test lint format-check typecheck build
