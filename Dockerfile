# Reproducible-environment proof + second-machine baseline (P3/R-26).
#
# Building this image IS the verification: it stands the repo up from nothing
# — python:3.11-slim, dependencies from requirements.lock.txt ONLY, an empty
# data/ tree — and runs the full gate (ruff + mypy + the 2,100-test suite,
# which needs no network; the root conftest blocks sockets outright). If the
# build succeeds, the environment is reproducible on any machine with Docker.
# This is non-API gotcha #7 ("a developer machine hides isolation and
# dependency bugs — verify against a fresh clone with an empty data/ in a
# clean 3.11 venv") turned from a habit into an artifact:  make docker-check
#
# What this image deliberately is NOT: the production deployment. Production
# is systemd user units + linger + cron (deploy/), where systemd is the
# orchestrator, journald the log sink, and the OnFailure alert hook the
# monitoring — containerizing the fleet would re-implement all of that for a
# single-machine system, and the audio/GPU stack (HDHomeRun, PulseAudio,
# faster-whisper) does not belong in a container anyway. For standing up a
# real second machine, see docs/second-machine.md.
FROM python:3.11-slim

# git: provenance.py resolves the commit from .git, and one test cross-checks
# against `git rev-parse`. make: the gate targets.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first, so source edits don't re-run pip.
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY . .

# The gate. test-fast = the full suite without the coverage pass (coverage
# thresholds are CI's job; the image proves the environment).
RUN make lint typecheck test-fast

CMD ["bash"]
