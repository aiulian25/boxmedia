#!/usr/bin/env bash
# BoxMedia pre-deploy gate: lint, tests, dependency audit, image vuln scan.
# Run from the repo root. Any failing stage aborts (set -e).
set -euo pipefail

VENV_PYTHON="${VENV_PYTHON:-.venv/bin/python}"
IMAGE="${IMAGE:-boxmedia:0.1.0}"

echo "==> ruff (lint)"
# The whole repo, not just app/: test and script files drifted because the gate never
# looked at them (pyproject's per-file-ignores already relax S101/S105/S106 for tests).
"$VENV_PYTHON" -m ruff check app tests scripts

echo "==> pytest (unit + integration)"
"$VENV_PYTHON" -m pytest -q

echo "==> pip-audit (known CVEs in the SHIPPED runtime dependencies)"
# Audits exactly what the image ships. Needs python3-venv on the host for isolation
# (apt install python3.12-venv). Trivy's image scan below also covers these packages.
"$VENV_PYTHON" -m pip_audit -r requirements-runtime.txt

echo "==> trivy (image vulnerability scan; .trivyignore holds documented waivers)"
if command -v trivy >/dev/null 2>&1; then
  # Scans both OS packages and the Python deps inside the image.
  trivy image --severity HIGH,CRITICAL --exit-code 1 --ignore-unfixed --scanners vuln "$IMAGE"
else
  echo "!! trivy not installed — install it, then: trivy image --severity HIGH,CRITICAL --exit-code 1 $IMAGE" >&2
  exit 2
fi

echo "==> all checks passed"
