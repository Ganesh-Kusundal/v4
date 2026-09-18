/**
 * A content hash of a file tree, in one place so the writer and the checker of a
 * hash cannot disagree about how it was computed.
 *
 * Algorithm, stated once because `trading/tests/analytics/test_goldens_provenance.py`
 * re-implements it in Python and the two must produce identical hex:
 *
 *   files   = every regular file under `dir`, recursively, excluding `exclude`
 *   rel     = path relative to `dir`, `/`-separated
 *   order   = `rel` sorted ascending (byte order; both sides sort the full path,
 *             not per directory — the two differ for `a/b.json` vs `a.json`)
 *   digest  = sha256 over, for each file in order: rel, 0x00, contents, 0x00
 *
 * Content, not timestamps: a `git checkout` rewrites the mtime of unchanged files,
 * and a timestamp check would then call a build of identical sources stale.
 */
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

/** Every regular file under `dir`, as `/`-separated paths relative to it, sorted. */
export function walkFiles(dir) {
  const out = [];
  const visit = (current, prefix) => {
    for (const name of readdirSync(current)) {
      const path = join(current, name);
      const rel = prefix === '' ? name : `${prefix}/${name}`;
      if (statSync(path).isDirectory()) visit(path, rel);
      else if (statSync(path).isFile()) out.push(rel);
    }
  };
  visit(dir, '');
  return out.sort();
}

/** `sha256:<hex>` for the tree, or `null` when `dir` does not exist. */
export function hashTree(dir, { exclude = () => false } = {}) {
  let files;
  try {
    files = walkFiles(dir);
  } catch {
    return null;
  }
  const hash = createHash('sha256');
  for (const rel of files) {
    if (exclude(rel)) continue;
    hash.update(rel);
    hash.update('\0');
    hash.update(readFileSync(join(dir, rel)));
    hash.update('\0');
  }
  return `sha256:${hash.digest('hex')}`;
}

/** `sha256:<hex>` of one file's bytes, or `null` when it does not exist. */
export function hashFile(path) {
  try {
    return `sha256:${createHash('sha256').update(readFileSync(path)).digest('hex')}`;
  } catch {
    return null;
  }
}
