default: run

run:
    uv run python -m assistant

test:
    uv run pytest -q

lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy src

fmt:
    uv run ruff format .
