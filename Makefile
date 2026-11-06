.PHONY: test test-fast coverage lint typecheck audit check run smoke docker-check

# Full suite: unit tests + doctests + coverage report
test:
	pytest

# Fast run without coverage (useful during development)
test-fast:
	pytest --no-cov -q

# Ruff linter
lint:
	ruff check .

# Mypy on the gated (non-Streamlit) modules. The target list lives in
# pyproject.toml's `[tool.mypy] files`, so a bare `mypy` here checks exactly
# what CI and the pre-commit hook check.
typecheck:
	mypy

# Dependency vulnerability scan against the lock file (P2/R-22). Needs
# network. pip-audit is not in requirements.txt (it drags in its own tree);
# install once with `pip install pip-audit`.
audit:
	pip-audit -r requirements.lock.txt --strict --progress-spinner off --desc on

# R-26: the clean-room proof. Builds python:3.11-slim + the LOCK + an empty
# data/ and runs the full gate inside — gotcha #7 as an artifact. --no-cache
# so "it passed" always means "it passed today, from nothing".
docker-check:
	docker build --no-cache -t polymarket-check .

# Everything CI runs (audit needs network + pip-audit installed; CI runs it
# as its own job, weekly too)
check: lint typecheck test audit

# Open the HTML coverage report
coverage: test
	open htmlcov/index.html 2>/dev/null || xdg-open htmlcov/index.html

# Live-API smoke test — hits real Kalshi endpoints, real filesystem.
# Run before declaring a release "good"; unit tests can't catch the
# kinds of integration issues this surfaces (auth flips, fetch-path
# bias, cron mutating shared state, etc.).
smoke:
	python scripts/smoke_test.py

# Start the Streamlit app
run:
	streamlit run app.py --server.address "$${BIND_HOST:-127.0.0.1}"
