.PHONY: help install check-ports up down logs ps migrate test lint format health

help:
	@echo "install      uv sync (incl. dev deps)"
	@echo "up           check ports, start postgres(:5435) + redis(:6380), wait for healthy"
	@echo "down         stop containers (volumes kept)"
	@echo "logs         follow container logs"
	@echo "migrate      alembic upgrade head"
	@echo "test         pytest"
	@echo "lint         ruff check + format check"
	@echo "format       ruff format + autofix"
	@echo "health       deskmind health"

install:
	uv sync

check-ports:
	uv run python scripts/check_ports.py

up: check-ports
	docker compose up -d --wait

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

migrate:
	uv run alembic upgrade head

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

health:
	uv run deskmind health
