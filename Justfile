# Task runner. Install `just` (https://just.systems) or copy the commands verbatim.

set windows-shell := ["powershell.exe", "-NoLogo", "-Command"]

# Start everything: kernel + voice + overlay + HUD (Ctrl+C stops all)
start:
    uv run python -m myagent.start

# Run only the kernel
dev:
    uv run python -m myagent

# Run only the overlay orb (kernel must be running)
overlay:
    uv run python -m myagent.overlay

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
