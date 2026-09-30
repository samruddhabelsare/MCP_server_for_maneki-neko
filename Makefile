.PHONY: install check lint type test run

# ── Setup ─────────────────────────────────────────────────────────────────────
install:
	uv pip install -e ".[dev]"

# ── CI gate (run all checks) ──────────────────────────────────────────────────
check: lint type test

# ── Lint + format check ───────────────────────────────────────────────────────
lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

# ── Type check (strict on services/) ─────────────────────────────────────────
type:
	mypy src/maneki/services/

# ── Tests ─────────────────────────────────────────────────────────────────────
test:
	pytest -v

# ── Local dev server ──────────────────────────────────────────────────────────
run:
	uvicorn maneki.main:app --host 0.0.0.0 --port 8000 --reload
