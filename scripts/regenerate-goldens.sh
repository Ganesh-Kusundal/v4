#!/usr/bin/env bash
# Regenerate every checked-in fixture and golden, in the one order that is coherent.
#
#   scripts/regenerate-goldens.sh
#
# The fixtures come first (the goldens are computed from them), then the three
# generators, then PROVENANCE.json is written once from the whole directory.
#
# Refuses to run unless the library checkout is at the commit in
# `openalgo-charts.pin` and its `dist/` is newer than its `src/` (see
# scripts/library-dist.mjs) — and it runs that library, not a path somebody
# typed. Bumping the pin therefore means: check it out, build it, run this, and
# review the diff. Reviewing the diff is the only place a library version bump
# shows up as a change to ground truth; the numbers themselves will look fine.
#
# `OPENALGO_CHARTS_ROOT` points at a different checkout; `ALLOW_UNPINNED=1` records
# goldens from a candidate commit (and says so in PROVENANCE.json).
#
# Everything is run through the venv's python when there is one, and `python3`
# otherwise, so the fixtures are byte-identical to the ones pytest reads back.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="python3"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON=".venv/bin/python"
fi

if [[ "${ALLOW_UNPINNED:-0}" == "1" ]]; then
  echo "! ALLOW_UNPINNED=1: capturing from whatever the checkout is on"
  export OAC_ALLOW_UNPINNED=1
fi

echo "== fixtures =="
"$PYTHON" scripts/generate_fixtures.py

echo "== indicator goldens =="
node scripts/generate_goldens.mjs

echo "== transform goldens =="
node scripts/generate_goldens_transforms.mjs

echo "== profile / seasonality goldens =="
node scripts/generate_goldens_profiles.mjs

echo
echo "== provenance =="
node scripts/write-provenance.mjs

echo
# The record is only worth writing if it describes what is on disk, so read it
# back through the same gate the test suite uses rather than trusting the write.
echo "== verify (the same check the pytest gate runs) =="
"$PYTHON" -c "
import sys
sys.path.insert(0, 'trading/tests/analytics')
from test_goldens_provenance import verify
verify()
print('provenance verified')
"

echo
echo "Review the diff before committing: 'git diff --stat trading/tests/analytics/goldens'"
echo "Then run: $PYTHON -m pytest trading/tests/analytics -q"
