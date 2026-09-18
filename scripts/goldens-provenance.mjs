/**
 * The record of what the checked-in goldens were captured from.
 *
 * `trading/tests/analytics/goldens/*.json` are ground truth for the backend's
 * parity suite, and until now nothing said where they came from. That left two
 * silent failure modes:
 *
 *   - a fixture set regenerated against a different (or stale) library build, and
 *   - a fixture or two hand-edited, or regenerated without its companions,
 *
 * both of which leave the parity tests green while the "ground truth" drifts from
 * the engine it is supposed to describe.
 *
 * So every generation writes `PROVENANCE.json` next to the fixtures, and
 * `trading/tests/analytics/test_goldens_provenance.py` fails when any of it stops
 * being true. The hashes are over file *contents* with the algorithm documented in
 * `scripts/hash-tree.mjs` — the Python side re-implements it and they must agree.
 */
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { hashFile, hashTree } from './hash-tree.mjs';
import { repoRoot } from './pin.mjs';

/** `trading/tests/analytics/goldens/`. */
export const goldensDir = (root = repoRoot()) =>
  join(root, 'trading', 'tests', 'analytics', 'goldens');

/** Not captured *from* the library: the fixture inputs, and this record itself. */
const NOT_CAPTURED = new Set(['PROVENANCE.json', 'fixtures.json']);

/** Hash of every captured fixture, relative to `goldens/`. */
export const goldensHash = (root = repoRoot()) =>
  hashTree(goldensDir(root), { exclude: (rel) => NOT_CAPTURED.has(rel) });

/** Hash of the fixture candles the goldens were computed from. */
export const fixturesHash = (root = repoRoot()) =>
  hashFile(join(goldensDir(root), 'fixtures.json'));

/**
 * Write `PROVENANCE.json` from `library` (a `resolveLibrary()` result) and what is
 * on disk now. Idempotent, and deliberately last: it records the fixtures as they
 * exist after the generators have run, so a partial regeneration is visible as a
 * hash mismatch rather than as a missing file.
 */
export function writeProvenance(library, { root = repoRoot(), generators = [] } = {}) {
  const record = {
    library: 'openalgo-charts',
    sha: library.sha,
    version: library.version,
    pinned: library.pinned,
    distHash: library.hash,
    fixturesSha256: fixturesHash(root),
    goldensSha256: goldensHash(root),
    generators,
    generatedAt: new Date().toISOString(),
  };
  writeFileSync(join(goldensDir(root), 'PROVENANCE.json'), `${JSON.stringify(record, null, 1)}\n`);
  return record;
}
