/**
 * The chart library's built artifacts, verified before anything captures values
 * out of them.
 *
 * Why this exists: the golden generators under `scripts/` write the fixtures the
 * backend's parity suite treats as ground truth, and they used to import the
 * library by an absolute path into one developer's `~/Downloads` — a *different*
 * checkout (2.1.7, while the pin is 2.1.8), with no check that its `dist/`
 * corresponded to its `src/`. Capturing goldens from a stale build there writes
 * wrong ground truth and the parity tests then pass against it, because they only
 * ever read the fixtures. Nothing downstream can detect that, so the check has to
 * happen here, before a single value is captured.
 *
 * Three things are verified, each because it can fail on its own:
 *
 *   1. the checkout is at the pinned SHA (`openalgo-charts.pin`),
 *   2. `dist/` is newer than the sources it is built from (upstream commits no
 *      `dist/`, so a `git pull` changes `src/` and leaves the build behind),
 *   3. the entry module the caller asked for resolves, through the library's own
 *      `exports` map rather than a filename this file guesses at.
 *
 * `allowUnpinned` exists for a deliberate experiment against a candidate version.
 * It has to be asked for by name — the option or `OAC_ALLOW_UNPINNED=1` — and it
 * says so on stdout, so it cannot quietly become the way goldens are regenerated.
 */
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { hashTree, walkFiles } from './hash-tree.mjs';
import { readPin, repoRoot } from './pin.mjs';

/** Library checkout: `$OPENALGO_CHARTS_ROOT`, else `<repo>/openalgo-charts`. */
export const libraryRoot = (root = repoRoot()) =>
  process.env.OPENALGO_CHARTS_ROOT ?? join(root, 'openalgo-charts');

/** Newest mtime in a file tree, in ms. 0 for a directory with no files. */
function newestMtime(dir) {
  let newest = 0;
  for (const rel of walkFiles(dir)) newest = Math.max(newest, statSync(join(dir, rel)).mtimeMs);
  return newest;
}

/** The commit the checkout is on, or null when it is not a git working tree. */
export function checkoutSha(libRoot) {
  try {
    return execFileSync('git', ['rev-parse', 'HEAD'], {
      cwd: libRoot,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  } catch {
    return null;
  }
}

/**
 * Resolve the library under `root` and refuse to return it unless it can be trusted.
 *
 * `kinds` are the export subpaths the caller will import (`indicators`, `transform`,
 * `profile`, …), resolved through the library's own `exports` map.
 *
 * @param {{ root?: string, kinds?: string[], allowUnpinned?: boolean }} [options]
 */
export function resolveLibrary({
  root = repoRoot(),
  kinds = [],
  allowUnpinned = process.env.OAC_ALLOW_UNPINNED === '1',
} = {}) {
  const pin = readPin(root);
  const libRoot = libraryRoot(root);
  if (!existsSync(libRoot)) {
    throw new Error(
      `no chart library checkout at ${libRoot}.\n` +
        '  git clone https://github.com/marketcalls/openalgo-charts.git openalgo-charts\n' +
        `  cd openalgo-charts && git checkout ${pin.sha} && npm ci && npm run build\n` +
        '  (or set OPENALGO_CHARTS_ROOT to a checkout that is already there)',
    );
  }

  const sha = checkoutSha(libRoot);
  if (sha !== pin.sha) {
    const verdict =
      sha === null ? 'not a git working tree (so its commit cannot be checked)' : `at ${sha}`;
    const message =
      `${libRoot} is ${verdict}, but the goldens are a snapshot of ${pin.sha}` +
      `${pin.label === '' ? '' : ` (${pin.label})`}.\n` +
      '  Check out the pin, or bump openalgo-charts.pin deliberately and regenerate.';
    if (!allowUnpinned) throw new Error(message);
    console.warn(`! capturing against an UNPINNED library: ${message}`);
  }

  const dist = join(libRoot, 'dist');
  if (!existsSync(dist)) {
    throw new Error(
      `${dist} does not exist — upstream commits no dist/, so it has to be built:\n` +
        `  cd ${libRoot} && npm ci && npm run build`,
    );
  }

  // Built-after-source, by mtime. Weaker than provenance on its own, and paired
  // with the dist hash recorded in PROVENANCE.json, which is the durable proof of
  // which bytes a fixture set came from.
  const srcDir = join(libRoot, 'src');
  const manifestNewest = Math.max(
    ...['package.json', 'rollup.config.mjs', 'rollup.config.js', 'tsconfig.json']
      .map((f) => join(libRoot, f))
      .filter((f) => existsSync(f))
      .map((f) => statSync(f).mtimeMs),
    0,
  );
  const srcNewest = Math.max(existsSync(srcDir) ? newestMtime(srcDir) : 0, manifestNewest);
  const distOldest = Math.min(...walkFiles(dist).map((rel) => statSync(join(dist, rel)).mtimeMs));
  if (distOldest < srcNewest) {
    throw new Error(
      `${dist} is older than ${libRoot}/src — it is a stale build.\n` +
        `  cd ${libRoot} && npm run build\n` +
        `  (dist: ${new Date(distOldest).toISOString()}, src: ${new Date(srcNewest).toISOString()})`,
    );
  }

  const manifest = JSON.parse(readFileSync(join(libRoot, 'package.json'), 'utf8'));

  /**
   * `import()`-ready URL for an export subpath, via the library's own exports map.
   *
   * @param {string} kind e.g. `indicators`
   */
  const entry = (kind) => {
    const key = `./${kind}`;
    const target = manifest.exports?.[key];
    if (target === undefined || target.import === undefined) {
      throw new Error(
        `the library's package.json has no "${key}" export — the artifact this ` +
          'generator needs is not in the build, so the checked-in goldens cannot be ' +
          'regenerated from it.',
      );
    }
    const path = join(libRoot, target.import);
    if (!existsSync(path)) {
      throw new Error(
        `${key} resolves to ${target.import}, which is missing from ${dist}. A partial ` +
          'build: run `npm run build` in the library.',
      );
    }
    return pathToFileURL(path).href;
  };

  return {
    root: libRoot,
    dist,
    sha: sha ?? pin.sha,
    pinned: sha === pin.sha,
    version: manifest.version ?? 'unknown',
    hash: hashTree(dist),
    entry,
    /** Ready-to-`await import()` URLs, in the order of `kinds`. */
    modules: kinds.map(entry),
  };
}
