# Task runner. Install `just` (https://just.systems) or copy the commands verbatim.

set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

# Run the kernel locally
dev:
    uv run python -m myagent

# Run the test suite
test:
    uv run pytest

# Lint (and report formatting drift)
lint:
    uv run ruff check src tests
    uv run ruff format --check src tests

# Auto-fix lint + formatting
fix:
    uv run ruff check --fix src tests
    uv run ruff format src tests

# Static type checking
typecheck:
    uv run pyright

# Everything CI runs
check: lint typecheck test
