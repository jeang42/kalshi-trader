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

# Mypy on the core (non-Streamlit) modules
typecheck:
	mypy pm/venue/polymarket_client.py pm/ledger/portfolio.py pm/ledger/calibration.py pm/ledger/alerts.py pm/ledger/risk.py pm/core/profiles.py pm/venue/provider.py pm/venue/kalshi_client.py pm/research/backtest.py pm/feeds/crypto_data.py edges/ pm/feeds/llm_data.py pm/feeds/llm_matcher.py pm/feeds/llm_cache.py pm/feeds/sports_data.py pm/feeds/manifold_data.py pm/feeds/macro_data.py pm/venue/slippage.py pm/archive/book_archive.py pm/capture/book_poller.py pm/arms/strategy_runner.py pm/research/backtest_history.py pm/venue/kalshi_auth.py pm/venue/kalshi_orders.py pm/feeds/llm_deep_analysis.py pm/feeds/web_context.py pm/ops/auto_resolve_runner.py pm/ops/bulk_deep_analysis.py pm/core/atomic_io.py pm/trading/live_gates.py pm/ops/backup_sqlite.py pm/trading/execution.py pm/core/sqlite_schema.py pm/core/shutdown.py pm/core/provenance.py pm/core/circuit_breaker.py

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
	streamlit run app.py
