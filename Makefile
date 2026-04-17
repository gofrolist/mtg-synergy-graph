.PHONY: test test-integration test-all lint import

# Fast unit suite — excludes `integration`-marked tests via pyproject addopts.
# This is what CI runs.
test:
	uv run pytest tests/ -q

# Integration suite — requires data/synergy.db (from scripts/import_cardsfolder.py)
# and data/forge/forge-gui/res/cardsfolder/ (from Card-Forge/forge). Skips
# cleanly if either is missing.
test-integration:
	uv run pytest tests/ -q -m integration --no-cov

# Full suite (unit + integration). Useful before cutting a release.
test-all:
	uv run pytest tests/ -q -m "integration or not integration"

lint:
	uv run ruff check src/ tests/ scripts/
	uv run ruff format --check src/ tests/ scripts/
	uv run pyright src/

import:
	uv run python scripts/import_cardsfolder.py
