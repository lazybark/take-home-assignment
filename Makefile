.PHONY: up dev down test lint lock logs venv

up:        ## Start the API as evaluated: gunicorn, no reloader (prod mode, the default)
	docker compose up --build

dev:       ## Start with live reload for development (a code change restarts it, like a crash)
	APP_MODE=dev docker compose up --build

down:
	docker compose down

test:
	docker compose run --rm --no-deps api uv run pytest

lint:      ## Formatting and lint checks, as CI runs them
	docker compose run --rm --no-deps api sh -c "uv run ruff format --check src tests scripts && uv run ruff check src tests scripts"

lock:      ## Regenerate uv.lock (uv is not required on the host)
	docker run --rm -v "$$PWD":/app -w /app ghcr.io/astral-sh/uv:python3.12-bookworm-slim uv lock

logs:
	docker compose logs -f api

venv:      ## Local .venv for the IDE only (needs uv on the host: brew install uv)
	uv sync --python 3.12
