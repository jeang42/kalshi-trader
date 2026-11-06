# Release audit — 08: Code quality and maintainability review

Independent code-quality pass over the public repository, 2026-11-05. Scope:
the maintainability and developer-experience findings left after the
security/safety audit (`01`–`07`). Not a repeat security review, not a
rewrite.

Status values below match `02-findings.md`: **Verified** (reproduced as
reported), **Partially verified** (real, but narrower or different than
reported), **Not reproduced**.

Every claim here was measured against the working tree. Where a number is
quoted, the command that produced it is given.

---

## A. Baseline

Repository state at the start of this pass:

| | |
|---|---|
| Branch / HEAD | `main` @ `5d47724` ("S-6: vendor htmx 1.9.12 instead of loading it from unpkg") |
| Working tree | clean |
| Tags | `v0.2.0a1` |
| Tracked Python files | 311 (`git ls-files '*.py'`), 50,964 lines excluding `attic/` and `tests/` |

Tooling. Two version sets are in play and both were exercised:

| Tool | Pinned (CI + pre-commit) | Local `.venv-ci` |
|---|---|---|
| Python | 3.11 | 3.11.18 |
| ruff | `0.6.9` (CI pin == hook rev) | 0.17.3 |
| mypy | `1.13.0` | 2.4.1 |
| pytest | `>=8.0.0`, floats | 9.2.0 |
| pip-audit | `2.10.1` | 2.x present |

Baseline check results (local versions, before any change):

```
ruff check .                     All checks passed!
make typecheck                   Success: no issues found in 45 source files
pytest --no-cov -q               2133 passed, 2 skipped
pytest -q  (coverage gate)       82% total, floor 80 — reached
```

The project arrived at this pass in good shape: lint clean, type-check clean,
a 2,133-test suite passing, a coverage floor that is actually met, and test
isolation (`conftest.py`) that blocks sockets and fails the run if anything
writes into the live `data/` tree. The findings below are real but none of
them is a correctness defect in the trading or research code.

Not run here: `make audit` (pip-audit, needs network; last run clean at
release — see `06-release-checklist.md`), `make smoke` (hits live Kalshi
endpoints — out of scope by the no-live-trades constraint), `make
docker-check` (no Docker daemon available in this environment).

---

## B. Findings

### F-1 — pre-commit mypy hook type-checked 1 file instead of 36

**Severity: Medium (silent loss of a quality gate). Status: Verified —
and worse than reported.**

Location: `.pre-commit-config.yaml`.

The hook selected files with
`files: ^(polymarket_client|portfolio|calibration|...|execution)\.py$` — a
list of module names from *before* the R-24 package split. Every one of those
modules now lives under `pm/`, and the pattern is anchored at `^`, so it
matched almost nothing.

Measured against the real tree:

```
total py files tracked:            311
matched by the pre-commit regex:     1   ->  live_gates.py   (a root shim)
intended targets (CI/Makefile):     36
```

The report said the hook "may not match the intended package paths". It
matched one path, and that path is a thin compatibility shim — so the local
type-check gate had been a no-op for as long as the `pm/` layout has existed.
CI kept its own correct copy of the list, which is why nothing surfaced it:
the gate was green in the one place anybody looked.

Demonstrated end to end rather than inferred. With a deliberate type error
added to `pm/venue/slippage.py`:

```
OLD hook:  Success: no issues found in 1 source file        -> Passed
NEW hook:  pm/venue/slippage.py:183: error: Incompatible return value type
           (got "str", expected "int")  [return-value]
           Found 1 error in 1 file (checked 45 source files) -> Failed
```

Root cause is not the regex, it is that one list existed in three places
(pre-commit, `.github/workflows/ci.yml`, `Makefile`), so two of them could
drift without the third noticing.

**Correction, and what was done:** the list now lives once, in
`[tool.mypy] files` in `pyproject.toml`. The Makefile target, the CI step and
the hook all invoke a bare `mypy`. The hook gets `pass_filenames: false` so it
checks the gated list rather than whatever happens to be staged. Coverage is
identical before and after — 45 source files, no issues. Cost on commit is
~2s warm; mypy's incremental cache absorbs it.

`tests/test_tooling_config.py` now pins the arrangement (9 tests). Mutation-
checked: reintroducing the original regex plus a pasted Makefile list fails
2 of them.

### F-2 — three tests required `.git` to exist

**Severity: Medium (false failure for anyone who downloads rather than
clones). Status: Verified — the report named one of the three.**

Locations: `tests/test_pm_layout.py`, `tests/test_provenance.py`.

Reproduced by extracting `git archive HEAD` into an empty directory:

```
before:  3 failed, 2130 passed, 2 skipped
  tests/test_pm_layout.py::test_repo_root_points_at_the_repository
  tests/test_provenance.py::test_git_commit_matches_git_rev_parse
  tests/test_provenance.py::test_stamp_and_line_shapes
```

The report named only the layout test. The two provenance failures came out of
the same reproduction; the second is a `TypeError` from
`re.fullmatch(..., None)` because `git_commit()` *correctly* returns `None`
with no repository.

**Correction, and what was done** — different treatment, because the
assumption means different things in the two files:

* The layout test was reading repository *metadata* to assert a fact about
  repository *content*. `pm.REPO_ROOT` does not need git to be right. It now
  asserts on markers only the root carries (`pyproject.toml`, `LICENSE`,
  `README.md`, `pm/__init__.py`), plus a new direct check that the root is not
  itself an importable package — which is the actual shape of the bug this
  file exists for (`emergency_stop`'s flag default silently becoming
  `pm/core/data/...` after the R-24 move). Coverage preserved and sharpened.
* The two provenance tests are **skipped**, not rewritten. They check
  agreement with git itself, which is only checkable where git is. Returning
  `None` without a repository is `provenance`'s documented contract, and
  `test_no_repo_gives_none_not_an_error` already pins that by monkeypatching
  `_repo_root`, so it runs everywhere.

```
after:   git clone           2162 passed,  2 skipped
         extracted, no .git  2160 passed,  4 skipped
```

### F-3 — public licence vs. package-distribution policy read as a contradiction

**Severity: Low (presentation). Status: Partially verified — the two facts
are not in conflict, but nothing said so where a reviewer looks.**

Location: `pyproject.toml`, `README.md`.

The report framed `license = {text = "Apache-2.0"}` alongside the
`Private :: Do Not Upload` classifier as contradictory metadata. It isn't:
the first is a licence grant, the second is a distribution choice, and the two
are independent. The previous audit already made this call deliberately
(`02-findings.md`, P1-E) and left an explanatory comment.

What the review is right about is legibility. The explanation sat inside a
classifier array, so a reviewer scanning metadata — or any tool that renders
classifiers — saw "Private" next to "Apache-2.0" with no context.

Two things were verified while resolving it:

* `Private :: Do Not Upload` is **not** a registered trove classifier
  (checked against the `trove_classifiers` package; no `Private ::` namespace
  exists). That is precisely the mechanism — PyPI rejects an upload carrying
  an unrecognised classifier, so the push fails at the index.
* The *stronger* guard was undocumented: there is no `[build-system]` table
  and no package-discovery config at all, so no artifact can be produced.
  `python -m build --wheel` fails before metadata is ever read:

  ```
  error: Multiple top-level packages discovered in a flat-layout:
  ['pm', 'wnba', 'edges', 'attic', 'tennis', 'deploy', 'research',
   'baseball', 'control_plane', 'user_strategies'].
  ```

**Correction, and what was done:** both guards kept; the reasoning written
down in `pyproject.toml` above `classifiers` and, in plain language, in the
README's License section ("Fork, copy, modify and redistribute freely under
those terms. Not published as a package on PyPI…"). Also recorded there: the
dependency tables are the manifest of record, while what you actually install
is `requirements.txt` or `requirements.lock.txt`. Three tests pin licence
consistency across `pyproject`/`LICENSE`/`NOTICE`/`README`, the upload
tripwire, and the absence of a build backend.

No automated publishing workflow was added, and none exists.

### F-4 — 26 documented commands no longer runnable as written

**Severity: Medium (documentation drift a new contributor hits immediately).
Status: Verified — not in the original report.**

Found by cross-checking every `python <name>.py` in the docs against
`git ls-files`. The R-24 split moved these modules under `pm/`; the prose was
never migrated with the code, so the commands fail with "No such file or
directory".

```
README.md                 1   (trade_poller — the only one on the front page)
docs/user-guide.md        4   (the one-off / manual-runs table)
docs/developer-guide.md   5   (incl. 3 rows of the systemd table, where
                               2 of 5 rows were already correct)
docs/module-notes.md     16
```

The correct convention was already established everywhere it is executed —
every `deploy/` unit file and every crontab line uses `python -m pm.<x>`, and
`docs/file-map.md` and `docs/module-notes.md` both explain it. This was pure
prose lag.

**Correction, and what was done:** rewritten to `python -m pm.<x>`. Each
replacement target was verified to exist and to have a `__main__` guard before
rewriting. Deliberately **not** touched: `CHANGELOG.md` and `attic/README.md`
(historical records of what was true at the time), and the
`broadcast_events.py` CLI line inside developer-guide's explicitly-retired
module section — that one is now marked as how it ran before retirement.

Also: the user guide's runnable-commands table offered
`.venv/bin/python broadcast_events.py` for a module retired to `attic/` on
2026-08-17. Row removed.

### F-5 — `pre-commit run --all-files` rewrote 17 files, 4 of which must not change

**Severity: Medium (corrupts vendored bytes; dirty tree for new contributors).
Status: Verified — not in the original report.**

Location: `.pre-commit-config.yaml`.

Running the hooks over a clean checkout — the natural thing to do after
`pre-commit install` — rewrote 17 files. Four of them should never be touched:

* `baseball/static/htmx.min.js`, `tennis/…`, `wnba/…` — vendored htmx 1.9.12
  (BSD-2-Clause, attributed in `NOTICE`, vendored in `5d47724` three commits
  earlier). Upstream ships it with no trailing newline, so `end-of-file-fixer`
  appends one to all three copies and they stop matching upstream. That
  defeats the point of vendoring and makes the NOTICE attribution inaccurate.
* `research/edge_2026_10_25/oi_confirmation.json` — recorded research output.
  Reformatting a result file is editing the research record.

The other 13 were the project's own whitespace debt: a trailing blank line at
EOF in nine `edges/` modules, two test files and `TODO.md`, plus one stray
trailing space in `docs/edge-research-log.md`.

**Correction, and what was done:** a top-level `exclude` for vendored static
assets, `research/*.json` and `docs/images/`, so no hook can rewrite those
bytes. Then the 13 genuine ones fixed, so the hooks are idempotent and a fresh
`pre-commit run --all-files` is clean rather than handing the contributor a
dirty tree. Verified whitespace-only rather than assumed: every changed `.py`
file parses to a byte-identical AST against `HEAD`, and both `.md` files are
identical with whitespace collapsed. No research content removed.

### F-6 — `fee_fn` failure fallback had no test coverage

**Severity: Low. Status: Verified — not in the original report.**

Location: `pm/research/backtest.py`.

`run_backtest`'s fee block fell back to flat `fee_pct` when a caller-supplied
`fee_fn` raised. Measured coverage showed the arm was dead:

```
pm/research/backtest.py  88%  missing: ..., 726-729, ...
```

Lines 726–729 were exactly the `except Exception` → `log.warning` → fallback
path. It was reachable only by running a whole backtest with a broken
callable, which no test did (`fee_fn` appears in the suite once, as a smoke
test in `tests/test_kalshi_fees.py`).

**Correction, and what was done:** the fee resolution was extracted to
`_trade_fee` (see section D) and the branch is now directly tested — a raising
`fee_fn` provably logs the underlying error and falls back to
`stake * fee_pct` instead of aborting the run, plus an end-to-end test that a
whole backtest survives it.

### F-7 — cash counters lost their `int` contract in transit

**Severity: Low (typing clarity; no runtime defect). Status: Verified.**

Location: `pm/research/backtest.py`, `run_backtest_walk_forward`.

`BacktestResult` declares `n_clipped_for_cash: int` / `n_skipped_for_cash: int`,
but walk-forward reads both out of a heterogeneous dict that also carries a
dollar figure, so their inferred type widened to `float`. Surfaced by mypy
only once the warnings assembly was extracted into a typed helper.

Not a bug: `_walk_forward_constrained` already stores `int(...)` for both, and
the result construction wraps them again. **Done:** coerced at the read site,
which changes nothing at runtime and makes the declared contract true. The
cleaner fix — a small frozen dataclass instead of the dict — is logged in
section F.

### F-8 — README inaccuracies

**Severity: Low. Status: Verified — not in the original report.**

* "CI runs … with four jobs: lint, typecheck, test, audit" — there are five.
  The missing one is `test-lock`, which is the one worth knowing about: it
  installs `requirements.lock.txt` and reruns the suite, proving the lock file
  still builds a working environment rather than only being scanned for CVEs.
* The Testing section told a contributor to run `make check` after
  `pip install -r requirements.txt`, which fails: `pip-audit` is deliberately
  not in `requirements.txt` and needs network. Now stated, with each target
  listed separately.
* `requires-python = ">=3.11"` and classifiers for 3.11/3.12/3.13, but the CI
  matrix is 3.11 only. The README now says so rather than implying three
  tested versions.

### Not reproduced / nothing to fix

* **Module sizes.** The six figures in the report are accurate to the line
  (`backtest.py` 1,684; `kalshi_client.py` 1,512; `strategy_runner.py` 1,490;
  `polymarket_client.py` 1,201; `kalshi_orders.py` 953; `execution.py` 846).
  Verified, but see section D — size is not the problem in most of them.
* **"Roughly 30 functions exceeding 100 lines."** Exactly 30, by AST census
  over tracked `.py` excluding `attic/` and `tests/`. Verified.
* **Permissive mypy settings.** Verified present and judged intentional —
  see section C.
* **Historical comments in production modules.** Assessed and left alone —
  see section C.

---

## C. Implemented improvements

Five local commits, in the order they were made.

**1. `d73d343` — Tooling: one mypy target list, and say what the package
metadata means.** F-1, F-3. `[tool.mypy] files` becomes the single source of
truth; Makefile / CI / hook all invoke a bare `mypy`; hook gets
`pass_filenames: false`. Package-metadata reasoning written into
`pyproject.toml` and the README. New `tests/test_tooling_config.py` (9 tests).

**2. `4f2c2c4` — Tests: don't require `.git`.** F-2. Layout test asserts on
root markers and non-packageness; the two provenance tests gain a
`needs_git` skip marker with the reason spelled out.

**3. `95dbbde` — backtest: extract the duplicated aggregation out of the two
engines.** F-6, F-7 and the refactor in section D. Four new module-private
helpers, 19 new tests.

**4. `bfa2305` — Docs: migrate pre-R-24 root-level commands to
`python -m pm.<x>`.** F-4, F-8.

**5. `b4aeded` — pre-commit: protect vendored bytes, then clear the
whitespace debt.** F-5.

### Deliberately not changed

* **Permissive mypy settings** (`disallow_untyped_defs = false`,
  `ignore_missing_imports = true`, `warn_return_any = false`). Left as-is, and
  they are defensible rather than merely tolerated: `ignore_missing_imports`
  is load-bearing because streamlit and plotly are untyped, and
  `check_untyped_defs = true` is already set — which is the setting that
  catches real bugs in unannotated code. Tightening is a staged project, not a
  config edit; sketched in section F. Typing work here was confined to code
  already being touched (F-7, and full annotations on the four new helpers),
  per the brief.
* **Historical comments and incident narratives.** Measured rather than
  eyeballed: comment-to-line ratio peaks at 28% in `app.py` (266 lines) and
  sits at 12–18% in the large modules; the longest contiguous comment block in
  the tree is 27 lines. Inspecting the longest blocks
  (`backtest.py`, `baseball/analysis.py`, `edges/macro.py`) they are
  architectural section headers explaining the model and its limits at the
  boundary they govern — exactly where that belongs. The long-form historical
  record already lives separately in `docs/module-notes.md`,
  `docs/edge-research-log.md` and `docs/failed-experiments.md`, so the
  separation this section of the brief asks for has already been done by the
  author. **Nothing moved, nothing deleted.** This area is a strength, not a
  debt.
* **Tool-version drift.** CI pins ruff 0.6.9 / mypy 1.13.0 while the local
  venv has 0.17.3 / 2.4.1. Both pass. Bumping the pins would import new rule
  sets and new pandas-typing findings into a concluded project; the pins are
  also deliberately matched between CI and the hook (with a comment saying
  why, after they drifted once). Left alone, noted in section F.
* **`requirements.txt` vs. the `dev` extra.** They differ slightly (`httpx`
  in one, `pre-commit` in the other). Since the project is not installable,
  `pip install .[dev]` cannot run at all, so the extra is inert documentation.
  Noted in `pyproject.toml` rather than reconciled.

---

## D. Refactoring assessment

### The five most consequential maintainability concerns

Ranked by cognitive load on an unfamiliar contributor, which is not the same
as ranked by line count. Complexity figures are branch-node counts from an AST
census (`if`/`for`/`while`/`except`/`bool-op`/`if-exp`/comprehension).

**1. `pm/research/backtest.py::run_backtest` — 329 lines, cyclomatic ~39.**
The worst function in the tree on both axes, and it mixed three jobs:
simulate trades, aggregate statistics, and construct the result. Worse, its
per-category breakdown and warnings assembly were **duplicated into
`run_backtest_walk_forward`** — the category block byte-for-byte — so there
were two places to fix one bug, and the warnings copies had already silently
diverged. **Improved** (below).

**2. `pm/arms/strategy_runner.py` — `_run_cycle_inner` 198 lines / cx ~32,
`_evaluate_exit` 115 / cx ~29.** The genuine architectural concern in the
repo. Unlike the venue clients it has real coupling — it imports from four
`pm/` layers plus `edges/` — and it holds the decision loop, position
management and exit logic together. **Deliberately left untouched.** It is the
module that transacts; the brief rules out refactoring order-execution paths
for cosmetic reasons, and this is the one place where an extraction error
would have financial consequences rather than aesthetic ones. Documented in
section F.

**3. Duplicated aggregation between the two backtest engines.** Called out
separately from (1) because it is the defect that *compounds*: a fix applied
to one engine and not the other produces two backtests that disagree, in a
codebase whose entire value proposition is that its measurements are
trustworthy. **Improved** (below).

**4. The venue clients' size — and why it is mostly not a problem.**
`kalshi_client.py` is 1,512 lines but holds 35 top-level functions (~43 lines
each) and imports from only `pm.core` and `pm.venue`;
`polymarket_client.py` is 1,201 lines and depends on `pm.core` alone. These
are flat, low-coupling API-surface modules — large because the venue's API
surface is large, not because responsibilities are tangled. The honest read is
that splitting them would relocate code without reducing what a contributor
must understand. **Deliberately left alone.** The real oversized unit inside
them is `get_closed_markets_in_range` (138 lines, cx ~22); logged in section F.

**5. Untyped dict-shaped internal contracts.** `_walk_forward_constrained`
returns `dict[str, float]` carrying two counts and a dollar figure, which is
how F-7 happened: the `int` contract was lost in transit and only a type
checker noticed. The pattern recurs (`by_category` entries are
`dict[str, Any]`). The minimal instance was fixed; the general case is a
staged typing project, section F.

### What was extracted

Three extractions from the worst function, all into the convention
`backtest.py` already follows — small module-private helpers alongside the
existing `_wilson_interval` / `_max_drawdown` / `_sample_size_warning` family:

| Helper | Lines | cx | Responsibility |
|---|---:|---:|---|
| `_trade_fee` | 38 | 4 | fee for one held-to-resolution trade, incl. the `fee_fn` fallback |
| `_summarize_by_category` | 28 | 5 | per-category breakdown of a finished trade log |
| `_quality_warnings` | 34 | 5 | warnings about whether the result means anything |
| `_cash_constraint_warnings` | 25 | 4 | warnings about the portfolio constraint having bitten |

The warnings split into *two* helpers rather than one on purpose:
walk-forward inserts its own signal-flip warning between the two groups, and
warning order is what the UI renders. Composing two groups at the call site
preserves that order explicitly, where a single helper would have needed an
awkward insertion parameter.

Result:

```
run_backtest               329 -> 273 lines,  cx ~39 -> ~24
run_backtest_walk_forward  212 -> 180 lines,  cx ~23 -> ~11
worst cyclomatic in repo   ~39 -> ~33  (now research/edge_2026_10_25/)
functions > 200 lines        2 -> 1
functions > 100 lines       30 -> 30   (unchanged — see below)
```

The ">100 lines" count is unchanged and that is the honest number: both
engines are still over 100 lines, and the extraction added no new function
anywhere near it. The objective was cognitive complexity, and the measurable
movement is there (38% and 52% complexity reductions in the two functions, and
one byte-for-byte duplication eliminated) — not in the line census.

**Behavioural equivalence was proved, not asserted.** Every field of every
`BacktestResult` was fingerprinted across 29 configurations — both engines,
both portfolio modes, three transaction-cost settings, per-trade max-position
on and off, the `fee_fn`-ok and `fee_fn`-raises paths, and the empty-result
path — before and after. 28 of the 29 produce real trades (60–164 each, five
categories, 84 warnings in total), so the fingerprint is not vacuous. The two
JSON dumps are byte-identical:

```
sha256  680021ce150cc9a0b8a042663d284d124859a68b92ac62ca4bf461ff762e6a83   (before)
sha256  680021ce150cc9a0b8a042663d284d124859a68b92ac62ca4bf461ff762e6a83   (after)
```

Strategies, Kelly sizing, fee arithmetic, spread handling and every recorded
research result are untouched. Error handling and logging are preserved
exactly — the `fee_fn` fallback still logs the same warning with the same
fields, and is now tested for it.

### Considered and rejected

* **Splitting `run_backtest` into simulate + aggregate.** The remaining 273
  lines are a cash-trajectory loop with `nonlocal` state (`cash`, `pending`,
  `min_cash`, two counters). Extracting it means either threading five
  mutable values through a signature or inventing a state object — more
  abstraction than the readability buys, and real regression risk in the
  module that produces the research record. Logged, not done.
* **Unifying the empty-result early return.** `run_backtest` constructs
  `BacktestResult` twice, ~30 fields each, which is a genuine hazard (add a
  field, forget one site). But merging them needs a conditional per differing
  field, which reads *worse* than the duplication. Logged, not done.
* **Order-execution paths** (`execution.py::_place_unguarded`, 161 lines /
  cx ~28; `kalshi_orders.py::place_limit_order`, 138 / cx ~20). Left
  untouched by design. These carry the mandatory caps, the atomic daily-cap
  reservation and the fail-closed audit writes that `03-order-safety.md`
  documents; their complexity *is* the safety logic, and the brief is explicit
  that cosmetic refactoring does not justify touching them.

---

## E. Testing

All results below are from this environment (`.venv-ci`, Python 3.11.18),
reported as observed.

### Final state

| Check | Command | Result |
|---|---|---|
| Compilation | `python -m compileall -q pm edges baseball tennis wnba control_plane user_strategies research scripts` | OK, no errors |
| Lint | `ruff check .` | All checks passed |
| Lint (pinned) | `pre-commit run ruff --all-files` (ruff 0.6.9) | Passed |
| Type check | `mypy` (bare, 45 files) | Success: no issues found |
| Type check (pinned) | `pre-commit run mypy --all-files` (mypy 1.13.0) | Passed |
| All hooks | `pre-commit run --all-files` | Passed, idempotent, tree clean |
| Suite | `pytest --no-cov -q` | **2162 passed, 2 skipped** |
| Suite + gate | `pytest -q` | **82.02%**, floor 80 — reached |
| Suite, no `.git` | extracted `git archive HEAD`, `make test-fast` | **2160 passed, 4 skipped** |
| Workflow, no `.git` | extracted, `make lint` + `make typecheck` | both pass |

Baseline was 2,133 passed / 2 skipped. 29 tests added: 9 tooling-config,
19 backtest helpers, 1 layout. The 2-test difference between the clone and
archive runs is the two `needs_git` skips from F-2, by design.

### Regression procedure for the one behavioural refactor

1. Baseline captured: 29-configuration fingerprint of every `BacktestResult`
   field, written to JSON (section D).
2. Characterization gap closed first: the previously-dead `fee_fn` fallback
   (F-6) was included in the baseline before extraction, so the refactor could
   not quietly change it.
3. Extraction applied.
4. Fingerprint re-captured — byte-identical, same sha256.
5. Targeted tests added for each extracted helper (19), including the fallback
   branch, the zero-entry-price guard, blank-category handling, zero-stake ROI,
   warning ordering in both engines, and a test that both engines still produce
   the same by-category table shape.
6. Full suite + coverage gate re-run.

### Additional verification

* **F-1 proved by mutation**, both directions: a deliberate type error in
  `pm/venue/slippage.py` passes the old hook ("1 source file") and fails the
  new one ("checked 45 source files"). Both configs restored cleanly
  afterwards.
* **The new guard tests proved to actually guard**: reintroducing the original
  regex and a pasted Makefile list fails 2 of the 9 tooling tests.
* **F-5 changes proved whitespace-only** by AST comparison against `HEAD` for
  all 11 changed `.py` files, and whitespace-collapsed text comparison for the
  2 `.md` files.
* **Final verification run from the committed tree** (`git archive HEAD`, no
  `.git`), not from the working directory.

### Limitations — not run, or not runnable here

* `make audit` (pip-audit): needs network. Not run. Last result clean at
  release (`06-release-checklist.md`); dependencies unchanged by this pass.
* `make smoke` (`scripts/smoke_test.py`): hits real Kalshi endpoints.
  **Deliberately not run** — the no-live-trades constraint. Nothing in this
  pass touches venue I/O.
* `make docker-check`: no Docker daemon available.
* CI itself was not exercised; the workflow change was verified by running the
  same bare `mypy` command locally and by `pre-commit run`, not by a CI run.
* Python 3.12/3.13 were not tested (no environments provisioned); this is F-8.
* **No test contacted a live order-submission endpoint.** The suite blocks
  `socket.connect` for every test and the session tripwire confirms nothing
  wrote into the live `data/` tree; both passed on every run above.

---

## F. Remaining technical debt

Meaningful, in rough priority order:

1. **`strategy_runner.py` decision loop** — `_run_cycle_inner` (198 lines /
   cx ~32) and `_evaluate_exit` (115 / cx ~29), with coupling across four
   `pm/` layers plus `edges/`. The highest-value remaining refactor and the
   riskiest; it needs a characterization harness over the arm cycle before
   anything is moved. Worth doing only if the project is ever reactivated.
2. **`run_backtest`'s simulation loop** — 273 lines of cash-trajectory logic
   over five `nonlocal` values. Extractable behind a small state object;
   deferred as more abstraction than the readability currently buys.
3. **Duplicated `BacktestResult` construction** in `run_backtest` (~30 fields,
   twice). Real maintenance hazard; merging it reads worse than the
   duplication, so it wants a different idea rather than a mechanical fix.
4. **Dict-shaped internal contracts.** `_walk_forward_constrained`'s return
   dict and the `dict[str, Any]` category entries. A `TypedDict` or small
   frozen dataclass each would make these self-documenting. F-7 is the
   evidence that the looseness is not harmless.
5. **Staged typing plan**, if anyone wants one. In order of value per unit of
   churn: (a) `disallow_untyped_defs` per-module on `pm/core/` and
   `pm/venue/` first — smallest, most-depended-on, already well annotated;
   (b) `warn_return_any = true` once (a) is clean; (c) adopt `pandas-stubs`
   last and on its own, since CI already records that it surfaces ~39
   pre-existing pandas-typing errors — real work, not a config tweak. Do not
   flip global settings; the module list in `[tool.mypy] files` is the natural
   ratchet.
6. **`get_closed_markets_in_range`** in `kalshi_client.py` (138 lines /
   cx ~22) — pagination, filtering and windowing in one function, on a
   read-only path. The one clean extraction candidate left in the venue
   clients.
7. **Tool-version drift.** CI pins ruff 0.6.9 / mypy 1.13.0; current releases
   are 0.17.x / 2.x. Both pass today. A bump will surface new findings and
   should be its own pass.
8. **3.12 / 3.13 are declared but untested.** Either add a CI matrix or narrow
   the classifiers. The README now states the gap, which is the honest
   minimum.

Stylistic preferences, explicitly *not* debt: module line counts in the venue
clients; the long architectural comment blocks; the permissive-but-deliberate
mypy settings; `E501` being ignored; the `attic/` tree; the duplication
between `requirements.txt` and the inert `dev` extra.

---

## G. Final quality assessment

Evidence-based, and critical where criticism is earned.

**Architecture — strong.** The `pm/` layer split is real, not nominal: a
contract test enforces that a package may only import from layers below it,
and it passes. The venue boundary is where order safety is enforced (mandatory
caps inside `place_limit_order`, not in callers), which is the correct place
for it. The `Broker` seam separates paper from live execution. Coupling is
genuinely low — the two largest modules in the tree depend on one and two
`pm/` packages respectively. The one weak spot is `strategy_runner.py`, which
reaches across four layers and concentrates the decision loop, position
management and exit logic in one place.

**Code organization — good, with known large modules.** Seven modules over 800
lines, two over 1,500. Most are large because they front a large external API
surface and are internally flat (35 functions averaging 43 lines), which is a
defensible shape. The real organizational defect this pass found was
duplication rather than size: two backtest engines carrying byte-identical
aggregation blocks. That is now one copy.

**Maintainability — good, and measurably better than at baseline.** 30
functions over 100 lines in ~51k lines of code is unremarkable. The worst
function in the tree dropped 38% in cyclomatic complexity with a byte-identical
output fingerprint. What remains is concentrated and identified rather than
diffuse.

**Type discipline — adequate and honest, not strong.** `check_untyped_defs`
with a curated 36-module gate is a pragmatic middle, and mypy is clean on it.
But the gate had been silently reduced to one file locally (F-1), annotations
are optional, return types are unchecked, and internal contracts pass through
untyped dicts — F-7 is a small instance of exactly the class of looseness that
permits. This is the weakest of the dimensions here. It is a deliberate
trade-off with a documented rationale, which is better than an accident, but
it is still the thing most likely to let a refactor regress silently.

**Test engineering — the standout.** 2,162 tests, an 80% floor that is met at
82%, and — more telling — isolation built from scar tissue rather than
convention: `socket.connect` refused for every test after a test was found
issuing a real authenticated DELETE to Kalshi; a session tripwire that
fingerprints the live `data/` tree and fails the run if anything in it
changes; every env-overridable store redirected to `tmp_path`; a comment
explaining why `testpaths = ["."]` and not `["*.py"]` because the glob
imported `app.py` at collection time and wrote four files into the real data
tree. That is test engineering done by someone who got burned and fixed the
category rather than the instance. The gaps found here were narrow: three
tests coupled to git metadata, and one dead error-handling branch.

**Documentation — excellent in substance, with mechanical drift.** Fourteen
cross-referenced documents including a research log, a failed-experiments
post-mortem file, a strategy scoreboard with honest negative verdicts, and
per-module long-form notes. The maintenance-status section sets expectations
better than most actively-developed projects manage. Against that: 26
documented commands no longer ran as written, a retired module was still
offered in a runnable-commands table, and the CI job count was wrong — drift
in the mechanical details while the substantive record stayed accurate. All
fixed in the live docs.

**Developer experience — good after this pass, with two real potholes before
it.** `make`-driven, pinned tool versions, a lock file that CI proves still
builds a working environment, pre-commit configured. But a source-ZIP download
failed three tests, and `pre-commit run --all-files` rewrote 17 files
including vendored third-party assets. Both were the kind of first-five-minutes
failure that costs a prospective contributor disproportionate confidence. Both
fixed and pinned by tests.

**Public repository presentation — strong.** The README states what the
project is, that it is concluded, that the author found no durable edge, and
what a reader should not assume about the live-execution code. `SECURITY.md`,
`NOTICE`, `CHANGELOG.md` and the `docs/release-audit/` trail are all present.
The licence/distribution ambiguity (F-3) was the one thing likely to make a
reviewer pause, and it is now stated plainly in both places a reviewer looks.

**Overall.** This is professional-quality Python engineering. The strongest
evidence is not the lint status or the test count but the pattern visible in
`conftest.py` and in the comment blocks: defects were repeatedly converted
into enforced invariants rather than fixed in place. The findings in this pass
are consistent with that — they cluster in the *mechanical* layer (a stale
regex, a stale path, a `.git` assumption, vendored bytes) rather than in
design or correctness, and several had survived precisely because they were
invisible to the one place anybody checked.

The weakest real dimension is type discipline, and the largest remaining
maintainability concern is `strategy_runner.py` — neither of which is a
blocker for a research release that is explicitly not offered as a supported
trading product.

---

## Completion status against the brief

| Criterion | Status |
|---|---|
| Verified tooling mistakes corrected | Yes — F-1, F-5 |
| Source archives and git checkouts both supported | Yes — F-2, verified in both |
| Public licence metadata clear | Yes — F-3 |
| Most valuable low-risk improvements implemented | Yes — section C and D |
| Trading and research behaviour unchanged | Yes — byte-identical fingerprint; no venue, strategy, sizing or model code touched |
| Relevant tests pass | Yes — 2162/2 clone, 2160/4 archive, 82.02% coverage |
| Remaining concerns documented | Yes — section F |
| Quality report complete | This document |
| No real orders, deployments or pushes | Confirmed — no network writes, no deploy, no push; 5 local commits only |
