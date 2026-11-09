# Install exactly the locked dependency graph
.PHONY: setup test lint check
setup:
	uv sync --locked

# Run unit and integration checks without provider credentials
test:
	uv run --locked python -m unittest discover -s tests -v

# Check source layout and common implementation errors
lint:
	uv run --locked ruff check .
	uv run --locked ruff format --check .

# Keep the aggregate gate reproducible
check: lint test
